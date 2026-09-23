"""One recorded extraction per immutable source batch; both search arms reuse it."""

import json
from pathlib import Path
from string import Formatter
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import Field, ValidationError, model_validator

from contexthub.llm.chat_client import OpenAIChatClient
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
    canonical_hash,
)
from contexthub.services.artifact_service import ArtifactService
from contexthub.services.evidence_service import EvidenceService
from ._work import LocalOperation, MeteredDB


class ExtractionConfig(Contract):
    id: Name
    model: Name
    base_url: Name
    temperature: float | None = Field(allow_inf_nan=False)
    max_tokens: Positive
    prompt_template: Name
    retry: RetryPolicy

    @model_validator(mode="after")
    def template(self):
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


def parse_response(raw, sources):
    result = ExtractionResponse.model_validate_json(raw)
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
    ):
        self.config, self.recorder = config, recorder
        self.artifacts = artifacts or ArtifactService()
        self.evidence = EvidenceService(self.artifacts)
        self.client = OpenAIChatClient(
            api_key,
            base_url=config.base_url,
            model=config.model,
            temperature=config.temperature,
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
                    validate_response=lambda raw: parse_response(raw, texts),
                ),
            )
            parsed = parse_response(raw, texts)
            refs = {s.id: s for s in sources}
            props = []
            for ordinal, prop in enumerate(parsed.propositions):
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
            obj = PropositionArtifact(
                **common, status="complete", propositions=tuple(props)
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
