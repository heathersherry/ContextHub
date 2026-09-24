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
from .run_gates import RunManifest, RunAuthorization, stage_preflight


class SystemConfiguration(Contract):
    mode: Literal["synthetic", "development", "formal"]
    knowledge: KnowledgeConfig
    manifest: RunManifest | None = None
    authorization: RunAuthorization | None = None

    @model_validator(mode="after")
    def identities(self):
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
        prices = (
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
        if any(price is None for price in prices):
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
