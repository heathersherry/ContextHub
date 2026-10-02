"""Grounded raw-text location, separate from legacy superseded-ID detection.

A proposal is limited to one complete source replacement with exact old/new
quotes. Ambiguity and incomplete evidence cannot cause a write. This does not
claim general natural-language identification accuracy.
"""

import json
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from contexthub.discovery.extraction import Span
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.knowledge import Contract, ContractError, Name, Sealed, SealRef


class LocatedChange(Contract):
    source_id: UUID
    version: int = Field(strict=True, ge=1)
    old: Span
    replacement: Span
    identity_basis: Span
    temporal_basis: Span
    relation: Literal["same_subject_same_attribute_replacement"]


class LocationResult(Contract):
    status: Literal["located", "unrelated", "pending"]
    matches: tuple[LocatedChange, ...]
    reason: Name

    @model_validator(mode="after")
    def unique(self):
        if (self.status == "located" and len(self.matches) != 1) or (
            self.status != "located" and self.matches
        ):
            raise ContractError("change_location_not_unique")
        return self


class UnitRange(Contract):
    first_id: Name
    last_id: Name


class IdentifiedChange(Contract):
    candidate_id: Name
    replacement: UnitRange
    identity_basis: UnitRange
    temporal_basis: UnitRange
    relation: Literal["same_subject_same_attribute_replacement"]


class IdentifiedLocationResult(Contract):
    status: Literal["located", "unrelated", "pending"]
    matches: tuple[IdentifiedChange, ...]
    reason: Name


def location_view(raw, candidates):
    from contexthub.discovery.evidence_units import partition_source
    candidate_map = {f"C{i+1:06d}": c for i,c in enumerate(candidates)}
    units = {f"N{i+1:06d}": (a,b,t) for i,(a,b,t) in enumerate(partition_source(raw["text"]))}
    payload = dict(change_metadata={k:v for k,v in raw.items() if k != "text"},
                   change_units=[dict(id=k,text=v[2]) for k,v in units.items()],
                   candidates=[dict(id=k,source=v) for k,v in candidate_map.items()])
    return payload, candidate_map, units


def range_span(selection, raw, units):
    keys=list(units)
    if selection.first_id not in units or selection.last_id not in units:
        raise ContractError("unknown_change_evidence_id")
    first,last=keys.index(selection.first_id),keys.index(selection.last_id)
    if first>last:
        raise ContractError("reversed_change_evidence_range")
    start,end=units[keys[first]][0],units[keys[last]][1]
    return Span(source_id=raw["snapshot_id"],start=start,end=end,
                excerpt=raw["text"][start:end],provenance=("location-ids-v2",*keys[first:last+1]))


def expand_location(text, raw, candidate_map, units):
    data = IdentifiedLocationResult.model_validate_json(text)
    matches=[]
    for match in data.matches:
        if match.candidate_id not in candidate_map:
            raise ContractError("unknown_change_candidate_id")
        old=candidate_map[match.candidate_id]
        matches.append(LocatedChange(source_id=old["context_id"],version=old["version"],
                       old=Span(source_id=old["snapshot_id"],start=0,end=len(old["text"]),
                                excerpt=old["text"],provenance=("location-ids-v2",match.candidate_id)),
                       replacement=range_span(match.replacement, raw, units),identity_basis=range_span(match.identity_basis, raw, units),
                       temporal_basis=range_span(match.temporal_basis, raw, units),relation=match.relation))
    return LocationResult(status=data.status,matches=tuple(matches),reason=data.reason)


class ChangeLocator:
    def __init__(self, config, *, api_key, recorder, transport=None):
        self.config, self.recorder = config, recorder
        self.client = OpenAIChatClient(
            api_key,
            model=config.model,
            base_url=config.base_url,
            temperature=config.temperature,
            request_parameters=config.request_parameters,
            timeout=config.retry.timeout_seconds,
            transport=transport,
        )

    async def close(self):
        await self.client.close()

    async def locate(self, raw, candidates, *, identity):
        # Inputs contain only actually stored source versions and original text.
        payload = {"change": raw, "candidates": candidates}
        identified = self.config.response_contract == "location-ids-v2"
        if identified:
            payload, candidate_map, units = location_view(raw, candidates)
        elif self.config.response_contract != "references-v1":
            raise ContractError("locator_response_contract_mismatch")
        parsed = None

        def validate(text):
            nonlocal parsed
            parsed = (expand_location(text, raw, candidate_map, units) if identified
                      else LocationResult.model_validate_json(text))
            if parsed.status != "located":
                return
            match = parsed.matches[0]
            options = [
                c
                for c in candidates
                if c["context_id"] == str(match.source_id)
                and c["version"] == match.version
            ]
            if len(options) != 1:
                raise ContractError("change_candidate_unknown")
            old = options[0]
            for span, source in (
                (match.old, old),
                (match.replacement, raw),
                (match.identity_basis, raw),
                (match.temporal_basis, raw),
            ):
                if (
                    str(span.source_id) != source["snapshot_id"]
                    or span.end <= span.start
                    or source["text"][span.start : span.end] != span.excerpt
                ):
                    raise ContractError("change_evidence_mismatch")
            if match.old.start != 0 or match.old.end != len(old["text"]):
                raise ContractError("partial_source_replacement_not_supported")
            if match.replacement.excerpt == old["text"]:
                raise ContractError("change_has_no_new_value")
            # Duplicate raw sources cannot be disambiguated merely by UUID.
            if sum(c["text"] == old["text"] for c in candidates) != 1:
                raise ContractError("change_location_ambiguous")

        await self.client.complete(
            self.config.prompt_template.format(
                input_json=json.dumps(payload, ensure_ascii=False)
            ),
            max_tokens=self.config.max_tokens,
            call=RecordedCall(
                identity,
                self.config.retry,
                self.recorder,
                self.config.price,
                validate_response=validate,
            ),
        )
        return parsed


class ChangeLocationRecord(Sealed):
    kind: Literal["change_location"] = "change_location"
    operation_id: UUID
    sources: tuple[SealRef, ...]
    result: LocationResult
