"""Offline fact-level retrieval primitives; never authorize source reads.

Caller admits the complete universe and obtains audited, model-bound embeddings.
A shortlist is not proof of search completeness or independence. This module is
not wired into DiscoveryService until the retrieval and batch contracts are ready.
"""
from math import fsum, isfinite, sqrt
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from contexthub.discovery.search import SearchCandidate
from contexthub.discovery.cascade_contracts import CascadeAudit
from contexthub.models.knowledge import Contract, ContractError, Hash, Name, canonical_hash


class RetrievalPolicy(Contract):
    version: Literal['semantic-facts-v1'] = 'semantic-facts-v1'
    initial_candidates: int = Field(strict=True, ge=1)
    expansion_factor: int = Field(strict=True, ge=2)
    batch_size: int = Field(strict=True, ge=1)
    max_partial_rounds: int = Field(strict=True, ge=1)


class EmbeddedFact(Contract):
    candidate: SearchCandidate
    vector: tuple[float, ...]

    @model_validator(mode='after')
    def valid_vector(self):
        norm = fsum(x*x for x in self.vector)
        if not self.vector or not all(isfinite(x) for x in self.vector) or not isfinite(norm) or norm <= 0:
            raise ContractError('invalid_retrieval_vector')
        return self


class FactIndexSnapshot(Contract):
    account_id: Name
    history_sha256: Hash
    embedding_identity_sha256: Hash
    facts: tuple[EmbeddedFact, ...]

    @model_validator(mode='after')
    def consistent(self):
        if len({f.candidate.id for f in self.facts}) != len(self.facts):
            raise ContractError('duplicate_retrieval_candidate')
        if len({len(f.vector) for f in self.facts}) > 1:
            raise ContractError('retrieval_dimension_mismatch')
        if any(s.account_id != self.account_id for f in self.facts for s in f.candidate.sources):
            raise ContractError('retrieval_tenant_mismatch')
        return self


class DecisionReuse(Contract):
    candidate_id: UUID
    from_generation: int = Field(strict=True, ge=0)
    decision_sha256: Hash
    context_sha256: Hash
    disposition: Literal['retained', 'missing_at_max']


class FactSelection(Contract):
    index_sha256: Hash
    policy_sha256: Hash
    target_id: UUID
    query_sha256: Hash
    generation: int = Field(strict=True, ge=0)
    limit: int = Field(strict=True, ge=1)
    required_ids: tuple[UUID, ...]
    selected_ids: tuple[UUID, ...]
    unexamined_ids: tuple[UUID, ...]
    batches: tuple[tuple[UUID, ...], ...]
    universe_exhausted: bool
    cascade: CascadeAudit | None = Field(default=None, exclude_if=lambda v: v is None)
    # Coverage of enumeration is not semantic completeness of evidence.
    reason: Literal['initial', 'missing_evidence']
    decision_reuses: tuple[DecisionReuse, ...] = Field(default=(), exclude_if=lambda v: not v)


def select_facts(index: FactIndexSnapshot, policy: RetrievalPolicy, target_id: UUID,
                 *, required_ids: tuple[UUID, ...] = (), previous: FactSelection | None = None,
                 gap_vector: tuple[float, ...] | None = None) -> FactSelection:
    """Rank locally; missing-evidence expansion monotonically retains prior picks.

    gap_vector, when supplied, must come from the same audited embedding identity.
    No embeddings, retries, model judgments or graph mutations occur here.
    """
    by_id = {f.candidate.id: f for f in index.facts}
    if target_id not in by_id:
        raise ContractError('retrieval_target_missing')
    if len(set(required_ids)) != len(required_ids) or any(i not in by_id or i == target_id for i in required_ids):
        raise ContractError('invalid_required_candidates')
    ih, ph = canonical_hash(index), canonical_hash(policy)
    # Revalidate a serialized cursor instead of trusting selected IDs from a caller.
    if previous:
        if (previous.index_sha256 != ih or previous.policy_sha256 != ph
                or previous.target_id != target_id or set(previous.required_ids) != set(required_ids)):
            raise ContractError('retrieval_cursor_mismatch')
        eligible = set(by_id) - {target_id}
        selected, remaining = set(previous.selected_ids), set(previous.unexamined_ids)
        flattened = tuple(i for batch in previous.batches for i in batch)
        if (selected & remaining or selected | remaining != eligible
                or len(selected) != len(previous.selected_ids)
                or len(remaining) != len(previous.unexamined_ids)
                or not set(required_ids) <= selected or flattened != previous.selected_ids
                or any(not b or len(b) > policy.batch_size for b in previous.batches)
                or previous.universe_exhausted != (not remaining)):
            raise ContractError('invalid_retrieval_cursor')
        if previous.universe_exhausted:
            raise ContractError('retrieval_universe_exhausted')
    elif gap_vector is not None:
        raise ContractError('gap_requires_missing_evidence_expansion')
    query = gap_vector if gap_vector is not None else by_id[target_id].vector
    validated = EmbeddedFact(candidate=by_id[target_id].candidate, vector=query).vector
    if len(validated) != len(by_id[target_id].vector):
        raise ContractError('retrieval_dimension_mismatch')
    norm = sqrt(fsum(x*x for x in validated))

    def score(f):
        # Normalize first to avoid overflow of the product of two squared norms.
        fn = sqrt(fsum(x*x for x in f.vector))
        return fsum((a/norm)*(b/fn) for a,b in zip(validated, f.vector))

    ranked = sorted((f for f in index.facts if f.candidate.id != target_id),
                    key=lambda f: (-score(f), str(f.candidate.id)))
    limit = policy.initial_candidates if previous is None else max(
        previous.limit * policy.expansion_factor, len(previous.selected_ids) + 1)
    generation = previous.generation + 1 if previous else 0
    if generation >= policy.max_partial_rounds:
        limit = max(1, len(ranked))
    limit = min(limit, max(1, len(ranked)))
    chosen = list(previous.selected_ids) if previous else []
    for ident in sorted(required_ids, key=str):
        if ident not in chosen:
            chosen.append(ident)
    # Required facts are additional to the initial top-k, never squeezed out.
    desired = {f.candidate.id for f in ranked[:limit]}
    for f in ranked:
        ident = f.candidate.id
        if ident not in chosen and (ident in desired or len(chosen) < limit):
            chosen.append(ident)
    remaining = tuple(f.candidate.id for f in ranked if f.candidate.id not in set(chosen))
    return FactSelection(index_sha256=ih, policy_sha256=ph, target_id=target_id,
        query_sha256=canonical_hash({'vector': validated, 'embedding_identity': index.embedding_identity_sha256}),
        generation=generation, limit=limit,
        required_ids=tuple(sorted(required_ids,key=str)), selected_ids=tuple(chosen),
        unexamined_ids=remaining,
        batches=tuple(tuple(chosen[i:i+policy.batch_size]) for i in range(0,len(chosen),policy.batch_size)),
        universe_exhausted=not remaining, reason='missing_evidence' if previous else 'initial')
