"""Sparse cascade audit contracts; an omitted edge is not proven independent."""
from typing import Literal, Annotated
from uuid import UUID
from pydantic import Field, model_validator
from contexthub.models.knowledge import Contract, ContractError, EvidenceRef, Hash, Name
from contexthub.discovery.dependency_batch import BatchJudgment, EvidenceNumber
from contexthub.discovery.stage0 import Mention


class IdentityQuestion(Contract):
    left_mention: Name
    right_mention: Name
    why_changes_edge: Name


class Proposal(Contract):
    candidate_number: int = Field(strict=True, ge=1)
    judgment: BatchJudgment
    identity_questions: tuple[IdentityQuestion, ...]
    review_reason: Name | None

    @model_validator(mode='after')
    def positive_only(self):
        if self.judgment.verdict != 'DEPENDS':
            raise ContractError('cascade_proposal_must_be_positive')
        return self


class EvidenceGap(Contract):
    candidate_number: int | None = Field(ge=1, strict=True)
    missing_fact: Name
    search_terms: tuple[Name, ...] = Field(min_length=1)
    anchor_evidence_ids: tuple[EvidenceNumber, ...] = Field(min_length=1)


class SparseAnswer(Contract):
    proposals: tuple[Proposal, ...]
    gaps: tuple[EvidenceGap, ...]
    unresolved_candidate_numbers: tuple[Annotated[int, Field(strict=True, ge=1)], ...]


class CascadeTrace(Contract):
    call_id: UUID
    role: Name
    payload_sha256: Hash
    response_sha256: Hash | None
    errors: tuple[Name, ...]


class IdentityAnswer(Contract):
    verdict: Literal['SAME', 'DIFFERENT', 'UNKNOWN']
    citation_ids: tuple[EvidenceNumber, ...]
    reason: Name
    conflicts: tuple[Name, ...]


class CachedIdentity(Contract):
    key: Hash
    mentions: tuple[Mention, Mention]
    verdict: Literal['SAME', 'DIFFERENT', 'UNKNOWN']
    answer: IdentityAnswer | None
    evidence: tuple[EvidenceRef, ...]
    traces: tuple[CascadeTrace, ...]
    errors: tuple[Name, ...]
    original_generation: int


class IdentityUse(Contract):
    candidate_id: UUID
    left_mention: Name
    right_mention: Name
    reused: bool
    record: CachedIdentity


class GapAudit(Contract):
    gap: EvidenceGap
    key: Hash
    status: Name
    lead_ids: tuple[UUID, ...]


class CascadeAudit(Contract):
    version: Literal['sparse-cascade-v1'] = 'sparse-cascade-v1'
    traces: tuple[CascadeTrace, ...]
    proposed_ids: tuple[UUID, ...]
    not_selected_ids: tuple[UUID, ...]
    gaps: tuple[GapAudit, ...]
    identities: tuple[IdentityUse, ...]
