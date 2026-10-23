"""Extract -> shared retrieval -> direct processing -> P1 binding -> P2 propagation."""

import asyncio
from .calls import CallContext
from .contracts import split_ref


class MemoryPipeline:
    def __init__(self, store, extractor, retriever, binder, executor, *, code_version):
        self.store, self.extractor, self.retriever = store, extractor, retriever
        self.binder, self.executor, self.code_version = binder, executor, code_version

    async def process_direct(self, batch, context):
        """P2 accepts or applies rule/content items before P1 sees stored memories."""
        accepted_refs, transitions, protected = [], [], set()
        session = await self.store.get_session(context.trace, context.session_seq)
        # Rules first, then content in extraction order. Requests never become facts.
        for index, item in enumerate(batch.items):
            focus = dict(item.__dict__, created_in_session=context.session_seq)
            focus["timestamp"] = session["timestamp"]
            candidates = await batch.resolve(index, self.store, context)
            keys, changes = await self.executor.process(
                focus, candidates, context, query_vector=batch.query_vectors[index],
            )
            if keys:
                batch.accepted[index] = keys
                accepted_refs.extend(keys)
                # Protect duplicates (including a later reconfirmation of a value
                # written earlier this session), but inspect new/update facts.
                protected.update(keys)
                protected.difference_update(
                    f"{t['after']['memory_id']}:{t['after']['version']}"
                    for t in changes
                    if t["operation"] in ("new", "update")
                    and t["after"] and t["after"]["type"] == "fact"
                )
            # Includes old values becoming unavailable without any new fact.
            transitions.extend(changes)
        return accepted_refs, transitions, protected

    async def maintain_conditions(self, transitions, protected, context, binding_refs=()):
        """Invalidate along rule endpoints, then repair only the reached matters."""
        all_transitions = list(transitions)
        wave, bindings = list(transitions), list(binding_refs)
        pending, attempted = set(), set()
        while True:
            while wave or bindings:
                rules = await self.executor.routed_rules(wave, context, bindings)
                pending.update(self.executor.changed_matters(wave))
                pending.update(uid for r in rules.values() for uid in r['affected_matter_ids'])
                wave = await self.executor.check_conditions(
                    wave, protected, context, rules=rules, binding_refs=bindings,
                )
                bindings = []
                all_transitions.extend(wave)
            # Propagate each successful repair before attempting another target.
            # Retain reached stale matters so changed evidence can awaken them again.
            for target in await self.store.current_rows(context.trace, pending):
                if target['type'] != 'fact' or target['status'] != 'stale' or not target['accepted']:
                    continue
                transition = await self.executor.repair(target, all_transitions, context, attempted)
                if transition:
                    wave = [transition]
                    all_transitions.append(transition)
                    break
            else:
                return

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
                extraction = await self.extractor.extract(numbered, context)
                await self.store.record(
                    context,
                    "extraction.ready",
                    {
                        "items": [i.__dict__ for i in extraction.items],
                        "source_session": session_seq,
                    },
                )
                batch = await self.retriever.prepare(extraction.items, context)
                # All direct decisions finish before binding or condition checks.
                accepted_refs, transitions, protected = await self.process_direct(batch, context)
                accepted = set(accepted_refs)
                # P1 stores both endpoint sets on active rule versions.
                candidates_by_ref = {}
                for index, keys in batch.accepted.items():
                    hits = await batch.resolve(index, self.store, context)
                    for key in keys:
                        candidates_by_ref.setdefault(key, {}).update(hits)
                binding_refs = await self.binder.bind(candidates_by_ref, context)
                # P2 consumes changes, not just newly created fresh facts.
                await self.maintain_conditions(transitions, protected, context, binding_refs)
                refs = [split_ref(key) for key in accepted_refs]
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
