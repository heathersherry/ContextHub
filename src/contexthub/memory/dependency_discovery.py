"""One model call per focus, with explicit pairs and all incident known edges."""

import json
from .contracts import Discovery, material, prompt, ref_of
from .storage import SCOPE


def edge_key(e):
    return f"{e['upstream_id']}:{e['upstream_version']}>{e['downstream_id']}:{e['downstream_version']}"


class DependencyDiscovery:
    def __init__(self, store, retriever, client, *, retry, price=None):
        self.store, self.retriever, self.client = store, retriever, client
        self.retry, self.price = retry, price

    async def discover(self, focus, session_rows, context, *, relationships=True):
        hits = await self.retriever.search(
            focus["text"],
            context,
            through_session=context.session_seq,
            purpose="p1",
            entities=focus["entities"],
            exclude_session=True,
        )
        rows = {
            ref_of(r): dict(r)
            for r in [focus, *session_rows, *[h.memory for h in hits]]
            if r["status"] != "deleted" and r["merged_into"] is None
        }
        known, edges = await self.store.related(context.trace, list(rows))
        rows.update({ref_of(r): r for r in known})
        async with self.store.pool.acquire() as conn:
            latest = await conn.fetch(
                f"SELECT DISTINCT ON(memory_id) memory_id,version FROM memories WHERE {SCOPE} AND temporal_scope='current' AND memory_id=ANY($4::uuid[]) ORDER BY memory_id,version DESC",
                *context.trace.args,
                list({r["memory_id"] for r in rows.values()}),
            )
        current_refs = {ref_of(r) for r in latest}
        focal = ref_of(focus)
        pairs = {
            f"{a}>{b}": [a, b]
            for r in rows
            if r != focal
            for a, b in ((focal, r), (r, focal))
            if rows[a]["type"] in ("fact", "rule")
            and rows[b]["type"] in ("fact", "rule")
        }
        existing = {edge_key(e): e for e in edges}
        sources = {}
        for key, row in rows.items():
            saved = await self.store.sources(
                context.trace, row["memory_id"], row["version"]
            )
            sources[key] = saved["message_refs"] if saved else {}
        inputs = dict(
            focus=focal,
            sources=sources,
            search_refs=[ref_of(h.memory) for h in hits],
            retrieval_limits=self.retriever.config.model_dump(),
            memories=[material(rows[r]) for r in sorted(rows)],
            session_refs=[ref_of(r) for r in session_rows if ref_of(r) in rows],
            pairs=pairs,
            existing_edges=existing,
            relationships=relationships,
            current_refs=sorted(current_refs),
            historical_refs=sorted(
                r
                for r, row in rows.items()
                if row["accepted"] and r not in current_refs
            ),
        )
        rendered = (
            prompt("p1") + "\n" + json.dumps(inputs, default=str, ensure_ascii=False)
        )
        import hashlib

        signature = hashlib.sha256(rendered.encode()).hexdigest()
        async with self.store.pool.acquire() as conn:
            saved = await conn.fetchval(
                f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage='p1.output' AND payload->>'signature'=$4 ORDER BY step_id DESC LIMIT 1",
                *context.trace.args,
                signature,
            )
        if saved:
            return dict(
                focus=focal,
                result=Discovery.model_validate(saved["result"]),
                pairs=pairs,
                existing=existing,
                rows=rows,
            )
        await self.store.record(
            context, "p1.input", {"prompt": rendered, "focus": focal}
        )
        call = context.make(
            "dependency_discovery", {"prompt": rendered}, self.retry, price=self.price
        )
        raw = await self.client.complete(rendered, max_tokens=None, call=call)
        result = Discovery.model_validate_json(raw)
        for field, expected in (
            (result.dependencies, pairs),
            (result.existing_edges, existing),
        ):
            keys = [e.pair_id for e in field]
            if len(set(keys)) != len(keys) or set(keys) != set(expected):
                raise ValueError(
                    "P1 must judge every supplied directed pair and known edge exactly once"
                )
        for binding in result.bindings:
            refs = [binding.content_ref, binding.target_ref, *binding.evidence_refs]
            if any(r is not None and r not in rows for r in refs):
                raise ValueError("P1 reference outside supplied material")
            if binding.relation != "unresolved" and not binding.evidence_refs:
                raise ValueError("P1 evidence reference required")
            if binding.content_ref:
                source = rows[binding.content_ref]
                if (
                    source["type"] not in ("fact", "rule")
                    or source["origin"] != "extracted"
                ):
                    raise ValueError(
                        "replacement content must be an extracted fact or rule"
                    )
                if focus["type"] != "change" and binding.content_ref != focal:
                    raise ValueError("non-change focus may only supply its own content")
                if (
                    focus["type"] == "change"
                    and binding.content_ref not in inputs["session_refs"]
                ):
                    raise ValueError("change new value must come from this session")
            if binding.target_ref == binding.content_ref and binding.relation not in (
                "unresolved",
            ):
                raise ValueError("a candidate cannot replace or duplicate itself")
            if binding.relation == "forget" and focus["type"] != "change":
                raise ValueError("forget requires a change statement")
        await self.store.record(
            context,
            "p1.output",
            {"focus": focal, "signature": signature, "result": result.model_dump()},
        )
        return dict(
            focus=focal, result=result, pairs=pairs, existing=existing, rows=rows
        )
