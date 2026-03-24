"""Embedding client protocol and NoOp implementation."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingClient(Protocol):
    async def embed(self, text: str) -> list[float] | None: ...


class NoOpEmbeddingClient:
    async def embed(self, text: str) -> list[float] | None:
        return None
