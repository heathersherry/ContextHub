from __future__ import annotations

import json
import time

import pytest

from integrations.memebench.oracle_paid_layer_experiment import (
    JUDGE_CASES,
    P2_EXECUTION_CASES,
    P2_VERDICT_CASES,
    RETRIEVAL_CASES,
    STATE_CASES,
    PreflightError,
    RunLock,
    assert_single_layer_change,
    build_retrieval_manifest,
    case_union,
    exact_case_manifest,
    load_checkpoint,
    merge_attempt_usage,
    prompt_has_gold_leak,
    stage_gate_summary,
    validate_success,
)
from integrations.memebench.run_oracle_paid_layer_experiment import LEDGER, judge_artifact


def _ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def test_exact_case_manifests_are_bound_to_decision_ledger() -> None:
    groups = exact_case_manifest(_ledger())
    assert groups["stage0_retrieval"] == RETRIEVAL_CASES
    assert groups["stage3_p2_execution"] == P2_EXECUTION_CASES
    assert groups["stage4_p2_verdict"] == P2_VERDICT_CASES
    assert groups["stage5_state"] == STATE_CASES
    assert groups["judge"] == JUDGE_CASES


def test_single_layer_intervention_rejects_extra_change() -> None:
    baseline = {"question": "q", "context": ["old"], "model": "m"}
    assert_single_layer_change(
        baseline, {"question": "q", "context": ["new"], "model": "m"},
        allowed={"context"},
    )
    with pytest.raises(PreflightError, match="single-layer"):
        assert_single_layer_change(
            baseline, {"question": "changed", "context": ["new"], "model": "m"},
            allowed={"context"},
        )


def test_gold_isolation_detects_prompt_leaks() -> None:
    assert not prompt_has_gold_leak("Question: q\nNotes: frozen evidence", gold="Zyranthium")
    assert prompt_has_gold_leak("The answer is Zyranthium", gold="Zyranthium")
    assert prompt_has_gold_leak("Reference answer: hidden", gold="Zyranthium")


def test_stage_gate_closes_paid_stages_without_frozen_nodes() -> None:
    cases = {case_id: {"retrieved_on": [f"ctx://{i}"], "gold_answer": "g"}
             for i, case_id in enumerate(RETRIEVAL_CASES)}
    manifest = build_retrieval_manifest(cases, {})
    summary = stage_gate_summary(manifest)
    assert manifest["eligible_case_count"] == 0
    assert summary["stage1_retrieval"]["run"] == 0
    assert summary["stage2_answer_only"]["eligible"] == 0


def test_checkpoint_resume_accepts_only_complete_success(tmp_path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    complete = {
        "status": "success", "case_id": "c", "stage": "s", "input_sha256": "a" * 64,
        "response": "r", "attempts": [{}],
        "tokens": {"tokens_are_real": True}, "cost_usd": 0.1, "answer": "a",
        "score": True, "run_config_sha256": "cfg",
    }
    failed = {**complete, "case_id": "bad", "status": "failed"}
    path.write_text(json.dumps(complete) + "\n" + json.dumps(failed) + "\n")
    done = load_checkpoint(path, run_config_sha256="cfg")
    assert list(done) == [("s", "c")]
    assert validate_success(complete)
    assert not validate_success(failed)


def test_lock_rejects_live_owner_and_recovers_stale_owner(tmp_path) -> None:
    path = tmp_path / "run.lock"
    path.write_text(json.dumps({"pid": 1, "hostname": "h", "heartbeat": time.time()}))
    with pytest.raises(PreflightError, match="active run lock"):
        RunLock(path, stale_after_s=10).acquire()
    path.write_text(json.dumps({"pid": 1, "hostname": "h", "heartbeat": 0}))
    owner = RunLock(path, stale_after_s=10).acquire()
    assert owner["heartbeat"] > 0


def test_retry_usage_includes_failed_attempt_cost() -> None:
    merged = merge_attempt_usage([
        {"prompt_tokens": 100, "completion_tokens": 0, "cost_usd": .01,
         "cost_complete": True, "error": "ReadTimeout"},
        {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": .02,
         "cost_complete": True},
    ])
    assert merged == {
        "calls": 2, "prompt_tokens": 200, "completion_tokens": 10,
        "cost_usd": .03, "cost_complete": True, "retry_count": 1,
    }


def test_oracle_retrieval_body_must_come_from_frozen_node() -> None:
    case_id = RETRIEVAL_CASES[0]
    cases = {cid: {"retrieved_on": ([f"ctx://{cid}"] if cid == case_id else []),
                   "gold_answer": "g"} for cid in RETRIEVAL_CASES}
    with pytest.raises(PreflightError, match="not frozen"):
        build_retrieval_manifest(
            cases, {f"ctx://{case_id}": {"source": "current_database", "text": "x"}}
        )


def test_judge_artifact_contains_only_blind_fields() -> None:
    cases = {
        "sw_023|hop1|approval_authority": {
            "before_question": "q", "before_gold": "VP A", "before_answer": "A",
            "after_question": "q", "gold_answer": "VP B", "on_answer": "B",
        },
        "sw_026|hop1|project_structure": {
            "before_question": "q", "before_gold": "monorepo /a", "before_answer": "/a",
            "after_question": "q", "gold_answer": "layered /b", "on_answer": "/b",
        },
        "sw_038|hop1|project_structure": {
            "before_question": "q", "before_gold": "modular /a", "before_answer": "/a",
            "after_question": "q", "gold_answer": "monorepo /b", "on_answer": "/a",
        },
    }
    artifact = judge_artifact(cases)
    encoded = json.dumps(artifact)
    assert "before_ok" not in encoded
    assert "old_primary" not in encoded
    assert sum(row["final_recovered_by_scoring_correction"] for row in artifact["cases"]) == 2


def test_case_union_deduplicates_overlapping_oracle_arms() -> None:
    rows = {
        "a": [{"case_id": "c1", "status": "success", "recovered": True}],
        "b": [
            {"case_id": "c1", "status": "success", "recovered": True},
            {"case_id": "c2", "status": "success", "recovered": True},
            {"case_id": "c3", "status": "failed", "recovered": True},
        ],
    }
    assert case_union(rows) == ["c1", "c2"]
