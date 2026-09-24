"""Public allowlists and immutable operation receipts; never commit capabilities."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from contexthub.models.knowledge import Sealed, SealRef, ContextVersion


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceVersion(PublicModel):
    context_id: UUID
    version: int = Field(ge=1)


class KnowledgeIngestRequest(PublicModel):
    request_id: UUID
    content: str = Field(min_length=1)
    scope: Literal["agent", "team"] = "agent"
    team: str | None = None
    mode: Literal["content", "change"] = "content"
    provenance: tuple[SourceVersion, ...] = ()

    @model_validator(mode="after")
    def scope_owner(self):
        if (self.scope == "team") != bool(self.team):
            raise ValueError("team is required only for team scope")
        return self


class KnowledgeMaintainRequest(PublicModel):
    request_id: UUID
    event_id: UUID
    graph_id: UUID


class KnowledgeNode(PublicModel):
    context_id: UUID
    uri: str
    version: int
    fresh: bool


class KnowledgeWork(PublicModel):
    work_id: UUID
    frame_id: UUID
    target_id: UUID
    status: Literal["pending", "running", "updated", "unchanged", "unresolved"]
    reason: str | None = None
    proof_id: UUID | None = None
    current_version: int
    fresh: bool


class KnowledgeResult(PublicModel):
    operation_id: UUID
    request_id: UUID
    execution_id: UUID
    config_sha256: str
    operation: Literal["ingest", "maintain"]
    status: Literal["running", "complete", "pending", "error"]
    reason: str | None = None
    content_saved: bool = False
    graph_status: Literal["pending", "deliverable"] = "pending"
    maintenance_ready: bool = False
    source: KnowledgeNode | None = None
    graph_id: UUID | None = None
    report_id: UUID | None = None
    watch_ids: tuple[UUID, ...] = ()
    event_ids: tuple[UUID, ...] = ()
    event_delivery: str | None = None
    cutoff_at: datetime | None = None
    nodes: tuple[KnowledgeNode, ...] = ()
    works: tuple[KnowledgeWork, ...] = ()
    location_status: Literal["not_requested", "located", "unrelated", "pending"] = (
        "not_requested"
    )


class PublicOperation(Sealed):
    kind: Literal["public_operation"] = "public_operation"
    # Internal reservation ownership, not a public commit capability. Legacy
    # records remain readable; a new request cannot adopt an old reservation.
    owner_call_id: UUID | None = None
    agent_id: str
    request_sha256: str
    configuration_sha256: str
    operation: Literal["ingest", "maintain"]
    request_id: UUID
    source_ids: tuple[ContextVersion, ...]
    execution_id: UUID
    scope: str
    owner_space: str
    event_id: UUID | None = None
    report: SealRef | None = None


class PublicCompletion(Sealed):
    kind: Literal["public_completion"] = "public_completion"
    operation_id: UUID
    result: KnowledgeResult
    extraction: SealRef | None = None
    report: SealRef | None = None
    watches: tuple[SealRef, ...] = ()
