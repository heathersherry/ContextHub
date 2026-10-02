"""S6 proposals: complete content and evidence, never a freshness capability."""

import asyncio
import json
from datetime import datetime, timezone
from string import Formatter
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.request_parameters import RequestParameters
from contexthub.llm.retry_policy import RecordedCall, RetryPolicy
from contexthub.models.execution import CallIdentity, PriceBasis
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    ContextVersion,
    EvidenceRef,
    Hash,
    Name,
    Nonnegative,
    Positive,
    Sealed,
    SealRef,
    canonical_hash,
)
from contexthub.models.maintenance import Candidate


class Content(Contract):
    l0_content: str | None
    l1_content: str | None
    l2_content: str | None

    def text(self):
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        )


class Claim(Contract):
    level: Literal["l0_content", "l1_content", "l2_content"]
    start: Nonnegative
    end: Positive
    conclusion: Name
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    derivation: Name


class Cause(Contract):
    event_id: UUID
    source: ContextVersion
    reason_sha256: Hash


class CauseSupport(Contract):
    cause: Cause
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    rationale: Name


class ProposalData(Contract):
    outcome: Literal["updated", "unchanged"]
    content: Content
    claims: tuple[Claim, ...] = Field(min_length=1)
    causes: tuple[CauseSupport, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def full_content(self):
        # Coverage is structural; the independent verifier checks semantic support.
        for level in ("l0_content", "l1_content", "l2_content"):
            text = getattr(self.content, level) or ""
            claims = sorted(
                (c for c in self.claims if c.level == level), key=lambda c: c.start
            )
            cursor = 0
            for claim in claims:
                if (
                    claim.start != cursor
                    or claim.end > len(text)
                    or text[claim.start : claim.end] != claim.conclusion
                ):
                    raise ContractError("incomplete_conclusion_coverage")
                cursor = claim.end
            if cursor != len(text):
                raise ContractError("incomplete_conclusion_coverage")
        if len({c.cause.event_id for c in self.causes}) != len(self.causes):
            raise ContractError("duplicate_cause_support")
        return self


class GenerationResponse(Contract):
    proposal: ProposalData | None
    unresolved_reason: Name | None

    @model_validator(mode="after")
    def result(self):
        if (self.proposal is None) != (self.unresolved_reason is not None):
            raise ContractError("candidate_or_unresolved_required")
        return self


class ActionBinding(Sealed):
    kind: Literal["maintenance_action"] = "maintenance_action"
    frame: SealRef
    receipt: SealRef
    snapshot: SealRef
    bundle: SealRef
    execution_id: UUID
    work_revision: Positive
    profile: Literal["local/base-only", "full/strong-only", "adaptive"]
    action: Literal["rule", "M1", "M2", "M3", "full/strong"]
    configuration_sha256: Hash
    original: Content
    causes: tuple[Cause, ...] = Field(min_length=1)


class Proposal(Sealed):
    """Composition preserves the byte contract of existing S2 candidates."""

    kind: Literal["maintenance_proposal"] = "maintenance_proposal"
    action: SealRef
    candidate: SealRef
    data: ProposalData
    producer: Literal["model", "rule"]
    rule_id: Name | None = None
    rule_version: Name | None = None
    rule_inputs_sha256: Hash | None = None


class ModelConfig(Contract):
    id: Name
    model: Name
    base_url: Name
    temperature: float | None = Field(allow_inf_nan=False)
    request_parameters: RequestParameters | None = None
    response_contract: Literal["references-v1", "location-ids-v2", "candidate-parts-v2", "verifier-ids-v2"] = Field(
        default="references-v1", exclude_if=lambda value: value == "references-v1"
    )
    max_tokens: Positive
    prompt_template: Name
    rules_version: Name
    retry: RetryPolicy
    price: PriceBasis | None

    @model_validator(mode="after")
    def template(self):
        if self.request_parameters is not None:
            self.request_parameters.check_temperature(self.temperature)
        fields = list(Formatter().parse(self.prompt_template))
        if [f for _, f, _, _ in fields if f is not None] != ["input_json"] or any(
            s or c for _, _, s, c in fields
        ):
            raise ContractError("template_requires_one_input_json")
        return self


def now():
    return datetime.now(timezone.utc)


def remaining(frame):
    seconds = (frame.deadline_at - now()).total_seconds()
    if seconds <= 0:
        raise ContractError("deadline_exhausted")
    return seconds


def call_identity(frame, binding, operation, config, role):
    return CallIdentity(
        account_id=frame.account_id,
        execution_id=frame.execution_id,
        call_id=uuid4(),
        event_id=frame.event_id,
        work_item_id=frame.snapshot.id,
        strategy=binding.profile,
        stage=binding.action,
        operation=operation,
        model_role=role,
        code_version="s6-v1",
        config_sha256=canonical_hash(config),
    )


class ModelCall:
    """No repository or commit capability is passed to the model transport."""

    def __init__(self, config, *, api_key, recorder, transport=None):
        self.config, self.recorder = config, recorder
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

    async def invoke(self, frame, binding, payload, parse, role):
        ident = call_identity(frame, binding, role, self.config, role)
        prompt = self.config.prompt_template.format(
            input_json=json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        # This bounds the S2 owner's attempts AND backoff, without another retry loop.
        async with asyncio.timeout(remaining(frame)):
            raw = await self.client.complete(
                prompt,
                max_tokens=self.config.max_tokens,
                call=RecordedCall(
                    identity=ident,
                    policy=self.config.retry,
                    recorder=self.recorder,
                    price=self.config.price,
                    validate_response=parse,
                ),
            )
        remaining(frame)  # late responses never become usable
        return parse(raw), ident


class CandidateGenerator(ModelCall):
    async def generate(self, frame, binding, bundle, sources):
        payload = {"binding": binding.model_dump(mode="json"),
                   "bundle": bundle.model_dump(mode="json"), "sources": sources}
        parse = GenerationResponse.model_validate_json
        if self.config.response_contract == "candidate-parts-v2":
            from .identified_wire import registry_view, expand_generation
            evidence, view = registry_view((r.evidence for r in bundle.read), "E")
            causes, cause_view = registry_view(binding.causes, "C")
            payload.update(evidence_ids=view, cause_ids=cause_view)
            parse = lambda raw: expand_generation(raw, evidence, causes)
        elif self.config.response_contract != "references-v1":
            raise ContractError("generator_response_contract_mismatch")
        response, ident = await self.invoke(frame, binding, payload, parse, "generator")
        return response, ident


class DeterministicRule(Protocol):
    id: str
    version: str

    def required_inputs(self, target: ContextVersion): ...
    def applies(self, target: ContextVersion, original: Content) -> bool: ...
    def execute(
        self,
        original: Content,
        inputs: tuple[EvidenceRef, ...],
        causes: tuple[Cause, ...],
    ) -> GenerationResponse: ...


async def seal_proposal(artifacts, db, binding, data, ident, *, rule=None, inputs=()):
    evidence = tuple(
        dict.fromkeys(e for c in (*data.claims, *data.causes) for e in c.evidence)
    )
    candidate = Candidate(
        account_id=binding.account_id,
        id=uuid4(),
        maintenance=binding.snapshot,
        bundle=binding.bundle,
        outcome=data.outcome,
        proposed_content=data.content.text(),
        evidence=evidence,
        generator_call_id=ident.call_id,
        generator_config_sha256=ident.config_sha256,
        rule_version=rule.version if rule else None,
    )
    await artifacts.seal(db, candidate)
    proposal = Proposal(
        account_id=binding.account_id,
        id=uuid4(),
        action=binding.reference(),
        candidate=candidate.reference(),
        data=data,
        producer="rule" if rule else "model",
        rule_id=rule.id if rule else None,
        rule_version=rule.version if rule else None,
        rule_inputs_sha256=canonical_hash([e.model_dump(mode="json") for e in inputs])
        if rule
        else None,
    )
    await artifacts.seal(db, proposal)
    return proposal
