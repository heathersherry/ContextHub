import pytest
from integrations.memebench.patha.bootstrap import (
    paired_cluster_bootstrap,
    paired_rate_statistic,
    conclusion,
    comparison_report,
)
from integrations.memebench.patha.run_gates import StatisticsConfiguration
from integrations.memebench.patha.scorer import score_report
from s8_helpers import score_materials


def protocol(**changes):
    return StatisticsConfiguration.model_validate(
        dict(
            repeats=1,
            aggregate="pooled_counts",
            bootstrap_seed=1729,
            bootstrap_resamples=10000,
            provider_seed_supported=False,
            provider_seed=None,
            wall_relative_tolerance=None,
            local_relative_tolerances=None,
            **changes,
        )
    )


def test_episode_cluster_retains_all_cases_arms_repeats_and_pairing():
    rows = [
        dict(
            episode_id=ep, arm=arm, replicate=rep, case=case, numerator=1, denominator=1
        )
        for ep in ("a", "b")
        for arm in ("new", "control")
        for rep in range(2)
        for case in range(3)
    ]
    seen = []

    def statistic(clusters):
        assert len(clusters) == 2
        assert all(
            len(c) == 12 and len({r["episode_id"] for r in c}) == 1 for c in clusters
        )
        seen.append(tuple(c[0]["episode_id"] for c in clusters))
        return 0

    result = paired_cluster_bootstrap(
        rows, episode_ids=["a", "b"], statistic=statistic, seed=3, resamples=10000
    )
    assert len(seen) == 10001 and ("a", "a") in seen and ("b", "b") in seen
    assert result["two_sided_95"] == [0, 0] and result["undefined_resamples"] == 0
    assert conclusion(result, "cost_lower") is False
    assert conclusion(result, "recovery_more") is False
    assert conclusion(result, "quality_noninferior") is True
    assert conclusion(result, "false_fresh_not_worse") is True


@pytest.mark.parametrize(
    "difference,rule,expected",
    [
        (-1, "cost_lower", True),
        (1, "cost_lower", False),
        (0.01, "recovery_more", True),
        (-0.05, "quality_noninferior", True),
        (-0.05001, "quality_noninferior", False),
        (0.001, "false_fresh_not_worse", False),
    ],
)
def test_statistical_directions(difference, rule, expected):
    rows = [dict(episode_id="a")]
    interval = paired_cluster_bootstrap(
        rows, episode_ids=["a"], statistic=lambda _: difference, seed=1, resamples=10000
    )
    assert conclusion(interval, rule) is expected


def test_no_drop_undefined_resamples_and_explicit_aggregation():
    rows = [
        dict(episode_id=ep, arm=arm, numerator=0, denominator=0 if ep == "empty" else 1)
        for ep in ("empty", "nonempty")
        for arm in ("a", "b")
    ]
    result = paired_cluster_bootstrap(
        rows,
        episode_ids=["empty", "nonempty"],
        statistic=paired_rate_statistic("a", "b", aggregate="pooled_counts"),
        seed=1,
        resamples=10000,
    )
    assert 0 < result["undefined_resamples"] < 10000 and result["point"] == 0
    assert (
        result["two_sided_95"] is None
        and conclusion(result, "quality_noninferior") is None
    )


@pytest.mark.asyncio
async def test_four_quality_metrics_cost_unknown_na_and_no_defaults():
    data = await score_materials()
    report = score_report(**data)
    out = comparison_report(
        report,
        [],
        episode_ids=[x.episode_id for x in data["labels"]],
        protocol=protocol(),
    )["comparisons"]
    assert all(
        name in out
        for name in ("internal_Cas", "internal_Abs", "official_Cas", "official_Abs")
    )
    assert (
        out["P1_C_billed"]["passed"] is None and out["P2_complete_cost_lower"] is None
    )
    assert out["false_fresh_Abs"]["interval"]["point"] is None
    assert out["false_fresh_Abs"]["na_raw_state_check"] is True
    assert out["recovery"]["passed"] is False
