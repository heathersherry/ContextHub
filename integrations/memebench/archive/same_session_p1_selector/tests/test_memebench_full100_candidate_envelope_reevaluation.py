from __future__ import annotations

import copy
import time

from integrations.memebench.gold_edge_audit import canonical_sha256
from integrations.memebench.run_full100_candidate_envelope_reevaluation import (
    STALE_SECONDS,
    _distribution,
    _valid_success,
    build_runtime_cases,
    checkpoint_stale,
    prompt_hash,
    reuse_decision,
)


def _case() -> dict:
    candidates = [
        {
            "evidence_id": "source",
            "text": "source text",
            "node_ids": ["source-node"],
            "source_origin": "history_snapshot",
            "session_index": 0,
            "turn_index": 1,
        }
    ]
    selector = {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "target_text": "target text",
        "target_node_ids": ["target-node"],
        "target_session_index": 1,
        "target_turn_index": 1,
        "candidates": candidates,
        "history_candidate_count": 1,
        "same_session_candidate_count": 0,
        "candidate_identity_hash": canonical_sha256(["source"]),
    }
    return {
        **selector,
        "input_hash": canonical_sha256(selector),
        "candidate_mapping_hash": canonical_sha256(candidates),
        "prompt_sha256": prompt_hash(selector),
    }


def _old(case: dict) -> dict:
    return {
        "case_key": "ep|turn_full_cheap|target",
        "config_sha256": "old",
        "input_hash": case["input_hash"],
        "candidate_identity_hash": case["candidate_identity_hash"],
        "candidate_mapping_hash": case["candidate_mapping_hash"],
        "candidate_mapping": case["candidates"],
        "model_calls": [
            {
                "prompt_sha256": case["prompt_sha256"],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            }
        ],
        "selected_sources": [],
        "cost_incomplete": False,
    }


def test_hash_reuse_requires_every_selector_input_to_match() -> None:
    case = _case()
    assert reuse_decision(case, _old(case)) == (True, [])
    for field in ("input_hash", "candidate_identity_hash", "candidate_mapping_hash"):
        changed = copy.deepcopy(case)
        changed[field] = "changed"
        reusable, reasons = reuse_decision(changed, _old(case))
        assert not reusable
        assert any(field in reason for reason in reasons)
    changed = copy.deepcopy(case)
    changed["prompt_sha256"] = "changed"
    assert reuse_decision(changed, _old(case))[0] is False


def test_changed_candidate_order_forces_rerun() -> None:
    case = _case()
    second = {
        **case["candidates"][0],
        "evidence_id": "second",
        "text": "second source",
        "node_ids": ["second-node"],
    }
    changed = copy.deepcopy(case)
    changed["candidates"] = [second, *case["candidates"]]
    changed["candidate_mapping_hash"] = canonical_sha256(changed["candidates"])
    changed["prompt_sha256"] = prompt_hash(changed)
    changed["candidate_identity_hash"] = canonical_sha256(["second", "source"])
    assert reuse_decision(changed, _old(case))[0] is False


def test_success_requires_complete_hash_bound_cost() -> None:
    case = _case()
    expected = {
        key: case[key]
        for key in (
            "input_hash",
            "candidate_identity_hash",
            "candidate_mapping_hash",
        )
    }
    result = {
        **_old(case),
        "config_sha256": "cfg",
        "candidate_mapping": case["candidates"],
    }
    checkpoint = {
        "status": "success",
        "config_sha256": "cfg",
        "result": result,
    }
    assert _valid_success(checkpoint, expected, "cfg")
    bad = copy.deepcopy(checkpoint)
    bad["result"]["cost_incomplete"] = True
    assert not _valid_success(bad, expected, "cfg")
    bad = copy.deepcopy(checkpoint)
    bad["result"]["model_calls"][0]["usage"] = None
    assert not _valid_success(bad, expected, "cfg")


def test_stale_recovery_needs_old_heartbeat_and_dead_owner() -> None:
    now = time.time()
    fresh = {
        "status": "in_progress",
        "heartbeat_epoch": now,
        "hostname": "remote",
        "pid": 1,
    }
    assert not checkpoint_stale(fresh, now=now, hostname="local")
    stale_remote = {
        **fresh,
        "heartbeat_epoch": now - STALE_SECONDS - 1,
    }
    assert checkpoint_stale(stale_remote, now=now, hostname="local")
    stopped = {**fresh, "status": "failed", "heartbeat_epoch": 0}
    assert not checkpoint_stale(stopped, now=now, hostname="local")


def test_per_episode_distribution_is_not_global_redistribution() -> None:
    stats = _distribution([0.0, 1.0, 9.0])
    assert stats["total"] == 10.0
    assert stats["mean"] == 10.0 / 3.0
    assert stats["p50"] == 1.0
    assert stats["p95"] == 9.0


def test_full100_runtime_is_gold_free_complete_and_safe() -> None:
    cases, evidence = build_runtime_cases()
    assert len({case["episode_id"] for case in cases}) == 100
    assert len(cases) >= 2209
    assert all(
        case["candidate_mapping_hash"] == canonical_sha256(case["candidates"])
        for case in cases
    )
    forbidden = (
        evidence["safety"]["future_to_past_count"],
        evidence["safety"]["same_turn_directed_count"],
        evidence["safety"]["self_loop_count"],
        evidence["safety"]["directed_cycle_or_nontrivial_scc_count"],
        evidence["safety"]["permutation_failure_count"],
    )
    assert forbidden == (0, 0, 0, 0, 0)
    serialized = str(cases)
    assert "gold_identity" not in serialized
    assert "decision_class" not in serialized
