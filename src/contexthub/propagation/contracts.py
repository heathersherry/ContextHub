"""S5 preparation records. Existing S2 payloads and hashes are unchanged."""

from typing import Literal, Protocol
from uuid import UUID
from pydantic import AwareDatetime, Field, model_validator
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    ContextVersion,
    Hash,
    Name,
    Nonnegative,
    Positive,
    Sealed,
    SealRef,
)
from contexthub.llm.retry_policy import RetryPolicy


class MaintenancePolicy(Contract):
    id: Name
    page_size: Positive
    chunk_size: Positive
    content_level: Literal["l0_content", "l1_content", "l2_content"]
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    retry: RetryPolicy
    # Local DB reads share the same bounded policy; no outer retry around build.
    retry_read_codes: tuple[Literal["connection", "timeout", "serialization"], ...]


class RequiredInput(Contract):
    source: ContextVersion
    content_level: Literal["l0_content", "l1_content", "l2_content"] | None = None
    role: Literal[
        "before",
        "after",
        "target",
        "upstream",
        "provenance",
        "conflict",
        "alternative",
        "rule",
    ]
    start: Nonnegative = 0
    end: Positive | None = None
    rule_id: Name | None = None
    rule_version: Name | None = None

    @model_validator(mode="after")
    def rule_identity(self):
        if self.role == "rule" and (not self.rule_id or not self.rule_version):
            raise ContractError("registered_rule_identity_required")
        if self.end is not None and self.end <= self.start:
            raise ContractError("invalid_span")
        return self


class InputRegistration(Sealed):
    kind: Literal["maintenance_inputs"] = "maintenance_inputs"
    target_id: UUID
    inputs: tuple[RequiredInput, ...]
    # These are registered relevant inputs, not conclusions about resolution.
    unresolved_conflict_ids: tuple[UUID, ...] = ()


class MaintenanceWatch(Sealed):
    kind: Literal["maintenance_watch"] = "maintenance_watch"
    report: SealRef
    graph: SealRef
    source: ContextVersion
    seeds: tuple[ContextVersion, ...]
    affected: tuple[ContextVersion, ...]
    reasons: tuple[Name, ...]
    policy_json: str
    policy_sha256: Hash
    agent_id: Name


class SourceSlot(Contract):
    source: ContextVersion
    content_level: Literal["l0_content", "l1_content", "l2_content"]
    ordinal: Nonnegative
    ingested_at: AwareDatetime
    content_sha256: Hash
    length: Nonnegative
    current_version: Positive
    seal: SealRef


class MaintenanceFrame(Sealed):
    kind: Literal["maintenance_frame"] = "maintenance_frame"
    execution_id: UUID
    event_id: UUID
    root_event_id: UUID
    watch: SealRef
    snapshot: SealRef
    old: ContextVersion
    new: ContextVersion
    expected_upstreams: tuple[ContextVersion, ...]
    required: tuple[RequiredInput, ...]
    history: tuple[SourceSlot, ...]
    cutoff_ordinal: Nonnegative
    cutoff_at: AwareDatetime
    registrations: tuple[SealRef, ...]
    relation_sources: tuple[ContextVersion, ...]
    reasons: tuple[Name, ...]
    deadline_at: AwareDatetime


class EvidenceReceipt(Sealed):
    """Authoritative preparation manifest, including requirements without evidence."""

    kind: Literal["maintenance_receipt"] = "maintenance_receipt"
    frame: SealRef
    scope: Literal["M1", "M2"]
    required: tuple[RequiredInput, ...]
    read_ordinals: tuple[Nonnegative, ...]
    pages: tuple[Nonnegative, ...]
    chunks: tuple[tuple[Nonnegative, Nonnegative, Nonnegative], ...]
    complete: bool
    ready: bool
    reasons: tuple[Name, ...]
    bundle: SealRef | None

    @model_validator(mode="after")
    def completion(self):
        if self.complete and (
            self.read_ordinals != tuple(range(len(self.required)))
            or self.bundle is None
        ):
            raise ContractError("incomplete_manifest")
        if self.ready and (not self.complete or self.reasons):
            raise ContractError("not_ready")
        return self


# A registration interface, not an execution interface. S6 supplies rule execution.


class RuleInputProvider(Protocol):
    id: str
    version: str

    def required_inputs(self, target: ContextVersion) -> tuple[RequiredInput, ...]: ...
