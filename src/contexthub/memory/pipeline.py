"""Only the implemented ingest prefix. No automatic promotion or P1/P2 placeholder."""

import asyncio
from .calls import CallContext


class MemoryPipeline:
    def __init__(self, store, extractor, indexer, *, code_version):
        self.store, self.extractor, self.indexer = store, extractor, indexer
        self.code_version = code_version

    async def ingest(
        self, trace, session_seq, messages, *, timestamp=None, timeout_seconds
    ):
        context = CallContext(self.store, trace, session_seq, self.code_version)
        # Extraction and every transport retry share this total session deadline.
        async with asyncio.timeout(timeout_seconds):
            numbered = await self.store.save_session(
                trace, session_seq, timestamp, messages
            )
            items = await self.extractor.extract(numbered, context)
            refs = await self.store.save_extracted(trace, session_seq, items)
            await self.indexer.index(refs, context)
        return refs
