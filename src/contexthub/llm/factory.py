"""Factory for embedding clients."""

from __future__ import annotations

from contexthub.config import Settings
from contexthub.llm.base import EmbeddingClient, NoOpEmbeddingClient


def create_embedding_client(settings: Settings) -> EmbeddingClient:
    return NoOpEmbeddingClient()
