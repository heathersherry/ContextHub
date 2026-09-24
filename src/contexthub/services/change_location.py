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


class ChangeLocator:
    def __init__(self, config, *, api_key, recorder, transport=None):
        self.config, self.recorder = config, recorder
        self.client = OpenAIChatClient(
            api_key,
            model=config.model,
            base_url=config.base_url,
            temperature=config.temperature,
            timeout=config.retry.timeout_seconds,
            transport=transport,
        )

    async def close(self):
        await self.client.close()

    async def locate(self, raw, candidates, *, identity):
        # Inputs contain only actually stored source versions and original text.
        payload = {"change": raw, "candidates": candidates}
        parsed = None

        def validate(text):
            nonlocal parsed
            parsed = LocationResult.model_validate_json(text)
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
