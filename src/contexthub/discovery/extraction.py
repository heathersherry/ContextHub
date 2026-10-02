"""One recorded extraction per immutable source batch; both search arms reuse it."""

import json
from pathlib import Path
from string import Formatter
from uuid import UUID, uuid4, uuid5
from typing import Literal

import httpx
from pydantic import Field, ValidationError, model_validator

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.request_parameters import RequestParameters
from contexthub.llm.retry_policy import RecordedCall, RetryPolicy
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    Name,
    Nonnegative,
    Positive,
    SealRef,
    SourceSnapshot,
    Proposition,
    PropositionArtifact,
    UnresolvedProposition,
    canonical_hash,
)
from contexthub.services.artifact_service import ArtifactService
from contexthub.services.evidence_service import EvidenceService
from ._work import LocalOperation, MeteredDB
from .evidence_units import evidence_unit_view, UNIT_VERSION


class ExtractionConfig(Contract):
    id: Name
    model: Name
    base_url: Name
    temperature: float | None = Field(allow_inf_nan=False)
    request_parameters: RequestParameters | None = None
    response_contract: Literal["offsets-v1", "unique-quote-v2", "evidence-ids-v3", "evidence-ids-v4-quarantine"] = Field(
        default="offsets-v1", exclude_if=lambda v: v == "offsets-v1"
    )
    max_tokens: Positive
    prompt_template: Name
    retry: RetryPolicy

    @model_validator(mode="after")
    def template(self):
        if self.request_parameters is not None:
            self.request_parameters.check_temperature(self.temperature)
        fields = [
            field
            for _, field, _, _ in Formatter().parse(self.prompt_template)
            if field is not None
        ]
        if fields != ["sources_json"]:
            raise ContractError("template_requires_one_sources_json")
        # Reject conversions/specifications that could alter the source payload.
        if any(
            spec or conv for _, _, spec, conv in Formatter().parse(self.prompt_template)
        ):
            raise ContractError("template_format_not_allowed")
        return self

    @classmethod
    def load(cls, path: str | Path):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class Span(Contract):
    source_id: UUID
    start: Nonnegative
    end: Positive
    excerpt: Name
    provenance: tuple[Name, ...] = Field(min_length=1)


class ExtractedProposition(Contract):
    # No normalization, digit stripping, semantic deduplication or clue filtering.
    text: Name
    evidence: tuple[Span, ...] = Field(min_length=1)


class ExtractionResponse(Contract):
    propositions: tuple[ExtractedProposition, ...]


class QuoteSpan(Contract):
    source_id: UUID
    excerpt: Name
    provenance: tuple[Name, ...] = Field(min_length=1)


class QuotedProposition(Contract):
    text: Name
    evidence: tuple[QuoteSpan, ...] = Field(min_length=1)


class QuoteExtractionResponse(Contract):
    propositions: tuple[QuotedProposition, ...]


class IdentifiedProposition(Contract):
    text: Name
    evidence_ids: tuple[Name, ...] = Field(min_length=1)


class EvidenceIdExtractionResponse(Contract):
    propositions: tuple[IdentifiedProposition, ...]


class PartialExtractionResponse(ExtractionResponse):
    unresolved: tuple[UnresolvedProposition, ...] = ()
    proposition_ordinals: tuple[int, ...] = ()


def parse_response(raw, sources, *, response_contract="offsets-v1"):
    if response_contract in ("evidence-ids-v3", "evidence-ids-v4-quarantine"):
        selected = EvidenceIdExtractionResponse.model_validate_json(raw)
        _, registry = evidence_unit_view(sources)
        propositions = []
        unresolved, ordinals = [], []
        for ordinal, prop in enumerate(selected.propositions):
            if len(set(prop.evidence_ids)) != len(prop.evidence_ids):
                raise ContractError("duplicate_evidence_id")
            unknown = tuple(ident for ident in prop.evidence_ids if ident not in registry)
            if unknown:
                if response_contract != "evidence-ids-v4-quarantine":
                    raise ContractError("undeclared_evidence_id")
                unresolved.append(UnresolvedProposition(ordinal=ordinal, text=prop.text,
                                  evidence_ids=prop.evidence_ids, unknown_evidence_ids=unknown))
                continue  # mixed valid/invalid IDs quarantine the entire proposition
            ordinals.append(ordinal)
            propositions.append(ExtractedProposition(
                text=prop.text,
                evidence=tuple(Span(**registry[ident]) for ident in prop.evidence_ids),
            ))
        result = (PartialExtractionResponse(propositions=tuple(propositions),
                  unresolved=tuple(unresolved), proposition_ordinals=tuple(ordinals))
                  if response_contract == "evidence-ids-v4-quarantine"
                  else ExtractionResponse(propositions=tuple(propositions)))
    elif response_contract == "unique-quote-v2":
        quoted = QuoteExtractionResponse.model_validate_json(raw)
        propositions = []
        for prop in quoted.propositions:
            spans = []
            for quote in prop.evidence:
                if quote.source_id not in sources:
                    raise ContractError("undeclared_source")
                text = sources[quote.source_id]
                start = text.find(quote.excerpt)
                if start < 0:
                    raise ContractError("source_quote_not_found")
                # Count overlapping occurrences too. Never normalize or fuzzy-match.
                if text.find(quote.excerpt, start + 1) >= 0:
                    raise ContractError("source_quote_ambiguous")
                spans.append(Span(source_id=quote.source_id, start=start,
                                  end=start + len(quote.excerpt), excerpt=quote.excerpt,
                                  provenance=quote.provenance))
            propositions.append(ExtractedProposition(text=prop.text, evidence=tuple(spans)))
        result = ExtractionResponse(propositions=tuple(propositions))
    elif response_contract == "offsets-v1":
        result = ExtractionResponse.model_validate_json(raw)
    else:
        raise ContractError("unsupported_extraction_response_contract")
    for prop in result.propositions:
        for span in prop.evidence:
            if span.source_id not in sources:
                raise ContractError("undeclared_source")
            text = sources[span.source_id]
            if (
                span.end <= span.start
                or span.end > len(text)
                or text[span.start : span.end] != span.excerpt
            ):
                raise ContractError("source_span_mismatch")
    return result


