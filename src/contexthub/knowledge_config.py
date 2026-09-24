"""Explicit, secret-free public composition; no deployment model defaults."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from contexthub.models.knowledge import Contract, ContractError, Name
from contexthub.models.execution import PriceBasis
from contexthub.discovery.extraction import ExtractionConfig
from contexthub.discovery.service import DiscoveryConfig
from contexthub.propagation.candidate import ModelConfig
from contexthub.propagation.contracts import MaintenancePolicy
from contexthub.propagation.profiles import ProfileConfig


class RuleIdentity(Contract):
    id: Name
    version: Name


class KnowledgeConfig(Contract):
    id: Name
    code_version: Name
    extraction: ExtractionConfig
    extraction_price: PriceBasis | None
    discovery: DiscoveryConfig
    maintenance: MaintenancePolicy
    profile: ProfileConfig
    base: ModelConfig
    strong: ModelConfig
    verifier: ModelConfig
    change_location: ModelConfig
    rules: tuple[RuleIdentity, ...]
    execution_owner: Literal["manual"]
    ingest_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_composition(self):
        self.discovery.check()
        for model in (
            self.extraction,
            self.discovery.adjudication.weak,
            self.discovery.adjudication.strong,
            self.base,
            self.strong,
            self.verifier,
            self.change_location,
        ):
            endpoint = urlsplit(model.base_url)
            if (
                endpoint.scheme not in ("http", "https")
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ContractError("knowledge_endpoint_must_be_secret_free")
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ContractError("duplicate_rule")
        if (
            len(
                {
                    self.base.rules_version,
                    self.strong.rules_version,
                    self.verifier.rules_version,
                }
            )
            != 1
        ):
            raise ContractError("rules_version_mismatch")
        return self

    @classmethod
    def load(cls, path):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
