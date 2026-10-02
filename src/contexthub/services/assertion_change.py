"""Grounded updates to directly asserted carriers, leaving raw evidence intact.

This is a separate contract from S7's whole-source replacement. The service
supplies candidates from the sealed graph, never from caller-provided targets.
The model's semantic claim remains fallible; exact spans are structural checks.
"""

import json
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from contexthub.discovery.extraction import Span
from .change_location import UnitRange, location_view, range_span
from contexthub.models.knowledge import Contract, ContractError, Sealed, SealRef, Name
from contexthub.llm.retry_policy import RecordedCall


class AssertionMatch(Contract):
    carrier_id: UUID
    version: int = Field(strict=True, ge=1)
    old: Span
    original_assertion: Span
    replacement: Span
    identity_basis: Span
    temporal_basis: Span
    relation: Literal["same_subject_same_attribute_direct_assertion_replacement"]


class AssertionLocation(Contract):
    status: Literal["located", "unrelated", "pending"]
    matches: tuple[AssertionMatch, ...]
    reason: Name

    @model_validator(mode="after")
    def matches_status(self):
        if (self.status == "located") != bool(self.matches):
            raise ContractError("assertion_location_status_mismatch")
        if len({m.carrier_id for m in self.matches}) != len(self.matches):
            raise ContractError("assertion_duplicate_target")
        return self


class AssertionChangeRecord(Sealed):
    kind: Literal["assertion_change"] = "assertion_change"
    operation_id: UUID
    handoff: SealRef
    sources: tuple[SealRef, ...]
    result: AssertionLocation


def validate_location(result, raw, candidates):
    intervals = []
    for match in result.matches:
        choices = [
            c
            for c in candidates
            if c["context_id"] == str(match.carrier_id)
            and c["version"] == match.version
        ]
        if len(choices) != 1:
            raise ContractError("assertion_candidate_unknown")
        old = choices[0]
        for span, source in (
            (match.old, old),
            (match.replacement, raw),
            (match.identity_basis, raw),
            (match.temporal_basis, raw),
        ):
            if (
                str(span.source_id) != source["snapshot_id"]
                or span.end <= span.start
                or span.end > len(source["text"])
                or source["text"][span.start : span.end] != span.excerpt
            ):
                raise ContractError("assertion_evidence_mismatch")
        # Whole *carrier*, never whole session; no splice/normalization is invented.
        if match.old.start != 0 or match.old.end != len(old["text"]):
            raise ContractError("assertion_carrier_not_fully_bound")
        if not any(
            str(match.original_assertion.source_id) == e["source"]["id"]
            and (
                match.original_assertion.start,
                match.original_assertion.end,
                match.original_assertion.excerpt,
            )
            == (e["start"], e["end"], e["excerpt"])
            for e in old["assertions"]
        ):
            raise ContractError("assertion_original_evidence_mismatch")
        if match.replacement.excerpt == old["text"]:
            raise ContractError("assertion_no_new_value")
        if sum(c["text"] == old["text"] for c in candidates) != 1:
            raise ContractError("assertion_ambiguous")
        interval = (match.replacement.start, match.replacement.end)
        if any(max(interval[0], a) < min(interval[1], b) for a, b in intervals):
            raise ContractError("assertion_overlapping_replacements")
        intervals.append(interval)
    return result



class IdentifiedAssertionMatch(Contract):
    candidate_id: Name
    assertion_id: Name
    replacement: UnitRange
    identity_basis: UnitRange
    temporal_basis: UnitRange
    relation: Literal["same_subject_same_attribute_direct_assertion_replacement"]


class IdentifiedAssertionLocation(Contract):
    status: Literal["located", "unrelated", "pending"]
    matches: tuple[IdentifiedAssertionMatch, ...]
    reason: Name


def assertion_view(raw, candidates):
    payload, candidate_map, units = location_view(raw, candidates)
    assertions = {}
    for item in payload["candidates"]:
        selected=[]
        for evidence in item["source"]["assertions"]:
            ident=f"A{len(assertions)+1:06d}"
            assertions[ident]=(item["id"], evidence)
            selected.append(dict(id=ident, evidence=evidence))
        item["assertion_ids"]=selected
    payload["contract"]="direct-assertion-change-ids-v2"
    return payload, candidate_map, units, assertions


def expand_assertions(text, raw, candidate_map, units, assertions):
    data=IdentifiedAssertionLocation.model_validate_json(text)
    matches=[]
    for match in data.matches:
        if match.candidate_id not in candidate_map or match.assertion_id not in assertions:
            raise ContractError("unknown_assertion_reference_id")
        owner,evidence=assertions[match.assertion_id]
        if owner!=match.candidate_id:
            raise ContractError("assertion_reference_wrong_candidate")
        old=candidate_map[match.candidate_id]
        original=Span(source_id=evidence["source"]["id"],start=evidence["start"],end=evidence["end"],
                      excerpt=evidence["excerpt"],provenance=evidence["provenance"])
        matches.append(AssertionMatch(carrier_id=old["context_id"],version=old["version"],
            old=Span(source_id=old["snapshot_id"],start=0,end=len(old["text"]),excerpt=old["text"],
                     provenance=("location-ids-v2",match.candidate_id)),original_assertion=original,
            replacement=range_span(match.replacement,raw,units),identity_basis=range_span(match.identity_basis,raw,units),
            temporal_basis=range_span(match.temporal_basis,raw,units),relation=match.relation))
    return AssertionLocation(status=data.status,matches=tuple(matches),reason=data.reason)


async def locate_assertions(locator, raw, candidates, *, identity):
    parsed = None
    identified = locator.config.response_contract == "location-ids-v2"
    if identified:
        payload, candidate_map, units, assertions = assertion_view(raw, candidates)
    elif locator.config.response_contract != "references-v1":
        raise ContractError("locator_response_contract_mismatch")

    def validate(text):
        nonlocal parsed
        parsed = validate_location(
            expand_assertions(text, raw, candidate_map, units, assertions) if identified
            else AssertionLocation.model_validate_json(text), raw, candidates
        )

    if not identified:
        payload = {"contract": "direct-assertion-change-v1", "change": raw, "candidates": candidates}
    await locator.client.complete(
        locator.config.prompt_template.format(
            input_json=json.dumps(payload, ensure_ascii=False)
        ),
        max_tokens=locator.config.max_tokens,
        call=RecordedCall(
            identity,
            locator.config.retry,
            locator.recorder,
            locator.config.price,
            validate_response=validate,
        ),
    )
    return parsed
