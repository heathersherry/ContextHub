"""IndexerService: content summarisation + embedding hook (NoOp in Task 3)."""

from __future__ import annotations

from contexthub.generation.base import ContentGenerator, GeneratedContent
from contexthub.llm.base import EmbeddingClient


class IndexerService:
    def __init__(self, content_generator: ContentGenerator, embedding_client: EmbeddingClient):
        self._generator = content_generator
        self._embedding = embedding_client

    async def generate(
        self,
        context_type: str,
        raw_content: str,
        metadata: dict | None = None,
    ) -> GeneratedContent:
        return self._generator.generate(context_type, raw_content, metadata)

    async def embed_l0(self, text: str) -> list[float] | None:
        return await self._embedding.embed(text)
