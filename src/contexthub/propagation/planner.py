"""Finite action selection; optimistic target count, never success probability."""

from decimal import Decimal
from typing import Literal
from pydantic import Field, model_validator
from contexthub.models.knowledge import Contract, ContractError, Name, Positive

Profile = Literal["local/base-only", "full/strong-only", "adaptive"]
Action = Literal["M1", "M2", "M3", "full/strong"]


class Estimate(Contract):
    action: Literal["M2", "M3"]
    amount: Decimal | None = Field(ge=0, allow_inf_nan=False)
    currency: Name
    algorithm_version: Name
    price_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class PlannerConfig(Contract):
    lambda_p2: Decimal = Field(ge=0, allow_inf_nan=False)
    lambda_unit: Literal["targets/currency"]
    currency: Name
    u_max_algorithm: Literal["single-target-count-v1"]
    estimates: tuple[Estimate, ...]

    @model_validator(mode="after")
    def estimates_unique(self):
        if len({e.action for e in self.estimates}) != len(self.estimates) or any(
            e.currency != self.currency for e in self.estimates
        ):
            raise ContractError("invalid_cost_estimates")
        return self


class Decision(Contract):
    action: Action | None
    reason: Name
    u_max: Positive | None = None
    estimate: Estimate | None = None
    lambda_p2: Decimal | None = None
    margin: Decimal | None = None


def choose_next(
    profile: Profile,
    previous: str | None,
    *,
    legal: bool,
    stop_reason: str | None,
    config: PlannerConfig,
):
    if not legal:
        return Decision(action=None, reason=stop_reason or "no_legal_action")
    paths = {
        "local/base-only": ("M1",),
        "full/strong-only": ("full/strong",),
        "adaptive": ("M1", "M2", "M3"),
    }
    if profile not in paths:
        raise ContractError("unknown_profile")
    path = paths[profile]
    if previous is None or previous == "rule":
        action = path[0]
    elif previous not in path:
        raise ContractError("profile_action_mismatch")
    elif path.index(previous) + 1 == len(path):
        return Decision(action=None, reason="profile_exhausted")
    else:
        action = path[path.index(previous) + 1]
    if profile != "adaptive" or action == "M1":
        return Decision(action=action, reason="fixed_action")
    estimate = next((e for e in config.estimates if e.action == action), None)
    if estimate is None or estimate.amount is None:
        return Decision(
            action=None,
            reason="cost_unknown",
            estimate=estimate,
            lambda_p2=config.lambda_p2,
            u_max=1,
        )
    margin = Decimal(1) - config.lambda_p2 * estimate.amount
    return Decision(
        action=action if margin >= 0 else None,
        reason="price_admitted" if margin >= 0 else "price_stopped",
        u_max=1,
        estimate=estimate,
        lambda_p2=config.lambda_p2,
        margin=margin,
    )
