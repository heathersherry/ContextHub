"""Answers use frozen content only; attempts are kept outside maintenance state."""

from uuid import uuid4
from urllib.parse import urlsplit
import httpx

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.services.knowledge_service import safe_reason
from contexthub.models.execution import CallIdentity
from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.propagation.candidate import ModelConfig
from .run_gates import stage_preflight, validate_model_seed
from .answer_prompt import OfficialAnswerConfig, render_answer


class AnswerRunner:
    def __init__(
        self,
        config,
        *,
        repository,
        account_id,
        api_key,
        transport,
        mode="synthetic",
        manifest=None,
        authorization=None,
    ):
        config_type = (
            OfficialAnswerConfig
            if isinstance(config, OfficialAnswerConfig)
            else ModelConfig
        )
        config = config_type.model_validate(config.model_dump(mode="json"))
        endpoint = urlsplit(config.base_url)
        if (
            endpoint.scheme not in ("http", "https")
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ContractError("answer_endpoint_must_be_secret_free")
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
                raise ContractError("answer_run_identity_mismatch")
            stage_preflight(
                manifest,
                authorization,
                "answer",
                configuration_sha256=canonical_hash(config),
            )
            if (
                isinstance(config, OfficialAnswerConfig)
                and config.official_source not in manifest.frozen_files
            ):
                raise ContractError("official_answer_source_not_frozen")
            validate_model_seed(manifest, config)
            if config.price is None and manifest.two_episode_token_only_authorization is None:
                raise ContractError("answer_price_missing")
        self.config, self.account_id = config, account_id
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

    async def answer(self, bundle, sealed_state, *, maintenance_account):
        if self.account_id == maintenance_account:
            raise ContractError("answer_ledger_account_must_be_separate")
        outputs, records = [], []
        for question in bundle.questions:
            identity = CallIdentity(
                account_id=self.account_id,
                execution_id=uuid4(),
                call_id=uuid4(),
                strategy="answer",
                stage="answer",
                operation="read_sealed_state",
                model_role="answer",
                code_version="patha-s8-v1",
                config_sha256=canonical_hash(self.config),
            )
            # No database handle, maintenance tool, task type or score label is
            # supplied to the answering model. Every question sees the same state.
            try:
                answer = await self.client.complete(
                    render_answer(
                        self.config,
                        question.text,
                        sealed_state["state"]["answer_contexts"],
                    ),
                    max_tokens=self.config.max_tokens,
                    call=RecordedCall(
                        identity, self.config.retry, self.ledger, self.config.price
                    ),
                )
                error = None
            except Exception as exc:
                answer, error = "", safe_reason(exc)
            outputs.append(
                dict(
                    question_id=question.question_id,
                    answer=answer,
                    error=error,
                    state_sha256=sealed_state["sha256"],
                    call_id=str(identity.call_id),
                )
            )
            records.extend(await self.ledger.records(self.account_id, identity.call_id))
        return outputs, tuple(records)
