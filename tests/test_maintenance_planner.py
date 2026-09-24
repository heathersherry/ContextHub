from decimal import Decimal
import pytest
from contexthub.propagation.planner import PlannerConfig, Estimate, choose_next
from knowledge_helpers import HASH


def planner(price="0", cost="1"):
    return PlannerConfig(
        lambda_p2=Decimal(price),
        lambda_unit="targets/currency",
        currency="TEST",
        u_max_algorithm="single-target-count-v1",
        estimates=tuple(
            Estimate(
                action=a,
                amount=None if cost is None else Decimal(cost),
                currency="TEST",
                algorithm_version="synthetic-v1",
                price_sha256=HASH,
            )
            for a in ("M2", "M3")
        ),
    )


@pytest.mark.parametrize("price,expected", [("1", "M2"), ("1.01", None), ("0", "M2")])
def test_price_equality_executes(price, expected):
    result = choose_next(
        "adaptive", "M1", legal=True, stop_reason=None, config=planner(price)
    )
    assert result.action == expected and result.u_max == 1
    assert result.margin == Decimal(1) - Decimal(price)


def test_unknown_cost_never_zero():
    result = choose_next(
        "adaptive", "M2", legal=True, stop_reason=None, config=planner(cost=None)
    )
    assert result.action is None and result.reason == "cost_unknown"


@pytest.mark.parametrize(
    "profile,action", [("local/base-only", "M1"), ("full/strong-only", "full/strong")]
)
def test_fixed_profile_not_subject_to_price_or_upgrade(profile, action):
    assert (
        choose_next(
            profile, None, legal=True, stop_reason=None, config=planner("999999")
        ).action
        == action
    )
    assert (
        choose_next(
            profile, action, legal=True, stop_reason=None, config=planner()
        ).reason
        == "profile_exhausted"
    )


def test_safety_checked_before_price():
    result = choose_next(
        "adaptive",
        "M1",
        legal=False,
        stop_reason="upstream_unresolved",
        config=planner("0"),
    )
    assert result.reason == "upstream_unresolved" and result.margin is None
