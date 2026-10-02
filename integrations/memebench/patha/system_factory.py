"""Explicit experiment configuration -> the sole core composition entry point.

Development/formal construction requires the S8 staged identity and explicit
run authorization before any clients. Synthetic construction requires MockTransport.
"""

from typing import Literal
from decimal import Decimal
import httpx
from contexthub.knowledge_config import KnowledgeConfig
from contexthub.models.knowledge import Contract, ContractError
from contexthub.services.knowledge_factory import create_knowledge_service
from pydantic import model_validator
from contexthub.models.knowledge import canonical_hash
from .run_gates import (
    RunManifest,
    RunAuthorization,
    stage_preflight,
    validate_model_seed,
)


class SystemConfiguration(Contract):
    mode: Literal["synthetic", "development", "formal"]
    knowledge: KnowledgeConfig
    manifest: RunManifest | None = None
    authorization: RunAuthorization | None = None

    @model_validator(mode="after")
    def identities(self):
        if self.mode == "formal" and self.knowledge.discovery.semantic is not None:
            raise ContractError("development_semantic_retrieval_not_formal")
        if self.mode == "formal" and self.knowledge.extraction.response_contract == "evidence-ids-v4-quarantine":
            raise ContractError("diagnostic_extraction_not_formal")
        if self.mode == "synthetic":
            if self.manifest is not None or self.authorization is not None:
                raise ContractError("synthetic_identity_cannot_be_formal")
        elif (
            self.manifest is None
            or self.authorization is None
            or self.manifest.mode != self.mode
        ):
            raise ContractError("separate_run_authorization_required")
        return self


def preflight_system(config, *, transport, stage="rq1"):
    config = SystemConfiguration.model_validate(config.model_dump(mode="json"))
    if config.mode == "formal" and config.knowledge.extraction.response_contract == "evidence-ids-v4-quarantine":
        raise ContractError("diagnostic_extraction_not_formal")
    if config.mode == "synthetic" and not isinstance(transport, httpx.MockTransport):
        raise ContractError("synthetic_transport_required")
    if config.mode != "synthetic":
        stage_preflight(
            config.manifest,
            config.authorization,
            stage,
            configuration_sha256=canonical_hash(config.knowledge),
        )
        if (
            Decimal(str(config.knowledge.discovery.search.lambda_p1))
            not in config.manifest.development.lambda_p1_grid
            or config.knowledge.profile.planner.lambda_p2
            not in config.manifest.development.lambda_p2_grid
        ):
            raise ContractError("lambda_outside_finite_development_manifest")
        for model in (
            config.knowledge.extraction,
            config.knowledge.discovery.adjudication.weak,
            config.knowledge.discovery.adjudication.strong,
            config.knowledge.base,
            config.knowledge.strong,
            config.knowledge.verifier,
            config.knowledge.change_location,
        ):
            validate_model_seed(config.manifest, model)
        prices = (
            *((config.knowledge.discovery.semantic.price,) if config.knowledge.discovery.semantic else ()),
            config.knowledge.extraction_price,
            config.knowledge.discovery.adjudication.weak_price,
            config.knowledge.discovery.adjudication.strong_price,
            *(
                model.price
                for model in (
                    config.knowledge.base,
                    config.knowledge.strong,
                    config.knowledge.verifier,
                    config.knowledge.change_location,
                )
            ),
        )
        token_only = config.manifest.two_episode_token_only_authorization is not None
        token_plans = any(p.protocol == "s4-token-only-zero-lambda-v1" for p in config.knowledge.discovery.adjudication.plans)
        token_planner = config.knowledge.profile.planner.cost_mode == "token_only_zero_lambda"
        if token_only != token_plans or token_only != token_planner:
            raise ContractError("token_only_planner_authorization_mismatch")
        if token_only and (any(price is not None for price in prices)
            or config.knowledge.discovery.adjudication.lambda_p1 != 0
            or any(p.c is not None for p in config.knowledge.discovery.adjudication.plans)):
            raise ContractError("token_only_must_not_fabricate_prices")
        if not token_only and any(price is None for price in prices):
            raise ContractError("run_price_configuration_missing")
    return config


async def create_system(
    config, *, repository, context_service, secrets, transport, rules=(), stage="rq1"
):
    config = preflight_system(config, transport=transport, stage=stage)
    return await create_knowledge_service(
        repository,
        config.knowledge,
        secrets,
        context_service=context_service,
        transport=transport,
        rules=rules,
    )
