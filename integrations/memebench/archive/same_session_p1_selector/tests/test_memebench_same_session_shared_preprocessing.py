from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from integrations.memebench.run_same_session_shared_preprocessing import (
    DEFAULT_OUT,
    FatalRunError,
    _cost_of_calls,
    _retryable,
    _stable_node_id,
    _successes,
)
from integrations.memebench.same_session_selector_intervention import build_selector_cases
from integrations.memebench.turn_candidate_mechanism import (
    generate_turn_candidates,
    verify_permutation_invariance,
)


def _node(node_id: str, text: str, session: int, turn: int) -> dict:
    return {
        "node_id": node_id,
        "text": text,
        "session_index": session,
        "original_session_id": f"s{session}",
        "original_turns": [
            {"turn_index": turn, "role": "user", "content": text},
        ],
        "candidate_snapshot_ids": [],
    }


def test_stable_node_identity_does_not_depend_on_extractor_position():
    a = _stable_node_id("ep", 1, "s1", "same fact")
    b = _stable_node_id("ep", 1, "s1", "same fact")
    assert a == b


def test_union_is_permutation_invariant_and_strict_earlier_turn():
    nodes = [
        {
            "node_id": "a",
            "text": "alpha fact",
            "session_index": 0,
            "original_session_id": "s0",
            "original_turns": [
                {"turn_index": 0, "role": "user", "content": "alpha fact"},
                {"turn_index": 1, "role": "user", "content": "beta fact"},
            ],
            "candidate_snapshot_ids": [],
        },
        {
            "node_id": "b",
            "text": "beta fact",
            "session_index": 0,
            "original_session_id": "s0",
            "original_turns": [
                {"turn_index": 0, "role": "user", "content": "alpha fact"},
                {"turn_index": 1, "role": "user", "content": "beta fact"},
            ],
            "candidate_snapshot_ids": [],
        },
    ]
    mechanism = generate_turn_candidates("ep", nodes)
    assert len(mechanism["candidates"]) == 1
    assert mechanism["candidates"][0]["source_turn_index"] == 0
    assert mechanism["candidates"][0]["target_turn_index"] == 1
    assert verify_permutation_invariance("ep", nodes)["all_identical"]


def test_gold_fields_cannot_enter_candidate_generator_interface():
    node = _node("a", "alpha fact", 0, 0)
    baseline = generate_turn_candidates("ep", [node])
    polluted = {**node, "gold_answer": "secret", "manual_source_id": "gold"}
    assert generate_turn_candidates("ep", [polluted]) == baseline


def test_selector_case_alias_dedup_and_hash_is_order_invariant():
    nodes = [_node("a", "same fact", 0, 0), _node("b", "same fact", 0, 0)]
    evidence = {
        "episode_id": "ep",
        "nodes": nodes,
        "candidate_traces": [
            {
                "node_id": row["node_id"],
                "candidate_snapshot_ids": [],
                "candidate_snapshot_size": 0,
            }
            for row in nodes
        ],
    }
    left = build_selector_cases([evidence])
    right = build_selector_cases([{**evidence, "nodes": list(reversed(nodes))}])
    assert len(left) == 1
    assert left[0]["input_hash"] == right[0]["input_hash"]


def test_resume_accepts_only_hash_bound_success(tmp_path: Path):
    shard = tmp_path / "episodes" / "ep.json"
    shard.parent.mkdir()
    shard.write_text('{"ok":true}\n')
    import hashlib

    digest = hashlib.sha256(shard.read_bytes()).hexdigest()
    (tmp_path / "episode_success.jsonl").write_text(
        json.dumps(
            {
                "episode_id": "ep",
                "config_sha256": "cfg",
                "manifest_path": "episodes/ep.json",
                "manifest_file_sha256": digest,
            }
        )
        + "\n"
    )
    config = {"expected_episode_ids": ["ep"], "config_sha256": "cfg"}
    assert set(_successes(tmp_path, config)) == {"ep"}
    shard.write_text('{"ok":false}\n')
    assert _successes(tmp_path, config) == {}


def test_retryable_and_fatal_classification():
    assert _retryable(httpx.ReadTimeout("slow"))
    response = httpx.Response(429, request=httpx.Request("POST", "https://x"))
    assert _retryable(httpx.HTTPStatusError("rate", request=response.request, response=response))
    response = httpx.Response(401, request=httpx.Request("POST", "https://x"))
    assert not _retryable(
        httpx.HTTPStatusError("auth", request=response.request, response=response)
    )
    assert not _retryable(FatalRunError("schema"))


