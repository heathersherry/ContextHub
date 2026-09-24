"""Public Python service contracts. No benchmark fields or caller-supplied edges.

Separate artifact kinds preserve the bytes and meaning of S1–S7 receipts.
"""

from typing import Literal
from uuid import UUID
from pydantic import Field
from contexthub.models.knowledge import Sealed, SealRef, ContextVersion, Hash
from contexthub.models.knowledge_public import (
    PublicModel,
    KnowledgeIngestRequest,
    KnowledgeResult,
)
from contexthub.discovery.search import HistorySnapshot


class PrepareRequest(KnowledgeIngestRequest):
    mode: Literal["content"] = "content"
    # None starts an isolated history, rather than scanning another run's inputs.
    previous: SealRef | None = None


class DiscoverPreparedRequest(PublicModel):
    request_id: UUID
    prepared: SealRef


class FinalizeGraphRequest(PublicModel):
    request_id: UUID
    reports: tuple[SealRef, ...] = Field(min_length=1)
    prepared: tuple[SealRef, ...] = Field(min_length=1)


class AssertionChangeRequest(PublicModel):
    request_id: UUID
    content: str = Field(min_length=1)
    handoff: SealRef


class PreparedInput(Sealed):
    kind: Literal["prepared_input"] = "prepared_input"
    operation_id: UUID
    agent_id: str
    scope: str
    owner_space: str
    source_ids: tuple[ContextVersion, ...]
    extraction: SealRef
    extraction_config_sha256: Hash
    prior_extractions: tuple[SealRef, ...]
    previous: SealRef | None
    history: HistorySnapshot


class PreparedResult(PublicModel):
    result: KnowledgeResult
    prepared: SealRef | None

    @property
    def operation_id(self):
        return self.result.operation_id

    @property
    def status(self):
        return self.result.status
