"""Model wire formats only; expand into existing strict domain contracts."""
from typing import Literal
from pydantic import Field
from contexthub.models.knowledge import Contract, ContractError, Name, Nonnegative, canonical_hash


def registry_view(values, prefix):
    registry = {}
    seen = set()
    for value in values:
        digest = canonical_hash(value)
        if digest not in seen:
            seen.add(digest)
            registry[f'{prefix}{len(registry)+1:06d}'] = value
    return registry, [dict(id=k, value=v.model_dump(mode='json')) for k,v in registry.items()]


def resolve(registry, ident):
    if ident not in registry:
        raise ContractError('undeclared_maintenance_reference_id')
    return registry[ident]


def resolve_many(registry, ids):
    if len(ids) != len(set(ids)):
        raise ContractError('duplicate_maintenance_reference_id')
    return tuple(resolve(registry, i) for i in ids)


class ContentPart(Contract):
    text: Name
    evidence_ids: tuple[Name, ...] = Field(min_length=1)
    derivation: Name


class ContentParts(Contract):
    l0_content: tuple[ContentPart, ...] | None
    l1_content: tuple[ContentPart, ...] | None
    l2_content: tuple[ContentPart, ...] | None


class IdentifiedCauseSupport(Contract):
    cause_id: Name
    evidence_ids: tuple[Name, ...] = Field(min_length=1)
    rationale: Name


class IdentifiedProposal(Contract):
    outcome: Literal['updated', 'unchanged']
    content_parts: ContentParts
    causes: tuple[IdentifiedCauseSupport, ...] = Field(min_length=1)


class IdentifiedGenerationResponse(Contract):
    proposal: IdentifiedProposal | None
    unresolved_reason: Name | None


def expand_generation(raw, evidence, causes):
    from .candidate import GenerationResponse, ProposalData, Content, Claim, CauseSupport
    data = IdentifiedGenerationResponse.model_validate_json(raw)
    if data.proposal is None:
        return GenerationResponse(proposal=None, unresolved_reason=data.unresolved_reason)
    if data.unresolved_reason is not None:
        raise ContractError('candidate_or_unresolved_required')
    prop=data.proposal
    ids=[c.cause_id for c in prop.causes]
    if len(ids)!=len(set(ids)) or set(ids)!=set(causes):
        raise ContractError('incomplete_or_duplicate_cause_support')
    content, claims = {}, []
    for level in ('l0_content','l1_content','l2_content'):
        parts=getattr(prop.content_parts,level)
        content[level]=None if parts is None else ''.join(p.text for p in parts)
        cursor=0
        for part in parts or ():
            end=cursor+len(part.text)
            claims.append(Claim(level=level,start=cursor,end=end,conclusion=part.text,
                                evidence=resolve_many(evidence,part.evidence_ids),derivation=part.derivation))
            cursor=end
    proposal=ProposalData(outcome=prop.outcome,content=Content(**content),claims=tuple(claims),
                          causes=tuple(CauseSupport(cause=resolve(causes,c.cause_id),
                                      evidence=resolve_many(evidence,c.evidence_ids),rationale=c.rationale) for c in prop.causes))
    return GenerationResponse(proposal=proposal,unresolved_reason=None)


class IdentifiedVerification(Contract):
    verdict: Literal['PASS','FAIL','UNKNOWN']
    reason: Name
    evidence_ids: tuple[Name,...]
    claim_indexes: tuple[Nonnegative,...]
    cause_ids: tuple[Name,...]


class IdentifiedVerdict(Contract):
    applicability: IdentifiedVerification
    complete_support: IdentifiedVerification
    conflicts_resolved: IdentifiedVerification


def expand_verdict(raw, evidence, causes):
    from .verifier import VerdictData, Judgment
    data=IdentifiedVerdict.model_validate_json(raw)
    return VerdictData(**{name:Judgment(verdict=item.verdict,reason=item.reason,
                          evidence=resolve_many(evidence,item.evidence_ids),claim_indexes=item.claim_indexes,
                          cause_ids=tuple(c.event_id for c in resolve_many(causes,item.cause_ids)))
                          for name in ('applicability','complete_support','conflicts_resolved')
                          for item in [getattr(data,name)]})
