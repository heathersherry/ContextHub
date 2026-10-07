"""Sequential research maintenance. Questions and labels never enter this boundary."""

import asyncio
from .calls import CallContext
from .contracts import ref_of, Binding, Discovery


class MemoryPipeline:
    def __init__(self, store, extractor, indexer, discovery, executor, *, code_version):
        self.store, self.extractor, self.indexer = store, extractor, indexer
        self.discovery, self.executor, self.code_version = (
            discovery,
            executor,
            code_version,
        )

    async def recheck(self, candidates, session_rows, context):
        affected, accepted = set(), set()
        for ref in sorted(candidates):
            current = await self.store.read_ref(context.trace, ref)
            if current["merged_into"] is not None or current["status"] != "unresolved":
                continue
            if current["type"] == "change":
                event = await self.store.get_change(context.trace, current["memory_id"])
                if event["applied_session_seq"] is not None:
                    continue
            result = await self.discovery.discover(dict(current), session_rows, context)
            changed, new = await self.executor.apply_discoveries([result], context)
            affected |= changed
            accepted |= new
            affected |= {
                r
                for r, row in result["rows"].items()
                if row["status"] == "stale" and row["accepted"]
            }
        return affected, accepted

    async def session_materials(self, refs, context):
        async with self.store.pool.acquire() as conn:
            rows = [
                await self.executor.canonical(f"{r[0]}:{r[1]}", context, conn)
                for r in refs
            ]
        return list({ref_of(r): r for r in rows if r["status"] != "deleted"}.values())

    async def ingest(
        self, trace, session_seq, messages, *, timestamp=None, timeout_seconds=1800
    ):
        if trace.strategy_id != "local_base":
            raise ValueError("only local_base is implemented")
        context = CallContext(self.store, trace, session_seq, self.code_version)
        try:
            async with asyncio.timeout(timeout_seconds):
                numbered = await self.store.save_session(
                    trace, session_seq, timestamp, messages
                )
                items = await self.extractor.extract(numbered, context)
                refs = await self.store.save_extracted(trace, session_seq, items)
                await self.store.record(context, "extraction.saved", {"refs": refs})
                await self.indexer.index(refs, context)
                session_rows = [
                    dict(await self.store.read_ref(trace, f"{r[0]}:{r[1]}"))
                    for r in refs
                ]
                discoveries = []
                for focus in session_rows:
                    if focus["type"] not in ("fact", "rule", "change"):
                        unresolved = Binding(
                            content_ref=None,
                            target_ref=None,
                            relation="unresolved",
                            temporal_scope=None,
                            evidence_refs=[ref_of(focus)],
                            issues=["Unrecognized extracted type: " + focus["type"]],
                            reason="Type cannot be accepted",
                        )
                        discoveries.append(
                            {
                                "focus": ref_of(focus),
                                "result": Discovery(
                                    bindings=[unresolved],
                                    dependencies=[],
                                    existing_edges=[],
                                ),
                                "rows": {},
                                "pairs": {},
                                "existing": {},
                            }
                        )
                        continue
                    discoveries.append(
                        await self.discovery.discover(focus, session_rows, context)
                    )
                affected, accepted = await self.executor.apply_discoveries(
                    discoveries, context
                )
                # Related unresolved candidates may now have identity/time support.
                # Only retrieved/known candidates are revisited, never a full DB scan.
                old_unresolved = {
                    r: row
                    for d in discoveries
                    for r, row in d["rows"].items()
                    if row["status"] == "unresolved"
                    and row["created_in_session"] < session_seq
                }
                changed, new = await self.recheck(
                    old_unresolved, await self.session_materials(refs, context), context
                )
                affected |= changed
                accepted |= new
                # Previously stale conclusions are eligible only when related to this
                # batch. The repair signature prevents repeated model calls on the same evidence.
                affected |= {
                    r
                    for d in discoveries
                    for r, row in d["rows"].items()
                    if row["status"] == "stale" and row["accepted"]
                }
                attempted = set()
                while affected:
                    pending = sorted(affected)
                    affected = set()
                    repaired_any = False
                    for ref in pending:
                        new, downstream = await self.executor.repair(
                            ref, context, attempted
                        )
                        affected |= downstream
                        if new:
                            repaired_any = True
                            row = dict(await self.store.read_ref(trace, new))
                            current_session = await self.session_materials(
                                refs, context
                            )
                            current_session.append(row)
                            refresh = await self.discovery.discover(
                                row, current_session, context, relationships=False
                            )
                            await self.executor.save_edges([refresh], context)
                            unresolved = {
                                r
                                for r, v in refresh["rows"].items()
                                if v["status"] == "unresolved"
                            }
                            changed, added = await self.recheck(
                                unresolved, current_session, context
                            )
                            affected |= changed
                            accepted |= added
                    if repaired_any:
                        affected |= set(pending)
                await self.store.record(
                    context,
                    "session.complete",
                    {"refs": refs, "accepted": sorted(accepted)},
                )
                return refs
        except BaseException as exc:
            await self.store.record(
                context,
                "session.failed",
                {
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "timeout": isinstance(exc, TimeoutError),
                },
            )
            raise
