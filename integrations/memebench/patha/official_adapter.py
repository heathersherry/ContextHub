"""Hash-pinned official Cas/Abs semantics, separate from P2/verifier judgments."""

import ast
import json
from urllib.parse import urlsplit
from pathlib import Path
from typing import Literal
from uuid import uuid4
import httpx
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    Hash,
    Name,
    canonical_hash,
)
from contexthub.models.execution import CallIdentity
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.services.knowledge_service import safe_reason
from contexthub.propagation.candidate import ModelConfig
from .run_gates import FileIdentity, stage_preflight, validate_model_seed


class OfficialJudgment(Contract):
    record_id: Name
    label_sha256: Hash
    run_id: str
    replicate_id: int
    profile: Literal["local/base-only", "full/strong-only", "adaptive"]
    phase: Literal["before", "after"]
    question_id: Name
    answer_sha256: Hash
    state_sha256: Hash
    official_code_sha256: Hash
    prompt_sha256: Hash
    configuration_sha256: Hash
    call_id: str | None
    correct: bool | None
    reason: Name
    error: str | None


def load_official_class(identity: FileIdentity):
    """Load just the pinned class: no module imports, CLI or OpenAI constructor.

    Its actual u_check supplies the prompt; no copied/modified Abs semantics.
    The synchronous official _call is replaced with prompt capture below.
    """
    identity.verify()
    tree = ast.parse(Path(identity.path).read_text())
    cls = next(
        (x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "LLMJudge"),
        None,
    )
    if cls is None:
        raise ContractError("official_judge_class_missing")
    namespace = {"Dict": dict, "List": list, "json": json}
    exec(
        compile(ast.Module(body=[cls], type_ignores=[]), identity.path, "exec"),
        namespace,
    )
    return namespace["LLMJudge"]


def official_prompt(judge_class, *, sidecar, phase, question, answer):
    if sidecar.task_type not in ("Cas", "Abs") or phase not in ("before", "after"):
        raise ContractError("official_scope_cas_abs_only")
    values = getattr(sidecar, phase + "_entity_values")
    if not values:
        raise ContractError("official_entity_values_missing")
    # Official judge_episode uses the first entity in insertion order.
    entity = next(iter(values))
    captured = []
    judge = object.__new__(judge_class)
    judge._call = lambda prompt: captured.append(prompt) or {"correct": False}
    judge.u_check(
        question=question,
        entity=entity,
        gold_value=values[entity],
        agent_answer=answer,
        task_type=sidecar.task_type,
        phase=phase,
    )
    if len(captured) != 1:
        raise ContractError("official_prompt_contract_changed")
    return captured[0]


def classify(before, after):
    if before is None or after is None:
        return "unknown"
    if type(before) is not bool or type(after) is not bool:
        raise ContractError("official_verdict_not_boolean")
    return {
        (True, True): "real",
        (False, True): "trivial",
        (True, False): "knew_but_failed",
        (False, False): "never_knew",
    }[(before, after)]


class OfficialJudgeRunner:
    def __init__(
        self,
        config,
        *,
        official_source,
        repository,
        account_id,
        api_key,
        transport,
        mode="synthetic",
        manifest=None,
        authorization=None,
    ):
        config = ModelConfig.model_validate(config.model_dump(mode="json"))
        endpoint = urlsplit(config.base_url)
        if (
            endpoint.scheme not in ("http", "https")
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ContractError("judge_endpoint_must_be_secret_free")
        # The official prompt is unwrapped. Model, decode/retry/price parameters
        # remain explicit; the official module's example defaults are never used.
        if config.prompt_template != "{input_json}":
            raise ContractError("official_prompt_must_be_unmodified")
        if mode == "synthetic":
            if (
                not isinstance(transport, httpx.MockTransport)
                or manifest is not None
                or authorization is not None
            ):
                raise ContractError("synthetic_transport_required")
        else:
            if (
                mode not in ("development", "formal")
                or manifest is None
                or manifest.mode != mode
            ):
                raise ContractError("judge_run_identity_mismatch")
            stage_preflight(
                manifest,
                authorization,
                "judge",
                configuration_sha256=canonical_hash(config),
            )
            validate_model_seed(manifest, config)
            if (
                config.request_parameters is None
                or config.request_parameters.response_format != "json_object"
            ):
                raise ContractError("official_judge_json_object_required")
            if (config.price is None and manifest.two_episode_token_only_authorization is None) or not any(
                r == official_source for r in manifest.frozen_files
            ):
                raise ContractError("judge_price_or_official_identity_missing")
            parent = json.loads(Path(manifest.s1_contract.path).read_text())
            if (
                official_source.sha256
                != parent["official_answer"]["reference"]["judge_sha256"]
            ):
                raise ContractError("judge_must_match_s1_official_code")
        self.judge_class = load_official_class(official_source)
        self.source, self.config, self.account_id = official_source, config, account_id
        self.ledger = ExecutionLedger(repository)
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

    async def judge(
        self, *, sidecar, phase, question, answer_row, run_id, replicate_id, profile
    ):
        text = answer_row.get("answer", "")
        prompt = official_prompt(
            self.judge_class,
            sidecar=sidecar,
            phase=phase,
            question=question,
            answer=text,
        )
        identity = CallIdentity(
            account_id=self.account_id,
            execution_id=uuid4(),
            call_id=uuid4(),
            strategy="official_judge",
            stage="judge",
            operation="offline_answer_judgment",
            model_role="judge",
            code_version="patha-s8-v1",
            config_sha256=canonical_hash(self.config),
        )
        error = None
        if answer_row.get("error") or not text.strip():
            correct, reason = False, "empty_or_failed_answer"
            records, call_id = (), None
        else:
            try:
                raw = await self.client.complete(
                    prompt,
                    max_tokens=self.config.max_tokens,
                    call=RecordedCall(
                        identity, self.config.retry, self.ledger, self.config.price
                    ),
                )
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                value = json.loads(raw)
                correct = value.get("correct", False)
                if type(correct) is not bool:
                    raise ContractError("official_verdict_not_boolean")
                reason = str(value.get("reason") or "official_result")
            except Exception as exc:
                correct, error, reason = (
                    None,
                    safe_reason(exc),
                    "judge_failed_or_unknown",
                )
            records = await self.ledger.records(self.account_id, identity.call_id)
            call_id = str(identity.call_id)
        return OfficialJudgment(
            record_id=sidecar.record_id,
            label_sha256=sidecar.record_sha256,
            run_id=str(run_id),
            replicate_id=replicate_id,
            profile=profile,
            phase=phase,
            question_id=getattr(sidecar, phase + "_question_id"),
            answer_sha256=canonical_hash(answer_row),
            state_sha256=answer_row["state_sha256"],
            official_code_sha256=self.source.sha256,
            prompt_sha256=canonical_hash(prompt),
            configuration_sha256=canonical_hash(self.config),
            call_id=call_id,
            correct=correct,
            reason=reason,
            error=error,
        ), records