def error_code(exc):
    if isinstance(exc, ContractError):
        return exc.code
    if isinstance(exc, ValidationError):
        return "response_schema_invalid"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_" + str(exc.response.status_code)
    return type(exc).__name__


class ExtractionService:
    def __init__(
        self,
        config: ExtractionConfig,
        *,
        api_key: str,
        recorder,
        transport=None,
        artifacts=None,
        price=None,
    ):
        self.config, self.recorder = config, recorder
        self.price = price
        self.artifacts = artifacts or ArtifactService()
        self.evidence = EvidenceService(self.artifacts)
        self.client = OpenAIChatClient(
            api_key,
            base_url=config.base_url,
            model=config.model,
            temperature=config.temperature,
            request_parameters=config.request_parameters,
            timeout=config.retry.timeout_seconds,
            transport=transport,
        )

    async def close(self):
        await self.client.close()

    async def extract(
        self,
        db,
        sources: tuple[SealRef, ...],
        *,
        identity,
        artifact_id: UUID | None = None,
    ) -> PropositionArtifact:
        if not sources or len({s.id for s in sources}) != len(sources):
            raise ContractError("invalid_source_batch")
        if any(
            s.account_id != identity.account_id or s.kind != "source" for s in sources
        ):
            raise ContractError("invalid_source_identity")
        if identity.config_sha256 != canonical_hash(self.config):
            raise ContractError("extraction_config_mismatch")
        local_identity = identity.model_copy(
            update={
                "call_id": uuid5(identity.call_id, "extraction-local"),
                "operation": "extract_local",
                "model_role": "local",
            }
        )
        work = LocalOperation(
            self.recorder,
            local_identity,
            {"sources": [s.model_dump(mode="json") for s in sources]},
        )
        await work.start()
        db = MeteredDB(db, work)
        result_id = artifact_id or uuid4()
        common = dict(
            account_id=identity.account_id,
            id=result_id,
            sources=sources,
            extraction_config_id=self.config.id,
            extraction_config_sha256=canonical_hash(self.config),
        )
        try:
            texts, payload = {}, []
            for ref in sources:
                obj = await self.artifacts.get(db, ref)
                if not isinstance(obj, SourceSnapshot):
                    raise ContractError("reference_kind_mismatch")
                text = await self.evidence.read_source(db, obj)
                texts[ref.id] = text
                payload.append({"snapshot": obj.model_dump(mode="json"), "text": text})
            if self.config.response_contract in ("evidence-ids-v3", "evidence-ids-v4-quarantine"):
                units, _ = evidence_unit_view(texts)
                payload = [dict(snapshot=item["snapshot"],
                                evidence_unit_version=UNIT_VERSION,
                                evidence_units=units[UUID(item["snapshot"]["id"])] )
                           for item in payload]
            if self.config.response_contract == "evidence-ids-v4-quarantine":
                # Fresh exhaustive allowlist from THIS request, never a static range.
                payload = {"allowed_evidence_ids": [u["id"] for source in payload
                           for u in source["evidence_units"]], "sources": payload}
            prompt = self.config.prompt_template.format(
                sources_json=json.dumps(payload, ensure_ascii=False)
            )
            raw = await self.client.complete(
                prompt,
                max_tokens=self.config.max_tokens,
                call=RecordedCall(
                    identity=identity,
                    policy=self.config.retry,
                    recorder=self.recorder,
                    price=self.price,
                    validate_response=lambda raw: parse_response(raw, texts, response_contract=self.config.response_contract),
                ),
            )
            parsed = parse_response(raw, texts, response_contract=self.config.response_contract)
            refs = {s.id: s for s in sources}
            props = []
            ordinals = (parsed.proposition_ordinals if isinstance(parsed, PartialExtractionResponse)
                        else tuple(range(len(parsed.propositions))))
            for ordinal, prop in zip(ordinals, parsed.propositions, strict=True):
                evidence = []
                for span in prop.evidence:
                    evidence.append(
                        await self.evidence.reference(
                            db,
                            refs[span.source_id],
                            start=span.start,
                            end=span.end,
                            provenance=span.provenance,
                        )
                    )
                    work.add("hard_checks")
                props.append(
                    Proposition(
                        id=uuid5(result_id, str(ordinal)),
                        text=prop.text,
                        evidence=tuple(evidence),
                    )
                )
            unresolved = parsed.unresolved if isinstance(parsed, PartialExtractionResponse) else ()
            obj = PropositionArtifact(
                **common, status="partial" if unresolved else "complete", propositions=tuple(props),
                unresolved=unresolved, failure_reason="unresolved_evidence_ids" if unresolved else None,
            )
            await self.artifacts.seal(db, obj)
        except Exception as exc:
            # Persist the attempted source identities, including missing source seals.
            # No enclosing retry and no partial result presented as complete.
            obj = PropositionArtifact(
                **common, status="pending", failure_reason=error_code(exc)
            )
            try:
                await self.artifacts.seal(db, obj)
            except Exception as storage_error:
                await work.finish(
                    {"artifact_id": str(result_id)}, error_code(storage_error)
                )
                raise
        await work.finish(obj, obj.failure_reason)
        return obj
