"""P1 binds condition sources and affected matters on each rule version."""

import json
from .contracts import RuleBindings, ref_of, split_ref
from .model_io import LocalReferences, validated_completion
from .storage import SCOPE


class ConditionBinder:
    def __init__(self, store, client, retriever, *, retry, price=None):
        self.store, self.client = store, client
        self.retriever = retriever
        self.retry, self.price = retry, price

    async def bind(self, candidates_by_ref, context):
        current = {
            r["memory_id"]: r for r in await self.store.current_rows(context.trace)
        }
        rules = await self.store.active_rules(context.trace)
        new_facts = {
            r["memory_id"]: r
            for key in candidates_by_ref
            if (r := current.get(split_ref(key)[0]))
            and ref_of(r) == key
            and r["type"] == "fact"
            and r["accepted"]
            and r["status"] != "deleted"
        }
        pending = {key for key in candidates_by_ref if key in rules}
        for fact in new_facts.values():
            for key in candidates_by_ref[ref_of(fact)]:
                if key in rules and fact["memory_id"] not in (
                    rules[key]["condition_matter_ids"]
                    + rules[key]["affected_matter_ids"]
                ):
                    pending.add(key)
        changed = []
        for key in sorted(pending):
            rule = rules[key]
            # Older versions supply candidates, never inherited conclusions.
            versions = await self.store.versions(context.trace, rule["memory_id"])
            related = {
                uid
                for r in versions
                for field in ("condition_matter_ids", "affected_matter_ids")
                for uid in r[field]
            }
            hits = candidates_by_ref.get(key, {})
            ids = {split_ref(k)[0] for k in hits} | set(new_facts) | related
            candidates = {
                ref_of(r): r
                for uid in sorted(ids, key=str)
                if (r := current.get(uid))
                and r["type"] == "fact"
                and r["accepted"]
                and r["status"] != "deleted"
            }
            if candidates:
                candidates = await self.retriever.filter_rule_candidates(
                    rule, candidates, context
                )
            if not candidates:
                missing = [
                    role for role in ("condition", "affected")
                    if not rule[role + "_matter_ids"]
                ]
                await self.store.record(
                    context,
                    "p1.unbound" if missing else "p1.skipped",
                    {
                        "rule": key, "reason": "no_candidates",
                        "missing": missing,
                    },
                )
                continue
            refs = LocalReferences(list(candidates.values()))
            inputs = {
                "rule": {"text": rule["text"]},
                "candidates": [
                    {"ref": refs.encode(k), "text": r["text"]}
                    for k, r in candidates.items()
                ],
            }
            selected = await self.select_bindings(inputs, refs, key, context)
            values = {
                field: sorted(
                    set(rule[field]) | {split_ref(r)[0] for r in selected[role]},
                    key=str,
                )
                for field, role in (
                    ("condition_matter_ids", "condition_refs"),
                    ("affected_matter_ids", "affected_refs"),
                )
            }
            async with self.store.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    f"UPDATE memories SET condition_matter_ids=$6,affected_matter_ids=$7 WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                    *context.trace.args,
                    *split_ref(key),
                    values["condition_matter_ids"],
                    values["affected_matter_ids"],
                )
                await self.store.record(
                    context,
                    "p1.output",
                    {
                        "rule": key,
                        **selected,
                        **values,
                        "before": {field: rule[field] for field in values},
                    },
                    conn=conn,
                )
                missing = [field for field, ids in values.items() if not ids]
                if missing:
                    await self.store.record(
                        context,
                        "p1.unbound",
                        {"rule": key, "missing": missing},
                        conn=conn,
                    )
            if any(set(values[field]) != set(rule[field]) for field in values):
                changed.append(key)
        return changed

    async def select_bindings(self, inputs, refs, key, context):
        rendered = (
            context.store.prompts.text("p1_conditions") + "\n" + json.dumps(inputs, ensure_ascii=False)
        )
        await self.store.record(
            context,
            "p1.input",
            {
                "prompt": rendered,
                "rule": key,
                "reference_map": refs.decode_map,
            },
        )

        def validate(raw):
            choice = RuleBindings.model_validate_json(raw)
            result = {}
            for role, selected in choice.model_dump().items():
                decoded = [refs.decode(r) for r in selected]
                if len(set(decoded)) != len(decoded):
                    raise ValueError(
                        "choose distinct supplied fact references for each role"
                    )
                result[role] = decoded
            return result

        return await validated_completion(
            self.client,
            context,
            stage="condition_binding",
            log_stage="p1",
            rendered=rendered,
            retry=self.retry,
            price=self.price,
            validate=validate,
        )
