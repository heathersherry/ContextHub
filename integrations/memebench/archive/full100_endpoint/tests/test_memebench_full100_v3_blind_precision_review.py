from __future__ import annotations

import copy
import json

import pytest

from integrations.memebench.full100_v3_blind_precision_review import (
    DEFAULT_DECISIONS,
    FORBIDDEN_BLIND_KEYS,
    SOURCE_RUN,
    ReviewError,
    _cluster_bootstrap,
    _protected_hashes,
    _rate_summary,
    _weighted,
    analyze,
    build_blinded_artifacts,
    validate_decisions,
    verify_source_packet,
    wilson_interval,
)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_packet_hash_counts_uniqueness_and_episode_caps() -> None:
    packet = verify_source_packet(SOURCE_RUN / "review_packet.json")
    assert packet["population_counts"] == {
        "common_unmatched": 489,
        "newly_added": 282,
        "removed_old": 164,
    }
    rows = [row for values in packet["samples"].values() for row in values]
    assert len(rows) == 90
    assert len({row["edge_id"] for row in rows}) == 90


def test_fixed_seed_blinding_is_reproducible_and_leak_free(tmp_path) -> None:
    first, first_sealed = build_blinded_artifacts(
        SOURCE_RUN / "review_packet.json", tmp_path / "first"
    )
    second, second_sealed = build_blinded_artifacts(
        SOURCE_RUN / "review_packet.json", tmp_path / "second"
    )
    assert first == second
    assert first_sealed == second_sealed
    serialized = json.dumps(first, sort_keys=True)
    for key in FORBIDDEN_BLIND_KEYS:
        assert f'"{key}"' not in serialized
    assert len({row["blind_review_id"] for row in first["rows"]}) == 90


def test_unblinding_refused_before_complete_frozen_decisions(tmp_path) -> None:
    blinded, _ = build_blinded_artifacts(
        SOURCE_RUN / "review_packet.json", tmp_path / "artifacts"
    )
    decisions = {
        "status": "draft",
        "blinded_packet_sha256": blinded["blinded_packet_sha256"],
        "decisions": [],
    }
    with pytest.raises(ReviewError, match="frozen_complete"):
        validate_decisions(blinded, decisions)


def test_decision_hash_binding_and_label_enumeration(tmp_path) -> None:
    blinded, _ = build_blinded_artifacts(
        SOURCE_RUN / "review_packet.json", tmp_path / "artifacts"
    )
    decisions = _load(DEFAULT_DECISIONS)
    rows = validate_decisions(blinded, decisions)
    assert len(rows) == 90
    assert {row["label"] for row in rows} <= {"yes", "no", "ambiguous"}
    changed = copy.deepcopy(decisions)
    changed["decisions"][0]["label"] = "invalid"
    with pytest.raises(ReviewError, match="invalid label|hash mismatch"):
        validate_decisions(blinded, changed)
    changed = copy.deepcopy(decisions)
    changed["blinded_packet_sha256"] = "wrong"
    with pytest.raises(ReviewError, match="another blinded packet"):
        validate_decisions(blinded, changed)


def test_weighted_estimator_uses_population_weights() -> None:
    left = {
        "lower_bound_ambiguous_as_no": {"point": 0.0, "wilson95": [0.0, 0.2]}
    }
    right = {
        "lower_bound_ambiguous_as_no": {"point": 1.0, "wilson95": [0.8, 1.0]}
    }
    value = _weighted(
        [(3, left), (1, right)], "lower_bound_ambiguous_as_no"
    )
    assert value["population_n"] == 4
    assert value["point"] == pytest.approx(0.25)
    assert value["conservative_weighted_wilson95"] == pytest.approx([0.2, 0.4])


def test_ambiguous_lower_and_upper_bounds() -> None:
    rows = [
        {"label": "yes", "episode_id": "a"},
        {"label": "no", "episode_id": "b"},
        {"label": "ambiguous", "episode_id": "c"},
    ]
    value = _rate_summary(rows)
    assert value["lower_bound_ambiguous_as_no"]["point"] == pytest.approx(1 / 3)
    assert value["upper_bound_ambiguous_as_yes"]["point"] == pytest.approx(2 / 3)


def test_wilson_boundary_cases_are_finite_and_bounded() -> None:
    zero = wilson_interval(0, 30)
    all_yes = wilson_interval(30, 30)
    assert zero[0] == 0.0
    assert 0.0 < zero[1] < 1.0
    assert 0.0 < all_yes[0] < 1.0
    assert all_yes[1] == 1.0
    with pytest.raises(ReviewError):
        wilson_interval(0, 0)


def test_cluster_bootstrap_is_reproducible() -> None:
    rows = [
        {"label": "yes", "episode_id": "a"},
        {"label": "no", "episode_id": "a"},
        {"label": "yes", "episode_id": "b"},
    ]
    first = _cluster_bootstrap(
        rows, ambiguous_as_yes=False, seed=7, replicates=1000
    )
    second = _cluster_bootstrap(
        rows, ambiguous_as_yes=False, seed=7, replicates=1000
    )
    assert first == second


def test_analysis_refuses_to_read_sealed_map_for_draft_decisions(
    tmp_path, monkeypatch
) -> None:
    blinded, _ = build_blinded_artifacts(
        SOURCE_RUN / "review_packet.json", tmp_path / "artifacts"
    )
    draft = {
        "status": "draft",
        "blinded_packet_sha256": blinded["blinded_packet_sha256"],
        "decisions": [],
    }
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    sealed_path = tmp_path / "must-not-be-read.json"
    with pytest.raises(ReviewError, match="frozen_complete"):
        analyze(
            tmp_path / "artifacts" / "blinded_review_packet.json",
            sealed_path,
            draft_path,
            tmp_path / "analysis",
        )


def test_frozen_source_inputs_remain_byte_identical(tmp_path) -> None:
    before = _protected_hashes()
    build_blinded_artifacts(SOURCE_RUN / "review_packet.json", tmp_path)
    after = _protected_hashes()
    assert before == after
