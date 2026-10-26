"""P2 direct maintenance, condition checks and supported repairs. Rule endpoints route each propagation wave."""

import hashlib
import json
from uuid import uuid4
from .contracts import (
    DirectDecision,
    DirectPlan,
    Targets,
    TriggerDecision,
    RepairDecision,
    ref_of,
    rule_queries,
    split_ref,
)
from .model_io import (
    OUTPUT_ATTEMPTS,
    OutputValidationFailed,
    LocalReferences,
    timed_text,
    validated_completion,
)
from .schemas import normalize_entities, vector_literal
from .storage import SCOPE


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


class MaintenanceExecutor:
    def __init__(self, store, retriever, indexer, client, *, retry, price=None, intent_classifier=None):
        self.store, self.retriever, self.indexer = store, retriever, indexer
        self.client, self.retry, self.price = client, retry, price
        self.intent_classifier = intent_classifier

    def render_request(self, name, inputs):
        instructions = self.store.prompts.text(name)
        if name == "p2_repair":
            instructions = instructions.replace(
                "{target_statement}", json.dumps(inputs["target"]["text"], ensure_ascii=False)
            )
            inputs = {k: v for k, v in inputs.items() if k != "target"}
        return instructions + "\n" + json.dumps(inputs, ensure_ascii=False, default=str)

    async def ask(self, name, inputs, refs, validate, context):
        rendered = self.render_request(name, inputs)
        await self.store.record(
            context,
            name + ".input",
            {"prompt": rendered, "reference_map": refs.decode_map,
             **({"target": inputs["target"]["ref"]} if name == "p2_repair" else {})},
        )
        result = await validated_completion(
            self.client,
            context,
            stage=name,
            log_stage=name,
            rendered=rendered,
            retry=self.retry,
            price=self.price,
            validate=validate,
        )
        await self.store.record(
            context,
            name + ".output",
            {
                "result": (
                    result.model_dump() if hasattr(result, "model_dump") else result
                )
            },
        )
        return result

    async def current(self, memory_id, context, conn):
        return await conn.fetchrow(
            f"""SELECT * FROM memories WHERE {SCOPE} AND memory_id=$4
            AND temporal_scope='current' ORDER BY version DESC LIMIT 1""",
            *context.trace.args,
            memory_id,
        )

    async def mutate(self, ref, values, context, conn):
        before = await self.store.read_ref(context.trace, ref, conn=conn)
        columns = (
            "status",
            "status_reason",
            "temporal_scope",
            "accepted",
            "invalidated",
            "basis_refs",
        )
        if not set(values) <= set(columns):
            raise ValueError("unsupported memory mutation")
        assignments = ",".join(f"{k}=${i}" for i, k in enumerate(values, 6))
        await conn.execute(
            f"UPDATE memories SET {assignments} WHERE {SCOPE} AND memory_id=$4 AND version=$5",
            *context.trace.args,
            *split_ref(ref),
            *values.values(),
        )
        after = await self.store.read_ref(context.trace, ref, conn=conn)
        await self.store.record(
            context,
            "p2.memory_change",
            {"before": dict(before), "after": dict(after)},
            conn=conn,
        )

    async def process(self, focus, candidates, context, *, query_vector=None, intent=None):
        """Return every accepted reference and transition for one extracted item."""
        try:
            if focus["type"] == "forget_request":
                intent = "forget"
                targets = await self.forget_scope(focus, None, context, query_vector=query_vector)
                if not targets:
                    await self.store.record(context, "p2.forget_no_targets", {"incoming": focus})
                    return [], []
                decision = DirectDecision(action="forget", target_ref=targets[0])
                return [], await self.apply_forget(focus, decision, targets, context)
            if self.intent_classifier is None:
                key, changes = await self.direct(focus, candidates, context)
                return [key] if key else [], changes
            if focus["type"] == "rule":
                key, changes = await self.apply_direct(
                    focus, DirectDecision(action="new", target_ref=None), context
                )
                return [key], changes
            if intent not in ("new", "correction", "hold"):
                raise ValueError("content requires a session-batch intent")
            if intent == "hold":
                await self.store.record(context, "p2.held", {"incoming": focus})
                return [], []
            plan = await self.plan_direct(
                intent, focus, candidates, context, query_vector=query_vector,
            )
        except OutputValidationFailed as exc:
            await self.store.record(context, "p2.direct_skipped", {
                "incoming": focus, "intent": intent, "attempts": OUTPUT_ATTEMPTS,
                "error": str(exc), "action": "skip_item",
            })
            return [], []

        if not plan.operations:
            await self.store.record(context, "p2.held", {"incoming": focus, "intent": intent})
            return [], []
        prepared = []
        for operation in plan.operations:
            target = candidates.get(operation.target_ref)
            content = dict(focus)
            if operation.text is not None:
                content.update(text=operation.text, entities=normalize_entities(operation.entities))
            if target:
                content["type"] = "rule" if target["type"] == "rule" else "content"
            decision = DirectDecision(action=operation.action, target_ref=operation.target_ref)
            embedding = await self.direct_embedding(content, decision, context)
            prepared.append((content, decision, embedding))
        accepted, transitions = [], []
        # Validate the whole plan and obtain all embeddings before any memory write.
        # A storage failure rolls back every operation for this incoming item.
        async with self.store.pool.acquire() as conn, conn.transaction():
            for content, decision, embedding in prepared:
                if decision.action in ("invalidate", "retract", "revoke"):
                    transitions.extend(await self.remove_direct(content, decision, context, conn))
                else:
                    key, changes = await self.write_direct(content, decision, embedding, context, conn)
                    accepted.append(key)
                    transitions.extend(changes)
        return accepted, transitions

    async def plan_direct(self, intent, focus, candidates, context, *, query_vector=None):
        candidates = {
            key: row for key, row in candidates.items()
            if row["accepted"] and row["status"] != "deleted"
            and (intent == "correction" or row["type"] == "fact")
        }
        current = {ref_of(r) for r in await self.store.current_rows(context.trace)}
        if intent == "new":
            candidates = await self.retriever.filter_direct_candidates(
                focus, candidates, context, current_refs=current, query_vector=query_vector,
            )
            if not candidates:
                action = {"current": "new", "historical": "history"}[focus["temporal_scope"]]
                plan = DirectPlan(operations=[dict(
                    action=action, target_ref=None,
                    text=focus["text"], entities=focus["entities"],
                )])
                await self.store.record(context, "p2_new.direct_save", {
                    "reason": "no_candidates", "incoming": focus,
                    "result": plan.model_dump(),
                })
                return plan
        refs = LocalReferences(list(candidates.values()))
        inputs = {
            "incoming": {"text": focus["text"]},
            "candidates": [dict(
                ref=refs.encode(key), text=row["text"], type=row["type"],
                matter=refs.matters[str(row["memory_id"])],
                is_current=key in current, temporal_scope=row["temporal_scope"],
                status=row["status"], invalidated=row["invalidated"], origin=row["origin"],
            ) for key, row in candidates.items()],
        }
        allowed = {
            "new": {"new", "update", "duplicate", "history", "invalidate"},
            "correction": {"correct", "update", "retract", "revoke", "invalidate"},
        }[intent]

        def validate(raw):
            plan = DirectPlan.model_validate_json(raw)
            seen, written, current_matters = set(), set(), set()
            for operation in plan.operations:
                if operation.action not in allowed:
                    raise ValueError(f"{operation.action} is not allowed for {intent}")
                if operation.target_ref:
                    operation.target_ref = refs.decode(operation.target_ref)
                    if operation.target_ref in seen:
                        raise ValueError("use each target version at most once")
                    seen.add(operation.target_ref)
                    target = candidates[operation.target_ref]
                    if target["type"] == "rule" and operation.text is not None:
                        rule_queries(operation.text)
                    if operation.action in ("update", "invalidate", "revoke") and operation.target_ref not in current:
                        raise ValueError("this operation requires a current target")
                    if operation.action == "revoke" and target["type"] != "rule":
                        raise ValueError("revoke requires a rule")
                    if operation.action == "invalidate" and target["type"] != "fact":
                        raise ValueError("invalidate requires a fact")
                    if operation.action == "duplicate" and (
                        target["status"] != "fresh" or target["invalidated"] or target["origin"] == "derived"
                    ):
                        raise ValueError("reconfirm an unavailable or derived record with update, not duplicate")
                    if operation.action in ("update", "correct") and operation.target_ref in current:
                        matter = target["memory_id"]
                        if matter in current_matters:
                            raise ValueError("only one new current version per matter")
                        current_matters.add(matter)
                if operation.text is not None:
                    scope = "historical" if operation.action == "history" else "current"
                    if operation.action == "correct" and operation.target_ref not in current:
                        scope = "historical"
                    signature = (operation.text.strip(), scope)
                    if signature in written:
                        raise ValueError("do not write the same content twice; retire redundant current targets instead")
                    written.add(signature)
            return plan

        return await self.ask("p2_" + intent, inputs, refs, validate, context)

    async def direct(self, focus, candidates, context):
        current = {ref_of(r) for r in await self.store.current_rows(context.trace)}
        refs = LocalReferences(list(candidates.values()))
        inputs = {
            "incoming": dict(timed_text(focus), type=focus["type"]),
            "candidates": [
                dict(
                    refs.describe(r),
                    type=r["type"],
                    matter=refs.matters[str(r["memory_id"])],
                    is_current=ref_of(r) in current,
                    status=r["status"],
                    invalidated=r["invalidated"],
                    origin=r["origin"],
                )
                for r in candidates.values()
            ],
        }

        def validate(raw):
            result = DirectDecision.model_validate_json(raw)
            if result.target_ref:
                result.target_ref = refs.decode(result.target_ref)
                if result.target_ref not in candidates:
                    raise ValueError("target must be a supplied old candidate")
                target = candidates[result.target_ref]
                expected_type = "rule" if focus["type"] == "rule" else "fact"
                if (
                    result.action in ("update", "correct", "duplicate", "history")
                    and target["type"] != expected_type
                ):
                    raise ValueError("replacement must match the target memory type")
                if (
                    result.action in ("update", "revoke", "invalidate")
                    and result.target_ref not in current
                ):
                    raise ValueError("this operation requires the current version")
                if result.action == "revoke" and target["type"] != "rule":
                    raise ValueError("revoke requires a rule")
                if result.action == "invalidate" and target["type"] != "fact":
                    raise ValueError("invalidate requires a fact")
                if result.action == "duplicate" and (
                    target["status"] != "fresh"
                    or target["invalidated"]
                    or target["origin"] == "derived"
                ):
                    raise ValueError(
                        "only a fresh direct assertion can be duplicate; reconfirmation is update"
                    )
            return result

        try:
            decision = await self.ask("p2_direct", inputs, refs, validate, context)
            forget_refs = (
                await self.forget_scope(focus, decision.target_ref, context)
                if decision.action == "forget"
                else None
            )
        except OutputValidationFailed as exc:
            await self.store.record(
                context,
                "p2.direct_skipped",
                {
                    "incoming": focus,
                    "attempts": OUTPUT_ATTEMPTS,
                    "error": str(exc),
                    "action": "skip_item",
                },
            )
            return None, []
        return await self.apply_direct(
            focus, decision, context, forget_refs=forget_refs
        )

    async def forget_scope(self, focus, primary, context, *, query_vector=None):
        candidates = await self.retriever.forget_candidates(
            focus, context, primary, query_vector=query_vector,
        )
        refs = LocalReferences(list(candidates.values()))
        inputs = {
            "request": {"text": focus["text"]},
            "primary_ref": refs.encode(primary) if primary else None,
            "candidates": [
                dict(
                    ref=refs.encode(ref_of(r)),
                    text=r["text"],
                    type=r["type"],
                    matter=refs.matters[str(r["memory_id"])],
                )
                for r in candidates.values()
            ],
        }

        def validate(raw):
            result = Targets.model_validate_json(raw)
            selected = [refs.decode(r) for r in result.target_refs]
            if (primary and primary not in selected) or len(selected) != len(set(selected)):
                raise ValueError(
                    "forget scope must include the primary target and distinct supplied references"
                )
            return selected

        return await self.ask("p2_forget_scope", inputs, refs, validate, context)

    async def apply_direct(self, focus, decision, context, *, forget_refs=None):
        action = decision.action
        if action == "hold":
            await self.store.record(
                context,
                "p2.held",
                {"incoming": focus, "source_session": context.session_seq},
            )
            return None, []
        if action in ("forget", "revoke", "invalidate", "retract"):
            return None, await self.apply_removal(
                focus, decision, context, forget_refs=forget_refs
            )
        embedding = await self.direct_embedding(focus, decision, context)
        async with self.store.pool.acquire() as conn, conn.transaction():
            return await self.write_direct(focus, decision, embedding, context, conn)

    async def direct_embedding(self, focus, decision, context):
        if decision.action not in ("new", "update", "correct", "history"):
            return None
        await self.indexer.index_topics(focus["topics"], context)
        (vector,) = await self.indexer._embed(
            [self.store.config.document_prefix + focus["text"]], context, "indexing"
        )
        return vector_literal(vector, self.store.config.dimensions)

    async def write_direct(self, focus, decision, embedding, context, conn):
        action = decision.action
        memory_type = "rule" if focus["type"] == "rule" else "fact"
        transitions = []
        target = (
            await self.store.read_ref(context.trace, decision.target_ref, conn=conn)
            if decision.target_ref
            else None
        )
        if target and (
            target["status"] == "deleted" or target["type"] != memory_type
        ):
            raise ValueError("invalid direct target")
        latest = (
            await self.current(target["memory_id"], context, conn)
            if target
            else None
        )
        if (
            target
            and action == "update"
            and (latest is None or ref_of(latest) != ref_of(target))
        ):
            raise ValueError("update requires the current target")
        scope = "historical" if action == "history" else "current"
        if target and action in ("correct", "duplicate"):
            scope = target["temporal_scope"]
            if action == "correct" and (
                latest is None or ref_of(latest) != ref_of(target)
            ):
                scope = "historical"
        if action == "duplicate":
            if target["status"] != "fresh" or target["invalidated"] or target["origin"] == "derived":
                raise ValueError("duplicate requires a fresh direct assertion")
            previous = await conn.fetchval(
                f"SELECT session_seq FROM memory_sources WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                *context.trace.args,
                target["memory_id"],
                target["version"],
            )
            if previous is None:
                raise ValueError("duplicate target has no direct session source")
            merged = sorted(set(previous) | {context.session_seq})
            await conn.execute(
                f"UPDATE memory_sources SET session_seq=$6 WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                *context.trace.args,
                target["memory_id"],
                target["version"],
                merged,
            )
            key = ref_of(target)
            await self.store.record(
                context,
                "p2.sources_merged",
                {"ref": key, "before": previous, "after": merged},
                conn=conn,
            )
        else:
            uid = target["memory_id"] if target else uuid4()
            version = await conn.fetchval(
                f"SELECT coalesce(max(version),0)+1 FROM memories WHERE {SCOPE} AND memory_id=$4",
                *context.trace.args,
                uid,
            )
            new = await self.store._insert(
                conn,
                context.trace,
                context.session_seq,
                focus["text"],
                memory_type,
                focus["entities"],
                normalize_entities(
                    focus["topics"] + (list(target["topics"]) if target else [])
                ),
                "extracted",
                uid,
                version,
                [context.session_seq],
            )
            key = f"{new[0]}:{new[1]}"
            await conn.execute(
                f"UPDATE memories SET search_vector=to_tsvector($6::regconfig,text),memory_embedding=$7::vector WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                *context.trace.args,
                *new,
                self.store.config.text_config,
                embedding,
            )
            await self.mutate(
                key,
                {
                    "status": "fresh",
                    "accepted": True,
                    "temporal_scope": scope,
                    "status_reason": "Direct " + action,
                },
                context,
                conn,
            )
            if target and action in ("update", "correct"):
                await self.mutate(
                    ref_of(target),
                    {
                        "status": "stale",
                        "invalidated": target["invalidated"] or action == "correct",
                        "status_reason": "Direct " + action,
                    },
                    context,
                    conn,
                )
                transitions.extend(
                    await self.invalidate_basis(
                        [ref_of(target)],
                        context,
                        conn,
                        exclude_history=action == "correct",
                    )
                )
            if scope == "current":
                transitions.insert(
                    0,
                    {
                        "operation": action,
                        "before": dict(target) if target else None,
                        "after": dict(
                            await self.store.read_ref(context.trace, key, conn=conn)
                        ),
                    },
                )
        await self.store.record(
            context,
            "p2.direct_applied",
            {
                "incoming": focus,
                "decision": decision.model_dump(),
                "result": key,
                "source_session": context.session_seq,
            },
            conn=conn,
        )
        return key, transitions

    async def apply_removal(self, focus, decision, context, *, forget_refs=None):
        action = decision.action
        if action == "forget":
            if not forget_refs:
                raise ValueError("forget requires a validated scope")
            return await self.apply_forget(focus, decision, forget_refs, context)
        async with self.store.pool.acquire() as conn, conn.transaction():
            return await self.remove_direct(focus, decision, context, conn)

    async def remove_direct(self, focus, decision, context, conn):
        action, key = decision.action, decision.target_ref
        transitions = []
        target = await self.store.read_ref(context.trace, key, conn=conn)
        if target is None or target["status"] == "deleted":
            raise ValueError("removal target unavailable")
        current = await self.current(target["memory_id"], context, conn)
        is_current = current and ref_of(current) == key
        if action in ("revoke", "invalidate") and not is_current:
            raise ValueError("removal requires a current target")
        if (
            action == "revoke"
            and target["type"] != "rule"
            or action == "invalidate"
            and target["type"] != "fact"
        ):
            raise ValueError("removal target has the wrong type")
        rows = [target]
        for row in rows:
            await self.mutate(
                ref_of(row),
                {
                    "status": "stale",
                    "invalidated": row["invalidated"]
                    or action in ("retract", "revoke"),
                    "status_reason": "Explicit " + action,
                },
                context,
                conn,
            )
        transitions.extend(
            await self.invalidate_basis(
                [ref_of(r) for r in rows],
                context,
                conn,
                exclude_history=action != "invalidate",
            )
        )
        changed = target if is_current else None
        if changed:
            transitions.append(
                {"operation": action, "before": dict(changed), "after": None}
            )
        await self.store.record(
            context,
            "p2.removal_applied",
            {
                "incoming": focus,
                "decision": decision.model_dump(),
                "source_session": context.session_seq,
            },
            conn=conn,
        )
        return transitions

    async def apply_forget(self, focus, decision, targets, context):
        matter_ids = sorted({split_ref(ref)[0] for ref in targets})
        async with self.store.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                f"SELECT * FROM memories WHERE {SCOPE} AND memory_id=ANY($4::uuid[]) AND status<>'deleted' ORDER BY memory_id,version",
                *context.trace.args,
                matter_ids,
            )
            current = [await self.current(uid, context, conn) for uid in matter_ids]
            for row in rows:
                await self.mutate(
                    ref_of(row),
                    {"status": "deleted", "status_reason": "Explicit forget"},
                    context,
                    conn,
                )
            transitions = await self.invalidate_basis(
                [ref_of(r) for r in rows], context, conn, exclude_history=True
            )
            transitions.extend(
                {"operation": "forget", "before": dict(row), "after": None}
                for row in current
                if row and row["status"] != "deleted"
            )
            await self.store.record(
                context,
                "p2.removal_applied",
                {
                    "incoming": focus,
                    "decision": decision.model_dump(),
                    "scope_refs": targets,
                    "source_session": context.session_seq,
                },
                conn=conn,
            )
        return transitions

    async def invalidate_basis(self, roots, context, conn, *, exclude_history=False):
        """Invalidate actual derivations using exact premises, including historical correction."""
        todo, seen, transitions = set(roots), set(), []
        while todo:
            wave = todo - seen
            if not wave:
                break
            seen |= wave
            rows = await conn.fetch(
                f"SELECT * FROM memories WHERE {SCOPE} AND origin='derived' AND accepted AND status<>'deleted' AND basis_refs ?| $4::text[]",
                *context.trace.args,
                sorted(wave),
            )
            todo = set()
            for row in rows:
                ref = ref_of(row)
                latest = await self.current(row["memory_id"], context, conn)
                is_current = latest and ref_of(latest) == ref
                if not exclude_history and not is_current:
                    continue
                if row["status"] == "stale" and (
                    not exclude_history or row["invalidated"]
                ):
                    # Already invalidated roots still lead to unvisited descendants.
                    todo.add(ref)
                    continue
                await self.mutate(
                    ref,
                    {
                        "status": "stale",
                        "invalidated": row["invalidated"] or exclude_history,
                        "status_reason": (
                            "A used premise was withdrawn"
                            if exclude_history
                            else "A current premise changed"
                        ),
                    },
                    context,
                    conn,
                )
                todo.add(ref)
                if is_current:
                    transitions.append(
                        {
                            "operation": "basis_invalidated",
                            "before": dict(row),
                            "after": None,
                        }
                    )
        return transitions

    async def transition_materials(self, transitions, context, *, for_repair=False):
        rendered = []
        for index, t in enumerate(transitions, 1):
            value = {"operation": t["operation"]}
            if not for_repair:
                value["id"] = f"C{index}"
            for side in ("before", "after"):
                row = t[side]
                if row is None:
                    value[side] = None
                    continue
                now = await self.store.read_ref(context.trace, ref_of(row))
                # Prior values describe transitions, not current premises.
                # Withdrawn contents must remain unavailable to repair.
                unavailable = now["status"] == "deleted" or now["invalidated"]
                value[side] = {
                    "text": (
                        "[unavailable]" if for_repair and unavailable else row["text"]
                    ),
                    "type": row["type"],
                    "status_now": now["status"],
                }
            rendered.append(value)
        return rendered

    @staticmethod
    def changed_matters(transitions):
        return {t[side]["memory_id"] for t in transitions for side in ("before", "after") if t[side]}

    async def routed_rules(self, transitions, context, binding_refs=()):
        ids = self.changed_matters(transitions)
        rule_ids = {t[side]["memory_id"] for t in transitions for side in ("before", "after")
                    if t[side] and t[side]["type"] == "rule"}
        rule_ids.update(split_ref(key)[0] for key in binding_refs)
        return await self.store.active_rules(context.trace, condition_ids=ids, rule_ids=rule_ids)

    async def check_conditions(self, transitions, protected, context, *, rules=None, binding_refs=()):
        rules = rules if rules is not None else await self.routed_rules(transitions, context, binding_refs)
        if not rules:
            return []
        # Binding is a reason to inspect the current state, never evidence that a source changed.
        transitions = list(transitions)
        for key in binding_refs:
            if key in rules and rules[key]['memory_id'] not in self.changed_matters(transitions):
                transitions.append({'operation': 'binding', 'before': None, 'after': rules[key]})
        ids = {uid for rule in rules.values() for field in ('condition_matter_ids', 'affected_matter_ids')
               for uid in rule[field]}
        current = {r['memory_id']: r for r in await self.store.current_rows(context.trace, ids)}
        targets, checks, used_rules, permitted = {}, [], {}, {}
        refs = LocalReferences(list(current.values()))
        relevant_indices = set()
        for condition, rule in rules.items():
            facts = [r for uid in rule['affected_matter_ids'] if (r := current.get(uid))
                     and r['type'] == 'fact' and r['status'] == 'fresh' and r['accepted']
                     and not r['invalidated'] and ref_of(r) not in protected]
            if not facts:
                continue
            source_ids = set(rule['condition_matter_ids'])
            changes = [f'C{i}' for i, t in enumerate(transitions, 1)
                       if self.changed_matters([t]) & (source_ids | {rule['memory_id']})]
            if not changes:
                continue
            relevant_indices.update(int(c[1:]) - 1 for c in changes)
            used_rules[condition] = rule
            rendered_facts = []
            for row in facts:
                key = ref_of(row)
                targets[key] = row
                permitted.setdefault(key, set()).update(changes)
                fact = {'ref': refs.encode(key), 'text': row['text']}
                incorporated = [f'C{i}' for i, t in enumerate(transitions, 1)
                                if f'C{i}' in changes and t['after'] and ref_of(t['after']) in row['basis_refs']]
                if incorporated:
                    fact['incorporates_changes'] = incorporated
                rendered_facts.append(fact)
            sources = []
            for uid in rule['condition_matter_ids']:
                row = current.get(uid)
                usable = row and row['accepted'] and row['status'] == 'fresh' and not row['invalidated']
                sources.append({'ref': refs.encode(ref_of(row)) if row else None,
                                'text': row['text'] if usable else None,
                                'status': 'fresh' if usable else 'unknown'})
            checks.append({'rule': {'text': rule['text']}, 'sources': sources,
                           'change_ids': changes, 'facts': rendered_facts})
        if not targets:
            return []
        rendered_changes = await self.transition_materials(transitions, context)
        inputs = {'changes': [r for i, r in enumerate(rendered_changes) if i in relevant_indices], 'checks': checks}
        await self.store.record(
            context,
            "p2.condition_groups",
            {
                "rules": {
                    condition: [
                        key
                        for key, row in targets.items()
                        if row["memory_id"] in used_rules[condition]["affected_matter_ids"]
                    ]
                    for condition in used_rules
                }
            },
        )

        def validate(raw):
            result = TriggerDecision.model_validate_json(raw)
            supplied_changes = {change["id"] for change in inputs["changes"]}
            selected = set()
            for trigger in result.triggers:
                trigger.fact_ref = refs.decode(trigger.fact_ref)
                if trigger.fact_ref not in targets or trigger.fact_ref in selected:
                    raise ValueError(
                        "fact_ref must name a distinct supplied fresh target"
                    )
                selected.add(trigger.fact_ref)
                if len(set(trigger.change_ids)) != len(trigger.change_ids) or any(
                    change_id not in supplied_changes or change_id not in permitted[trigger.fact_ref]
                    for change_id in trigger.change_ids
                ):
                    raise ValueError("change_ids must name distinct supplied changes")
            return result

        decision = await self.ask("p2_conditions", inputs, refs, validate, context)
        result = []
        async with self.store.pool.acquire() as conn, conn.transaction():
            for trigger in decision.triggers:
                key = trigger.fact_ref
                before = await self.store.read_ref(context.trace, key, conn=conn)
                if before["status"] != "fresh":
                    continue
                await self.mutate(
                    key,
                    {
                        "status": "stale",
                        "status_reason": "A bound rule condition was triggered",
                    },
                    context,
                    conn,
                )
                await self.store.record(
                    context, "p2.condition_evidence",
                    {"target": key, "changes": self.change_references([
                        transitions[int(change_id[1:]) - 1]
                        for change_id in trigger.change_ids
                    ])},
                    conn=conn,
                )
                result.append(
                    {
                        "operation": "condition_triggered",
                        "before": dict(before),
                        "after": None,
                    }
                )
                result.extend(await self.invalidate_basis([key], context, conn))
        return result

    @staticmethod
    def change_references(transitions):
        return [
            {"operation": t["operation"], **{
                side: ref_of(t[side]) if t[side] else None
                for side in ("before", "after")
            }}
            for t in transitions
        ]

    async def repair_evidence(self, target, transitions, context):
        """Follow selected causes and existing bases, never all session writes."""
        saved = await self.store.latest_repair_evidence(context.trace, ref_of(target))
        evidence = []
        for change in saved["changes"] if saved else []:
            evidence.append({"operation": change["operation"], **{
                side: dict(await self.store.read_ref(context.trace, change[side]))
                if change[side] else None
                for side in ("before", "after")
            }})
        rules = await self.store.active_rules(context.trace, affected_ids=[target["memory_id"]])
        related = {split_ref(ref)[0] for ref in target["basis_refs"]}
        related.update(uid for r in rules.values() for uid in r["condition_matter_ids"])
        related.update(
            t[side]["memory_id"] for t in evidence
            for side in ("before", "after") if t[side]
        )
        evidence.extend(
            t for t in transitions
            if any(t[side] and t[side]["memory_id"] in related for side in ("before", "after"))
        )
        # Stable reference-only records preserve actual change evidence across sessions.
        unique = {fingerprint(refs): t for refs, t in zip(self.change_references(evidence), evidence)}
        evidence = list(unique.values())
        references = self.change_references(evidence)
        if references and (not saved or references != saved["changes"]):
            await self.store.record(context, "p2.repair_evidence", {
                "target": ref_of(target), "changes": references,
            })
        current = await self.store.current_rows(context.trace, related)
        materials = {
            ref_of(r): r for r in current
            if r["memory_id"] in related
            and r["memory_id"] != target["memory_id"]
            and r["type"] == "fact" and r["status"] == "fresh"
            and r["accepted"] and not r["invalidated"]
        }
        materials.update(rules)
        return materials, evidence

    async def repair_inputs(self, target, materials, evidence, context, *, retrieval_available):
        """One compact rendering shared by execution and offline input previews."""
        refs = LocalReferences(list(materials.values()))
        text = target["text"] if not target["invalidated"] else (
            "[obsolete value unavailable; use the attached rules to identify the matter]"
        )
        return {
            "target": {"text": text, "ref": ref_of(target)},
            "materials": [
                {"ref": refs.encode(key), "text": r["text"], "type": r["type"]}
                for key, r in materials.items()
            ],
            "changes": await self.transition_materials(evidence, context, for_repair=True),
            "retrieval_available": retrieval_available,
        }, refs

    async def repair(self, target, transitions, context, attempted):
        key = ref_of(target)
        if (
            target["type"] != "fact"
            or target["status"] != "stale"
            or not target["accepted"]
        ):
            return None
        materials, evidence = await self.repair_evidence(target, transitions, context)
        if not any(r["type"] == "rule" for r in materials.values()):
            return None
        for retrieval_available in (True, False):
            inputs, refs = await self.repair_inputs(
                target, materials, evidence, context, retrieval_available=retrieval_available,
            )
            signature = fingerprint({
                "target": key, "text": inputs["target"]["text"],
                "materials": sorted(materials), "changes": inputs["changes"],
                "retrieval_available": retrieval_available,
            })
            async with self.store.pool.acquire() as conn:
                previous = await conn.fetchrow(
                    f"SELECT stage,payload FROM memory_steps WHERE {SCOPE} "
                    "AND stage IN ('p2.repair_result','p2.repair_skipped') "
                    "AND payload->>'signature'=$4 ORDER BY step_id DESC LIMIT 1",
                    *context.trace.args, signature,
                )

            def validate(raw):
                result = RepairDecision.model_validate_json(raw)
                if result.retrieval_query is not None and not retrieval_available:
                    raise ValueError("retrieval already used; decide from supplied evidence")
                result.basis_refs = [refs.decode(r) for r in result.basis_refs]
                if len(set(result.basis_refs)) != len(result.basis_refs) or any(
                    r not in materials for r in result.basis_refs
                ):
                    raise ValueError("repair uses distinct supplied current premises, never the target")
                if result.can_repair and {
                    materials[r]["type"] for r in result.basis_refs
                } != {"fact", "rule"}:
                    raise ValueError("repair requires an explicit rule and current fact premises")
                return result

            if previous:
                if previous["stage"] == "p2.repair_skipped":
                    return None
                result = RepairDecision.model_validate(previous["payload"]["result"])
            else:
                if signature in attempted:
                    return None
                attempted.add(signature)
                try:
                    result = await self.ask("p2_repair", inputs, refs, validate, context)
                except OutputValidationFailed as exc:
                    await self.store.record(context, "p2.repair_skipped", {
                        "signature": signature, "target": key, "attempts": OUTPUT_ATTEMPTS,
                        "error": str(exc), "action": "keep_stale",
                    })
                    return None
                await self.store.record(context, "p2.repair_result", {
                    "signature": signature, "target": key, "result": result.model_dump(),
                })
            if result.can_repair:
                break
            if not result.retrieval_query:
                return None
            # Only a concrete missing fact warrants a supplementary search.
            hits = await self.retriever.search(
                result.retrieval_query, context, through_session=context.session_seq,
                purpose="p2", memory_type="fact", exclude_id=target["memory_id"], limit=5,
                topics=normalize_entities(
                    list(target["topics"]) + [
                        topic for row in materials.values() if row["type"] == "rule"
                        for topic in row["topics"]
                    ]
                ),
            )
            await self.store.record(context, "p2.repair_retrieval", {
                "target": key, "query": result.retrieval_query,
                "refs": [ref_of(h.memory) for h in hits],
            })
            added = {ref_of(h.memory): dict(h.memory) for h in hits if ref_of(h.memory) not in materials}
            if not added:
                return None
            materials.update(added)
            # Include actual session changes to newly retrieved premises, not unrelated writes.
            ids = {str(r["memory_id"]) for r in added.values()}
            extra = [t for t in transitions if any(
                t[side] and str(t[side]["memory_id"]) in ids for side in ("before", "after")
            )]
            known = {fingerprint(r) for r in self.change_references(evidence)}
            evidence.extend(t for t in extra if fingerprint(self.change_references([t])[0]) not in known)
            if extra:
                await self.store.record(context, "p2.repair_evidence", {
                    "target": key, "changes": self.change_references(evidence),
                })
        else:
            return None
        (embedding,) = await self.indexer._embed(
            [self.store.config.document_prefix + result.text], context, "indexing"
        )
        async with self.store.pool.acquire() as conn, conn.transaction():
            latest = await self.current(target["memory_id"], context, conn)
            if not latest or ref_of(latest) != key or latest["status"] != "stale":
                raise ValueError("repair target changed")
            for basis in result.basis_refs:
                row = await self.store.read_ref(context.trace, basis, conn=conn)
                latest_basis = await self.current(row["memory_id"], context, conn)
                if (
                    ref_of(latest_basis) != basis
                    or row["status"] != "fresh"
                    or row["invalidated"]
                ):
                    raise ValueError("repair premise no longer usable")
            version = await conn.fetchval(
                f"SELECT max(version)+1 FROM memories WHERE {SCOPE} AND memory_id=$4",
                *context.trace.args,
                target["memory_id"],
            )
            new = await self.store._insert(
                conn,
                context.trace,
                context.session_seq,
                result.text,
                "fact",
                result.entities,
                target["topics"],
                "derived",
                target["memory_id"],
                version,
                None,
            )
            new_key = f"{new[0]}:{new[1]}"
            await conn.execute(
                f"UPDATE memories SET search_vector=to_tsvector($6::regconfig,text),memory_embedding=$7::vector WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                *context.trace.args,
                *new,
                self.store.config.text_config,
                vector_literal(embedding, self.store.config.dimensions),
            )
            await self.mutate(
                new_key,
                {
                    "status": "fresh",
                    "accepted": True,
                    "temporal_scope": "current",
                    "basis_refs": result.basis_refs,
                    "status_reason": "Repaired from current premises",
                },
                context,
                conn,
            )
            after = dict(await self.store.read_ref(context.trace, new_key, conn=conn))
        return {"operation": "repair", "before": dict(target), "after": after}
