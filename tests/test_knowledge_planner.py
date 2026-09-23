from decimal import Decimal
import random
import pytest
from contexthub.discovery.planner import Plan, Item, choose_price, solve_mckp


def test_reference_matches_independent_recursive_enumeration():
    rng = random.Random(903)
    for _ in range(60):
        items = tuple(
            Item(
                id=str(i),
                plans=tuple(
                    Plan(
                        id=str(j),
                        q=rng.randrange(8),
                        c=rng.randrange(8),
                        optional_strong=bool(j),
                    )
                    for j in range(rng.randrange(1, 4))
                ),
            )
            for i in range(rng.randrange(1, 5))
        )
        budget = rng.randrange(20)
        candidates = []

        def enumerate_rec(index, choices, q, c):
            if index == len(items):
                if c <= budget:
                    candidates.append((-q, c, tuple(choices)))
                return
            for p in items[index].plans:
                enumerate_rec(
                    index + 1, choices + [(items[index].id, p.id)], q + p.q, c + p.c
                )

        enumerate_rec(0, [], 0, 0)
        out = solve_mckp(items, budget)
        assert out.feasible == bool(candidates)
        if candidates:
            best = min(candidates)
            assert (out.quality, out.cost, out.choices) == (-best[0], best[1], best[2])
            assert len(out.choices) == len(items)


def test_budget_empty_infeasible_ties_and_equality_upgrade():
    plans = (
        Plan(id="cheap", q=1, c=1, optional_strong=False),
        Plan(id="strong", q=2, c=2, optional_strong=True),
    )
    assert choose_price(plans, 1).id == "strong"
    assert choose_price(plans, Decimal("1.01")).id == "cheap"
    assert not solve_mckp((Item(id="x", plans=plans),), 0).feasible
    assert not solve_mckp((Item(id="x", plans=()),), 100).feasible
    assert solve_mckp((), 0).feasible
    with pytest.raises(ValueError):
        solve_mckp((), float("nan"))
    with pytest.raises(ValueError):
        choose_price(plans, -1)
    with pytest.raises(ValueError):
        Item(id="x", plans=(plans[0], plans[0]))
