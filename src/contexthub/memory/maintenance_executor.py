"""Apply explicit relations atomically; repair only existing affected conclusions."""

import hashlib
import json
from collections import deque
from contextlib import asynccontextmanager
from .contracts import Repair, material, prompt, ref_of, split_ref
from .schemas import normalize_entities, vector_literal
from .storage import SCOPE


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


class MaintenanceExecutor:
    def __init__(
        self, store, retriever, indexer, discovery, client, *, retry, price=None
    ):
        self.store, self.retriever, self.indexer = store, retriever, indexer
        self.discovery, self.client, self.retry, self.price = (
            discovery,
            client,
            retry,
            price,
        )

    @asynccontextmanager
    async def transaction(self, connection=None):
        if connection is not None:
            yield connection
        else:
            async with self.store.pool.acquire() as conn, conn.transaction():
                yield conn

    async def canonical(self, ref, context, conn):
        seen = set()
        while ref not in seen:
            seen.add(ref)
            row = await self.store.read_ref(context.trace, ref, conn=conn)
            if row is None:
                raise ValueError("missing memory reference in this trajectory")
            if row["merged_into"] is None:
                return dict(row)
            ref = row["merged_into"]["ref"]
        raise ValueError("cyclic merged references")

    async def current(self, memory_id, context, conn):
        return await conn.fetchrow(
            f"""SELECT * FROM memories WHERE {SCOPE} AND memory_id=$4
            AND temporal_scope='current' AND merged_into IS NULL ORDER BY version DESC LIMIT 1""",
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
            "merged_into",
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

    async def sources(self, row, context, conn):
        source = await conn.fetchrow(
            f"SELECT * FROM memory_sources WHERE {SCOPE} AND memory_id=$4 AND version=$5",
            *context.trace.args,
            row["memory_id"],
            row["version"],
        )
        if source is None or not source["message_refs"]:
            raise ValueError("direct statement has no source references")
        for seq, refs in source["message_refs"].items():
            messages = await conn.fetchval(
                f"SELECT messages FROM session_records WHERE {SCOPE} AND session_seq=$4",
                *context.trace.args,
                int(seq),
            )
            valid = {m["message_no"] for m in messages or []}
            if not refs or any(type(n) is not int or n not in valid for n in refs):
                raise ValueError("invalid direct message reference")
        return source["message_refs"]

    async def invalidate(
        self, roots, context, conn, *, exclude_history=False, forget=False, protected=()
    ):
        affected = set()
        # Only actual invalid premises invalidate historical conclusions. A mere
        # maintenance edge says "recheck", not "this fact was false".
        if exclude_history or forget:
            provenance = deque(roots)
            visited = set()
            while provenance:
                basis = provenance.popleft()
                if basis in visited:
                    continue
                visited.add(basis)
                rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE {SCOPE} AND basis_refs @> $4::jsonb AND status<>'deleted' AND NOT invalidated",
                    *context.trace.args,
                    [basis],
                )
                for child in rows:
                    key = ref_of(child)
                    await self.mutate(
                        key,
                        {
                            "invalidated": True,
                            "status": "stale",
                            "status_reason": f"{'forgotten' if forget else 'incorrect'} premise: {basis}",
                        },
                        context,
                        conn,
                    )
                    current = await self.current(child["memory_id"], context, conn)
                    if current and ref_of(current) == key:
                        affected.add(key)
                    provenance.append(key)
        queue, seen = deque([*roots, *affected]), set()
        while queue:
            key = queue.popleft()
            if key in seen:
                continue
            seen.add(key)
            edges = await conn.fetch(
                f"SELECT * FROM dependencies WHERE {SCOPE} AND upstream_id=$4 AND upstream_version=$5 AND maintenance_active",
                *context.trace.args,
                *split_ref(key),
            )
            for e in edges:
                target = f"{e['downstream_id']}:{e['downstream_version']}"
                child = await self.store.read_ref(context.trace, target, conn=conn)
                current = await self.current(child["memory_id"], context, conn)
                if (
                    target not in protected
                    and child["status"] != "deleted"
                    and current
                    and ref_of(current) == target
                ):
                    if child["status"] != "stale":
                        await self.mutate(
                            target,
                            {
                                "status": "stale",
                                "status_reason": f"upstream changed: {key}",
                            },
                            context,
                            conn,
                        )
                    if child["accepted"]:
                        affected.add(target)
                    queue.append(target)
        return affected

    async def apply(self, binding, focus_ref, context, connection=None):
        key = fingerprint(binding.model_dump(exclude={"reason", "evidence_refs"}))
        async with self.transaction(connection) as conn:
            prior = await conn.fetchval(
                f"""SELECT payload FROM memory_steps WHERE {SCOPE}
                AND stage='p2.applied' AND payload->>'key'=$4 ORDER BY step_id DESC LIMIT 1""",
                *context.trace.args,
                key,
            )
            if prior:
                return prior["result"], set(), False
            focus = await self.store.read_ref(context.trace, focus_ref, conn=conn)
            if focus is None or focus["status"] == "deleted":
                raise ValueError("missing or forgotten focus")
            await self.sources(focus, context, conn)
            if binding.relation == "unresolved":
                source = (
                    await self.canonical(binding.content_ref, context, conn)
                    if binding.content_ref
                    else None
                )
                target = (
                    await self.canonical(binding.target_ref, context, conn)
                    if binding.target_ref
                    else None
                )
                reason = "; ".join(binding.issues)
                if (
                    target
                    and source
                    and target["memory_id"] != source["memory_id"]
                    and binding.temporal_scope == "current"
                ):
                    latest = await self.current(target["memory_id"], context, conn)
                    if (
                        not latest
                        or ref_of(latest) != ref_of(target)
                        or target["status"] == "deleted"
                    ):
                        raise ValueError("unresolved bound target is not current")
                    sources = await self.sources(source, context, conn)
                    version = await conn.fetchval(
                        f"SELECT max(version)+1 FROM memories WHERE {SCOPE} AND memory_id=$4",
                        *context.trace.args,
                        target["memory_id"],
                    )
                    new = await self.store._insert(
                        conn,
                        context.trace,
                        context.session_seq,
                        source["text"],
                        source["type"],
                        source["entities"],
                        "extracted",
                        target["memory_id"],
                        version,
                        sources,
                    )
                    unresolved = f"{new[0]}:{new[1]}"
                    await conn.execute(
                        f"UPDATE memories SET search_vector=to_tsvector($6::regconfig,text),memory_embedding=$7::vector WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                        *context.trace.args,
                        *new,
                        self.store.config.text_config,
                        source["memory_embedding"],
                    )
                    await self.mutate(
                        unresolved,
                        {"temporal_scope": "current", "status_reason": reason},
                        context,
                        conn,
                    )
                    await self.mutate(
                        binding.content_ref,
                        {"merged_into": {"ref": unresolved}, "status_reason": reason},
                        context,
                        conn,
                    )
                elif not focus["accepted"] and focus["merged_into"] is None:
                    await self.mutate(
                        focus_ref,
                        {
                            "status": "unresolved",
                            "temporal_scope": binding.temporal_scope,
                            "status_reason": reason,
                        },
                        context,
                        conn,
                    )
                return None, set(), False
            source = (
                await self.canonical(binding.content_ref, context, conn)
                if binding.content_ref
                else None
            )
            target = (
                await self.canonical(binding.target_ref, context, conn)
                if binding.target_ref
                else None
            )
            if target and target["status"] == "deleted":
                raise ValueError("cannot restore a forgotten matter")
            if source and source["type"] not in ("fact", "rule"):
                raise ValueError("fact/rule content required")
            if target and target["type"] not in ("fact", "rule"):
                raise ValueError("fact/rule target required")
            if (
                target
                and binding.relation in ("replace", "correct", "duplicate")
                and binding.temporal_scope == "current"
            ):
                latest = await self.current(target["memory_id"], context, conn)
                if latest is None or ref_of(latest) != ref_of(target):
                    raise ValueError("target is not the observed current version")
            if target and binding.relation == "correct" and not target["accepted"]:
                raise ValueError("correction requires an observed accepted target")
            affected = set()
            performed = True
            result = None
            if binding.relation == "forget":
                versions = await conn.fetch(
                    f"SELECT * FROM memories WHERE {SCOPE} AND memory_id=$4",
                    *context.trace.args,
                    target["memory_id"],
                )
                for old in versions:
                    await self.mutate(
                        ref_of(old),
                        {"status": "deleted", "status_reason": binding.reason},
                        context,
                        conn,
                    )
                affected |= await self.invalidate(
                    [ref_of(r) for r in versions], context, conn, forget=True
                )
                await conn.execute(
                    f"""UPDATE dependencies SET maintenance_active=false WHERE {SCOPE}
                    AND (upstream_id=$4 OR downstream_id=$4)""",
                    *context.trace.args,
                    target["memory_id"],
                )
                result = ref_of(target)
            else:
                sources = await self.sources(source, context, conn)
                if source["status"] == "deleted" or source["invalidated"]:
                    raise ValueError("unusable replacement content")
                if (
                    source["memory_embedding"] is None
                    or source["search_vector"] is None
                ):
                    raise ValueError("replacement must be indexed before acceptance")
                if target and source["memory_id"] == target["memory_id"]:
                    # A sibling fact/change has already merged this provisional record.
                    result = ref_of(source)
                    performed = False
                elif binding.relation == "duplicate" and not target["invalidated"]:
                    previous = (
                        await self.sources(target, context, conn)
                        if target["origin"] == "extracted"
                        else {}
                    )
                    merged = {
                        k: sorted(set(previous.get(k, [])) | set(sources.get(k, [])))
                        for k in previous.keys() | sources.keys()
                    }
                    await conn.execute(
                        """INSERT INTO memory_sources VALUES($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT(run_id,episode_id,strategy_id,memory_id,version)
                        DO UPDATE SET session_seq=EXCLUDED.session_seq,message_refs=EXCLUDED.message_refs""",
                        *context.trace.args,
                        target["memory_id"],
                        target["version"],
                        sorted(int(k) for k in merged),
                        merged,
                    )
                    await self.store.record(
                        context,
                        "p2.sources_merged",
                        {"ref": ref_of(target), "before": previous, "after": merged},
                        conn=conn,
                    )
                    result = ref_of(target)
                    await self.mutate(
                        result,
                        {
                            "status": "fresh",
                            "accepted": True,
                            "invalidated": False,
                            "status_reason": binding.reason,
                        },
                        context,
                        conn,
                    )
                elif target:
                    version = await conn.fetchval(
                        f"SELECT max(version)+1 FROM memories WHERE {SCOPE} AND memory_id=$4",
                        *context.trace.args,
                        target["memory_id"],
                    )
                    new = await self.store._insert(
                        conn,
                        context.trace,
                        context.session_seq,
                        source["text"],
                        source["type"],
                        source["entities"],
                        "extracted",
                        target["memory_id"],
                        version,
                        sources,
                    )
                    result = f"{new[0]}:{new[1]}"
                    await conn.execute(
                        f"""UPDATE memories SET search_vector=to_tsvector($6::regconfig,text),memory_embedding=$7::vector
                        WHERE {SCOPE} AND memory_id=$4 AND version=$5""",
                        *context.trace.args,
                        *new,
                        self.store.config.text_config,
                        source["memory_embedding"],
                    )
                    if binding.relation in ("replace", "correct", "duplicate"):
                        await self.mutate(
                            ref_of(target),
                            {
                                "status": "stale",
                                "invalidated": target["invalidated"]
                                or binding.relation == "correct",
                                "status_reason": binding.reason,
                            },
                            context,
                            conn,
                        )
                        affected |= await self.invalidate(
                            [ref_of(target)],
                            context,
                            conn,
                            exclude_history=binding.relation == "correct",
                        )
                    await self.mutate(
                        result,
                        {
                            "status": "fresh",
                            "accepted": True,
                            "temporal_scope": binding.temporal_scope,
                            "status_reason": binding.reason,
                        },
                        context,
                        conn,
                    )
                else:
                    result = ref_of(source)
                    await self.mutate(
                        result,
                        {
                            "status": "fresh",
                            "accepted": True,
                            "temporal_scope": binding.temporal_scope,
                            "status_reason": binding.reason,
                        },
                        context,
                        conn,
                    )
                if binding.content_ref != result:
                    await self.mutate(
                        binding.content_ref,
                        {
                            "merged_into": {"ref": result},
                            "status": "unresolved",
                            "accepted": False,
                            "status_reason": "merged into " + result,
                        },
                        context,
                        conn,
                    )
            await self.store.record(
                context,
                "p2.applied",
                {
                    "key": key,
                    "focus": focus_ref,
                    "binding": binding.model_dump(),
                    "result": result,
                    "affected": sorted(affected),
                },
                conn=conn,
            )
            return result, affected, performed

    async def save_edges(
        self, discoveries, context, *, reviews_only=False, connection=None
    ):
        async with self.transaction(connection) as conn:
            judgments = {}
            for d in discoveries:
                for j in d["result"].existing_edges:
                    # An explicit release wins over an unrelated focus retaining an edge.
                    judgments[j.pair_id] = judgments.get(j.pair_id, True) and j.yes
            for key, active in judgments.items():
                up, down = key.split(">")
                previous = await conn.fetchrow(
                    f"""SELECT * FROM dependencies WHERE {SCOPE}
                    AND upstream_id=$4 AND upstream_version=$5 AND downstream_id=$6 AND downstream_version=$7""",
                    *context.trace.args,
                    *split_ref(up),
                    *split_ref(down),
                )
                # Inactive historical edges are never automatically reactivated.
                if previous and previous["maintenance_active"] and not active:
                    await conn.execute(
                        f"""UPDATE dependencies SET maintenance_active=false WHERE {SCOPE}
                        AND upstream_id=$4 AND upstream_version=$5 AND downstream_id=$6 AND downstream_version=$7""",
                        *context.trace.args,
                        *split_ref(up),
                        *split_ref(down),
                    )
                    await self.store.record(
                        context,
                        "p2.edge_change",
                        {"before": dict(previous), "maintenance_active": False},
                        conn=conn,
                    )
            if reviews_only:
                return
            for d in discoveries:
                for j in d["result"].dependencies:
                    if not j.yes or judgments.get(j.pair_id) is False:
                        continue
                    a, b = d["pairs"][j.pair_id]
                    up, down = (
                        await self.canonical(a, context, conn),
                        await self.canonical(b, context, conn),
                    )
                    if up["memory_id"] == down["memory_id"] or any(
                        r["status"] == "deleted" or r["merged_into"] is not None
                        for r in (up, down)
                    ):
                        continue
                    # Version relations need an actual judgment for that content;
                    # only provisional-to-canonical merges preserve identical text.
                    current = [
                        await self.current(r["memory_id"], context, conn)
                        for r in (up, down)
                    ]
                    active = all(
                        c
                        and ref_of(c) == ref_of(r)
                        and r["temporal_scope"] == "current"
                        for c, r in zip(current, (up, down))
                    )
                    await conn.execute(
                        """INSERT INTO dependencies(run_id,episode_id,strategy_id,upstream_id,upstream_version,
                        downstream_id,downstream_version,edge_status,reason,maintenance_active) VALUES($1,$2,$3,$4,$5,$6,$7,'confirmed',$8,$9)
                        ON CONFLICT(run_id,episode_id,strategy_id,upstream_id,upstream_version,downstream_id,downstream_version)
                        DO UPDATE SET reason=EXCLUDED.reason,maintenance_active=EXCLUDED.maintenance_active""",
                        *context.trace.args,
                        up["memory_id"],
                        up["version"],
                        down["memory_id"],
                        down["version"],
                        j.reason,
                        active,
                    )
                    await self.store.record(
                        context,
                        "p2.edge_saved",
                        {
                            "upstream": ref_of(up),
                            "downstream": ref_of(down),
                            "active": active,
                            "reason": j.reason,
                        },
                        conn=conn,
                    )
            # Historical edges remain stored but cannot affect newer content.
            retired = await conn.fetch(
                f"""UPDATE dependencies d SET maintenance_active=false WHERE {SCOPE} AND maintenance_active
                AND (EXISTS(SELECT 1 FROM memories m WHERE m.run_id=d.run_id AND m.episode_id=d.episode_id AND m.strategy_id=d.strategy_id
                  AND m.memory_id=d.upstream_id AND m.temporal_scope='current' AND m.version>d.upstream_version)
                OR EXISTS(SELECT 1 FROM memories m WHERE m.run_id=d.run_id AND m.episode_id=d.episode_id AND m.strategy_id=d.strategy_id
                  AND m.memory_id=d.downstream_id AND m.temporal_scope='current' AND m.version>d.downstream_version)) RETURNING d.*""",
                *context.trace.args,
            )
            for edge in retired:
                after = dict(edge)
                await self.store.record(
                    context,
                    "p2.edge_change",
                    {"before": dict(after, maintenance_active=True), "after": after},
                    conn=conn,
                )

    async def apply_discoveries(self, discoveries, context):
        # One direct-maintenance batch: updates, releases, propagation and change
        # completion commit together. Model requests and repairs stay outside.
        async with self.store.pool.acquire() as conn, conn.transaction():
            return await self._apply_discoveries(discoveries, context, conn)

    async def _apply_discoveries(self, discoveries, context, connection):
        await self.save_edges(
            discoveries, context, reviews_only=True, connection=connection
        )
        affected, accepted, changed_refs = set(), set(), set()
        # If a newly extracted value targets another new value, process that value first.
        pending = [(d, b) for d in discoveries for b in d["result"].bindings]
        while pending:
            progress = False
            remaining = []
            for d, b in pending:
                prerequisites = {
                    x.content_ref
                    for _, x in pending
                    if x.content_ref and x.relation != "unresolved"
                }
                if b.target_ref in prerequisites and b.target_ref != b.content_ref:
                    target = await self.store.read_ref(
                        context.trace, b.target_ref, conn=connection
                    )
                    if not target["accepted"] and target["merged_into"] is None:
                        remaining.append((d, b))
                        continue
                result, changed, performed = await self.apply(
                    b, d["focus"], context, connection
                )
                affected |= changed
                if result and b.relation != "forget":
                    accepted.add(result)
                    if (
                        performed
                        and b.relation in ("new", "replace", "correct")
                        and b.temporal_scope == "current"
                    ):
                        changed_refs.add(result)
                progress = True
            if not progress:
                raise ValueError("cyclic or unresolved same-session target ordering")
            pending = remaining
        await self.save_edges(discoveries, context, connection=connection)
        # Newly discovered constraints can affect existing conclusions even when
        # no old edge existed before this session. Direct new statements prevail.
        affected |= await self.invalidate(
            changed_refs, context, connection, protected=accepted
        )
        for d in discoveries:
            focus = await self.store.read_ref(
                context.trace, d["focus"], conn=connection
            )
            if focus["type"] != "change":
                continue
            bindings = d["result"].bindings
            operations = {
                "forget" if b.relation == "forget" else "update"
                for b in bindings
                if b.relation != "unresolved"
            }
            async with self.transaction(connection) as conn:
                before = await conn.fetchrow(
                    f"SELECT * FROM change_events WHERE {SCOPE} AND change_id=$4",
                    *context.trace.args,
                    focus["memory_id"],
                )
                targets = [
                    {
                        "memory_id": str(split_ref(b.target_ref)[0]),
                        "version": split_ref(b.target_ref)[1],
                    }
                    for b in bindings
                    if b.target_ref
                ]
                await conn.execute(
                    f"""UPDATE change_events SET operation=$5,target_refs=$6,applied_session_seq=$7 WHERE {SCOPE} AND change_id=$4""",
                    *context.trace.args,
                    focus["memory_id"],
                    next(iter(operations)) if len(operations) == 1 else None,
                    targets,
                    context.session_seq
                    if all(b.relation != "unresolved" for b in bindings)
                    else None,
                )
                await self.store.record(
                    context,
                    "p2.change_result",
                    {
                        "before": dict(before),
                        "bindings": [b.model_dump() for b in bindings],
                        "complete": all(b.relation != "unresolved" for b in bindings),
                    },
                    conn=conn,
                )
        return affected, accepted

    async def repair(self, target_ref, context, attempted):
        target = await self.store.read_ref(context.trace, target_ref)
        if (
            target is None
            or target["status"] != "stale"
            or not target["accepted"]
            or target["merged_into"] is not None
        ):
            return None, set()
        async with self.store.pool.acquire() as conn:
            current = await self.current(target["memory_id"], context, conn)
        if not current or ref_of(current) != target_ref:
            return None, set()
        related, _ = await self.store.related(context.trace, [target_ref])
        hits = await self.retriever.search(
            target["text"],
            context,
            through_session=context.session_seq,
            purpose="p2",
            entities=target["entities"],
        )
        materials = {}
        async with self.store.pool.acquire() as conn:
            for r in [*related, *[h.memory for h in hits]]:
                current = await self.current(r["memory_id"], context, conn)
                if (
                    current
                    and ref_of(current) == ref_of(r)
                    and r["status"] == "fresh"
                    and r["accepted"]
                    and not r["invalidated"]
                    and r["memory_id"] != target["memory_id"]
                    and r["type"] in ("fact", "rule")
                ):
                    materials[ref_of(r)] = r
        signature = fingerprint(
            {
                "target": material(target),
                "materials": [material(materials[r]) for r in sorted(materials)],
            }
        )
        if signature in attempted:
            return None, set()
        attempted.add(signature)
        inputs = {
            "target": material(target),
            "materials": [material(r) for r in materials.values()],
        }
        rendered = (
            prompt("p2") + "\n" + json.dumps(inputs, default=str, ensure_ascii=False)
        )
        await self.store.record(
            context, "p2.repair_input", {"signature": signature, "prompt": rendered}
        )
        # Persist signatures across sessions; irrelevant sessions do not repeat the call.
        async with self.store.pool.acquire() as conn:
            previous = await conn.fetchval(
                f"""SELECT 1 FROM memory_steps WHERE {SCOPE} AND stage='p2.repair_result'
                AND payload->>'signature'=$4 LIMIT 1""",
                *context.trace.args,
                signature,
            )
        if previous:
            return None, set()
        raw = await self.client.complete(
            rendered,
            max_tokens=None,
            call=context.make(
                "repair", {"prompt": rendered}, self.retry, price=self.price
            ),
        )
        result = Repair.model_validate_json(raw)
        if result.target_ref != target_ref or any(
            r not in materials for r in result.basis_refs
        ):
            raise ValueError("repair target or premise not in supplied material")
        await self.store.record(
            context,
            "p2.repair_result",
            {"signature": signature, "result": result.model_dump()},
        )
        if not result.can_repair:
            return None, set()
        if not any(
            materials[r]["type"] == "rule" for r in result.basis_refs
        ) or not any(materials[r]["type"] == "fact" for r in result.basis_refs):
            raise ValueError("repair requires explicit rule and fact premises")
        (embedding,) = await self.indexer._embed(
            [self.store.config.document_prefix + result.text], context, "indexing"
        )
        async with self.store.pool.acquire() as conn, conn.transaction():
            check = await self.current(target["memory_id"], context, conn)
            if not check or ref_of(check) != target_ref or check["status"] != "stale":
                raise ValueError("repair target changed")
            for basis in result.basis_refs:
                row = await self.store.read_ref(context.trace, basis, conn=conn)
                current = await self.current(row["memory_id"], context, conn)
                if (
                    not current
                    or ref_of(current) != basis
                    or row["status"] != "fresh"
                    or not row["accepted"]
                    or row["invalidated"]
                ):
                    raise ValueError("repair premise no longer valid")
            version = await conn.fetchval(
                f"SELECT max(version)+1 FROM memories WHERE {SCOPE} AND memory_id=$4",
                *context.trace.args,
                target["memory_id"],
            )
            ref = await self.store._insert(
                conn,
                context.trace,
                context.session_seq,
                result.text,
                target["type"],
                normalize_entities(result.entities),
                "derived",
                target["memory_id"],
                version,
                None,
            )
            key = f"{ref[0]}:{ref[1]}"
            await conn.execute(
                f"""UPDATE memories SET search_vector=to_tsvector($6::regconfig,text),memory_embedding=$7::vector
                WHERE {SCOPE} AND memory_id=$4 AND version=$5""",
                *context.trace.args,
                *ref,
                self.store.config.text_config,
                vector_literal(embedding, self.store.config.dimensions),
            )
            await self.mutate(
                key,
                {
                    "status": "fresh",
                    "accepted": True,
                    "temporal_scope": "current",
                    "basis_refs": sorted(set(result.basis_refs)),
                    "status_reason": result.reason,
                },
                context,
                conn,
            )
            affected = await self.invalidate([target_ref], context, conn)
            await self.store.record(
                context,
                "p2.repaired",
                {"old": target_ref, "new": key, "basis_refs": result.basis_refs},
                conn=conn,
            )
        return key, affected
