from __future__ import annotations

import json
from dataclasses import replace

import pytest

from integrations.memebench.p1_policy_certification import (
    EpisodeResult,
    PolicyCandidate,
    _json_safe,
    clopper_pearson_upper,
    load_policy_menu,
    run_certification,
    stable_episode_split,
)
from integrations.memebench.provenance_simulation import run_simulation


def _episode(
    episode_id: str,
    *,
    miss: bool = False,
    tokens: float = 10.0,
) -> EpisodeResult:
    return EpisodeResult(
        episode_id=episode_id,
        n_gold=2,
        n_pred=2,
        n_tp=1 if miss else 2,
        precision=0.5 if miss else 1.0,
        recall=0.5 if miss else 1.0,
        cheap_tokens=tokens,
        strong_tokens=0.0,
    )


def _policy(
    name: str,
    ids: list[str],
    *,
    miss_ids: set[str] = frozenset(),
    tokens: float = 10.0,
) -> PolicyCandidate:
    return PolicyCandidate(
        policy_id=name,
        source="synthetic",
        parameters={"name": name},
        episodes=tuple(
            _episode(episode_id, miss=episode_id in miss_ids, tokens=tokens)
            for episode_id in ids
        ),
    )


def test_stable_split_is_order_independent_and_disjoint() -> None:
    ids = [f"ep-{index:03d}" for index in range(80)]
    forward = stable_episode_split(ids, seed="paper-v1", selection_fraction=0.55)
    reverse = stable_episode_split(
        reversed(ids), seed="paper-v1", selection_fraction=0.55
    )

    assert forward == reverse
    assert set(forward.selection_ids).isdisjoint(forward.certification_ids)
    assert set(forward.selection_ids) | set(forward.certification_ids) == set(ids)


def test_certification_data_cannot_change_selection_or_trigger_fallback() -> None:
    ids = [f"ep-{index:03d}" for index in range(100)]
    split = stable_episode_split(ids, seed="freeze", selection_fraction=0.5)
    cert_ids = set(split.certification_ids)
    cheap_but_bad_on_cert = _policy(
        "cheap", ids, miss_ids=cert_ids, tokens=5.0
    )
    expensive_but_good = _policy("expensive", ids, tokens=20.0)

    report = run_certification(
        [expensive_but_good, cheap_but_bad_on_cert],
        seed="freeze",
        selection_fraction=0.5,
        epsilon=0.1,
        alpha=0.05,
    )
    assert report["selected_policy_id"] == "cheap"
    assert report["decision"] == "No-Go"
    assert report["certification"]["evaluations_after_freeze"] == 1

    # Changing only certification observations must not alter which policy wins
    # selection.  It may change Go/No-Go, which is certification's sole role.
    repaired = replace(
        cheap_but_bad_on_cert,
        episodes=tuple(_episode(episode_id, tokens=5.0) for episode_id in ids),
    )
    changed_cert = run_certification(
        [expensive_but_good, repaired],
        seed="freeze",
        selection_fraction=0.5,
        epsilon=0.1,
        alpha=0.05,
    )
    assert changed_cert["selected_policy_id"] == "cheap"
    assert changed_cert["decision"] == "Go"


def test_clopper_pearson_boundaries_and_known_interior_value() -> None:
    assert clopper_pearson_upper(0, 10, 0.05) == pytest.approx(
        1 - 0.05 ** (1 / 10)
    )
    assert clopper_pearson_upper(10, 10, 0.05) == 1.0
    assert clopper_pearson_upper(0, 0, 0.05) == 1.0
    assert clopper_pearson_upper(1, 10, 0.05) == pytest.approx(
        0.3941633024, abs=1e-9
    )


def test_sweep_loader_rejects_episode_misalignment(tmp_path) -> None:
    def result(parameter: float, ids: list[str]) -> dict:
        return {
            "tau": parameter,
            "cases": [
                {
                    "episode_id": episode_id,
                    "n_gold": 1,
                    "n_pred": 1,
                    "n_tp": 1,
                    "precision": 1.0,
                    "recall": 1.0,
                    "cheap_tokens": 3,
                    "strong_tokens": 0,
                }
                for episode_id in ids
            ],
        }

    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {"results": [result(float("inf"), ["a", "b"]), result(0.2, ["a", "c"])]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="episode set mismatch"):
        load_policy_menu([path])
    assert _json_safe(float("inf")) == "Infinity"


def test_provenance_keep_all_and_prune_have_expected_directions() -> None:
    report = run_simulation(
        n_trials=600,
        n_nodes=12,
        dependency_probability=0.12,
        capture_probabilities=[1.0],
        irrelevant_read_inflations=[0.5],
        pruning_false_negative_risks=[0.0, 0.35],
        irrelevant_keep_probability=0.05,
        seed="paired-provenance",
    )
    points = report["points"]
    keep_all = next(point for point in points if point["policy"] == "keep-all")
    safe_prune = next(
        point
        for point in points
        if point["policy"] == "prune"
        and point["pruning_false_negative_risk"] == 0.0
    )
    risky_prune = next(
        point
        for point in points
        if point["policy"] == "prune"
        and point["pruning_false_negative_risk"] == 0.35
    )

    assert keep_all["capture_coverage"] == 1.0
    assert keep_all["graph_miss_rate"] == 0.0
    assert safe_prune["graph_miss_rate"] == 0.0
    assert safe_prune["precision"] > keep_all["precision"]
    assert safe_prune["edge_inflation"] < keep_all["edge_inflation"]
    assert risky_prune["graph_miss_rate"] > safe_prune["graph_miss_rate"]