def test_cost_is_aggregated_per_episode_and_missing_usage_is_not_zero():
    cost = _cost_of_calls(
        [
            {
                "usage": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 1_000_000,
                },
                "cost_incomplete": False,
            },
            {"usage": None, "cost_incomplete": True},
        ]
    )
    assert cost["extraction"]["known_usd"] == pytest.approx(2.0)
    assert cost["extraction"]["cost_incomplete_calls"] == 1
    assert cost["shared_preprocessing_total"]["cost_complete"] is False


def test_full_candidate_union_needs_no_embedding():
    history = _node("h", "history fact", 0, 0)
    target = _node("t", "target fact", 1, 0)
    target["candidate_snapshot_ids"] = ["h"]
    evidence = {
        "episode_id": "ep",
        "nodes": [history, target],
        "candidate_traces": [
            {
                "node_id": "h",
                "candidate_snapshot_ids": [],
                "candidate_snapshot_size": 0,
            },
            {
                "node_id": "t",
                "candidate_snapshot_ids": ["h"],
                "candidate_snapshot_size": 1,
            },
        ],
    }
    cases = build_selector_cases([evidence])
    target_case = next(row for row in cases if row["target_node_ids"] == ["t"])
    assert target_case["history_candidate_count"] == 1
    assert all("embedding" not in row for row in target_case["candidates"])


@pytest.mark.skipif(not (DEFAULT_OUT / "summary.json").is_file(), reason="formal run absent")
def test_frozen_full100_manifest_completeness_gold_isolation_and_hashes():
    import hashlib

    summary = json.loads((DEFAULT_OUT / "summary.json").read_text())
    index = json.loads((DEFAULT_OUT / "shared_manifest_index.json").read_text())
    expected = json.loads((DEFAULT_OUT / "expected_episodes.json").read_text())
    scoring = json.loads((DEFAULT_OUT / "gold_scoring_side.json").read_text())
    assert summary["completed_episode_count"] == 100
    assert summary["hop2_applicable_count"] == 64
    assert not summary["missing_episode_ids"]
    assert len(index["episodes"]) == len(expected["episodes"]) == 100
    assert sum(row["hop2_applicable"] for row in index["episodes"]) == 64
    assert {row["episode_id"] for row in index["episodes"]} == {
        row["episode_id"] for row in expected["episodes"]
    }
    scoring_ids = {
        item["gold_scoring_id"]
        for row in scoring["episodes"]
        for item in row["scoring_records"]
    }
    for entry in index["episodes"]:
        shard = DEFAULT_OUT / entry["manifest_path"]
        assert hashlib.sha256(shard.read_bytes()).hexdigest() == entry[
            "manifest_file_sha256"
        ]
        manifest = json.loads(shard.read_text())
        selector_payload = json.dumps(manifest["selector_cases"], sort_keys=True)
        assert not any(
            forbidden in selector_payload
            for forbidden in (
                "gold_scoring_id",
                "gold_answer",
                "gold_edge",
                "manual_source",
                "manual_target",
            )
        )
        assert not scoring_ids.intersection(selector_payload.split('"'))


@pytest.mark.skipif(not (DEFAULT_OUT / "summary.json").is_file(), reason="formal run absent")
def test_frozen_full100_safety_cost_and_summary_success_only():
    summary = json.loads((DEFAULT_OUT / "summary.json").read_text())
    safety = summary["safety_invariants"]
    assert safety["future_to_past_count"] == 0
    assert safety["same_turn_directed_count"] == 0
    assert safety["future_session_source_count"] == 0
    assert safety["self_loop_count"] == 0
    assert safety["directed_cycle_or_nontrivial_scc_count"] == 0
    assert safety["look_ahead_count"] == 0
    assert safety["extractor_order_dependence_count"] == 0
    assert summary["retryable_failure_count"] == 0
    assert summary["fatal_failure_count"] == 0
    costs = [
        json.loads(line)
        for line in (DEFAULT_OUT / "per_episode_cost.jsonl").read_text().splitlines()
        if line.strip()
    ]
    successes = [
        json.loads(line)
        for line in (DEFAULT_OUT / "episode_success.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(costs) == len(successes) == summary["completed_episode_count"] == 100
    assert not summary["cost_usd"]["cost_incomplete_episode_ids"]
    assert sum(
        row["shared_preprocessing_total"]["known_usd"] for row in costs
    ) == pytest.approx(summary["cost_usd"]["total"])
