"""Explicit Chat Completions wire contract; no provider capability inference."""

from typing import Literal
from pydantic import Field, model_validator
from contexthub.models.knowledge import Contract, ContractError, Name


class SeedPolicy(Contract):
    support: Literal["unknown", "supported", "unsupported"]
    enabled: bool
    value: int | None = Field(default=None, strict=True, ge=-(2**63), lt=2**63)
    evidence_reference: Name | None = None

    @model_validator(mode="after")
    def check(self):
        if self.enabled != (self.value is not None):
            raise ContractError("seed_enable_value_mismatch")
        if self.enabled and self.support != "supported":
            raise ContractError("seed_support_not_confirmed")
        if self.support != "unknown" and self.evidence_reference is None:
            raise ContractError("seed_support_evidence_required")
        return self


class RequestParameters(Contract):
    version: Literal["chat-request-v1"] = "chat-request-v1"
    output_limit_field: Literal["max_tokens", "max_completion_tokens"]
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    )
    response_format: Literal["text", "json_object"] | None
    seed: SeedPolicy
    # This is a provider contract assertion, not inferred from an alias.
    temperature_supported: bool

    def check_temperature(self, temperature):
        if temperature is not None and not self.temperature_supported:
            raise ContractError("temperature_not_supported_by_request_contract")

    # Opt-in local audit archive; request identity binds the chosen directory.
    response_archive_directory: Name | None = Field(default=None, exclude_if=lambda v: v is None)

    # Explicit exception, hash-bound by the experiment gate; never assert support.
    disabled_unknown_seed_authorization: Name | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def archive_path(self):
        if self.response_archive_directory is not None:
            from pathlib import Path
            if not Path(self.response_archive_directory).is_absolute():
                raise ContractError("response_archive_absolute_path_required")
        return self

    def require_resolved(self):
        if (self.seed.support == "unknown" and not (
            self.disabled_unknown_seed_authorization and not self.seed.enabled
            and self.seed.value is None
        )):
            raise ContractError("provider_seed_support_unknown")

    def wire(self, limit):
        result = {} if limit is None else {self.output_limit_field: limit}
        if self.reasoning_effort is not None:
            result["reasoning_effort"] = self.reasoning_effort
        if self.response_format is not None:
            result["response_format"] = {"type": self.response_format}
        if self.seed.enabled:
            result["seed"] = self.seed.value
        return result
