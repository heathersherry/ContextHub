"""Shared application memory runtime; owns no benchmark answers or scoring."""

from .calls import CallContext
from .context import ContextBuilder
from .public import MemoryIngestResult
from contexthub.errors import ConflictError
from .storage import SCOPE


class MemoryEngine:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.store = pipeline.store
        self.contexts = ContextBuilder(self.store, pipeline.retriever)
        self.code_version = pipeline.code_version
        self._resources = None

    async def close(self):
        if self._resources is not None:
            await self._resources.aclose()

    async def ingest(
        self, scope, sequence, messages, *, timestamp=None, timeout_seconds=1800
    ):
        async with self.store.lease(scope):
            latest = await self.store.latest_session(scope)
            if sequence != latest + 1:
                raise ConflictError(f"expected_memory_sequence:{latest + 1}")
            await self._require_complete(scope, latest)
            refs = await self.pipeline.ingest(
                scope,
                sequence,
                messages,
                timestamp=timestamp,
                timeout_seconds=timeout_seconds,
            )
            return MemoryIngestResult(
                namespace=scope.namespace,
                sequence=sequence,
                memory_refs=[f"{uid}:{version}" for uid, version in refs],
            )

    async def context(self, scope, query, *, mode="auto", max_chars=None):
        async with self.store.lease(scope):
            sequence = await self.store.latest_session(scope)
            await self._require_complete(scope, sequence)
            context = CallContext(self.store, scope, sequence, self.code_version)
            selection = await self.contexts.select(
                query, context, mode=mode, max_chars=max_chars
            )
            return await self.contexts.result(selection, context)

    async def _require_complete(self, scope, sequence):
        if not sequence:
            return
        # Also detects process termination, where no failure event was written.
        async with self.store.pool.acquire() as conn:
            complete = await conn.fetchval(
                f"SELECT 1 FROM memory_steps WHERE {SCOPE} AND session_seq=$4 AND stage='session.complete'",
                *scope.args,
                sequence,
            )
        if not complete:
            raise ConflictError("memory_ingestion_incomplete")
