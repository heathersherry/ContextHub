"""Exact reference MCKP and separable complete-plan pricing; no risk router."""

from decimal import Decimal
from itertools import product
from pydantic import Field, model_validator
from contexthub.models.knowledge import Contract, ContractError, Name


class Plan(Contract):
    id: Name
    q: Decimal = Field(allow_inf_nan=False)
    c: Decimal | None = Field(ge=0, allow_inf_nan=False)
    optional_strong: bool
    # Mandatory expansion/dual-weak/strong are part of EVERY plan's protocol.
    protocol: str = "s4-mandatory-v1"

    @model_validator(mode="after")
    def protocol_check(self):
        if self.protocol not in ("s4-mandatory-v1", "s4-token-only-zero-lambda-v1"):
            raise ContractError("unsupported_plan_protocol")
        if self.c is None and self.protocol != "s4-token-only-zero-lambda-v1":
            raise ContractError("unsupported_plan_protocol")
        return self


class Item(Contract):
    id: Name
    plans: tuple[Plan, ...]

    @model_validator(mode="after")
    def unique(self):
        if len({p.id for p in self.plans}) != len(self.plans):
            raise ContractError("duplicate_plan")
        return self


class Allocation(Contract):
    feasible: bool
    choices: tuple[tuple[str, str], ...]
    quality: Decimal | None
    cost: Decimal | None


def nonnegative(value):
    try:
        value = Decimal(str(value))
    except Exception as exc:
        raise ContractError("invalid_price_or_budget") from exc
    if not value.is_finite() or value < 0:
        raise ContractError("invalid_price_or_budget")
    return value


def choose_price(plans: tuple[Plan, ...], price) -> Plan | None:
    price = nonnegative(price)
    Item(id="validation", plans=plans)
    if any(p.c is None for p in plans):
        if price != 0 or any(p.protocol != "s4-token-only-zero-lambda-v1" for p in plans):
            raise ContractError("unknown_cost_requires_token_only_zero_lambda")
        # No monetary estimate is fabricated. Distinct frozen q selects the same
        # optional review as every finite-price lambda=0 configuration.
        if len({p.q for p in plans}) != len(plans):
            raise ContractError("unknown_cost_tie_not_resolvable")
        return min(plans, key=lambda p: (-p.q, p.id)) if plans else None
    # Objective ties prefer higher q, then higher c (upgrade at equality), then ID.
    return (
        min(plans, key=lambda p: (-(p.q - price * p.c), -p.q, -p.c, p.id))
        if plans
        else None
    )


def solve_mckp(items: tuple[Item, ...], budget) -> Allocation:
    """Exponential reference solver, intended for fixed small synthetic menus.

    Max q; ties: min total c, then lexical (item ID, plan ID). No rounding.
    Empty item universe is feasible; an empty menu is infeasible.
    """
    if any(p.c is None for i in items for p in i.plans):
        raise ContractError("mckp_requires_known_costs")
    budget = nonnegative(budget)
    if len({i.id for i in items}) != len(items):
        raise ContractError("duplicate_item")
    items = tuple(sorted(items, key=lambda i: i.id))
    best = None
    for plans in product(*(i.plans for i in items)):
        cost = sum((p.c for p in plans), Decimal(0))
        if cost > budget:
            continue
        q = sum((p.q for p in plans), Decimal(0))
        choices = tuple((i.id, p.id) for i, p in zip(items, plans))
        rank = (-q, cost, choices)
        if best is None or rank < best[0]:
            best = (rank, choices, q, cost)
    return Allocation(
        feasible=best is not None,
        choices=best[1] if best else (),
        quality=best[2] if best else None,
        cost=best[3] if best else None,
    )
