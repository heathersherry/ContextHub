from __future__ import annotations

import inspect
import json
from pathlib import Path

import httpx

from integrations.memebench.gold_edge_audit import append_jsonl, canonical_sha256
from integrations.memebench.run_full100_endpoint_selector_rerun import (
    ARM,
    MODEL,
    _retryable,
    _successful,
    build_runtime_cases,
    build_target_manifest,
    prepare,
    summarize,
)


def test_target_manifest_binds_exact_six_node_and_evidence_identities() -> None:
    cases, results = build_runtime_cases()
    manifest = build_target_manifest(cases, results)
    assert [row["episode_id"] for row in manifest["rows"]] == [
        "pl_001",
        "pl_011",
        "pl_016",
        "pl_019",
        "pl_024",
        "pl_049",
    ]
    assert all(len(row["target_identity"]["node_ids"]) == 1 for row in manifest["rows"])
    assert all(
        row["expected_source_identity"]["counterfactual_evidence_ids"]
        and row["expected_source_identity"]["candidate_ids"]
        and row["runtime_binding"]["expected_source_visible"]
        for row in manifest["rows"]
    )


def test_runtime_cases_are_gold_free_and_hash_bound() -> None:
    cases, _ = build_runtime_cases()
    payload = json.dumps(cases, sort_keys=True)
    for forbidden in (
        "gold_identity",
        "expected_source_identity",
        "source_gold_endpoint",
        "decision_class",
        "evidence_explanation",
        "漏边",
    ):
        assert forbidden not in payload
    for case in cases:
        assert case["candidate_identity_hash"] == canonical_sha256(
            [row["evidence_id"] for row in case["candidates"]]
        )
        assert case["candidate_mapping_hash"] == canonical_sha256(case["candidates"])
        without_input = {k: v for k, v in case.items() if k != "input_hash"}
        assert case["input_hash"] == canonical_sha256(without_input)


def test_frozen_v3_candidate_counts_and_expected_source_are_stable() -> None:
    cases, results = build_runtime_cases()
    manifest = build_target_manifest(cases, results)
    assert [len(case["candidates"]) for case in cases] == [25, 17, 22, 17, 22, 21]
    assert [row["old_checkpoint"]["candidate_count"] for row in manifest["rows"]] == [
        19,
        16,
        18,
        12,
        17,
        18,
    ]


def test_resume_rejects_hash_or_cost_incomplete_success(tmp_path: Path) -> None:
    config = {
        "config_sha256": "cfg",
        "expected_cases": [
            {
                "case_key": "ep|arm|target",
                "input_hash": "input",
                "candidate_identity_hash": "ids",
                "candidate_mapping_hash": "mapping",
            }
        ],
    }
    base = {
        "case_key": "ep|arm|target",
        "config_sha256": "cfg",
        "input_hash": "input",
        "candidate_identity_hash": "ids",
        "candidate_mapping_hash": "mapping",
        "candidate_mapping": [],
        "selected_sources": [],
        "model_calls": [
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                }
            }
        ],
        "cost_incomplete": False,
    }
    append_jsonl(tmp_path / "case_success.jsonl", base)
    assert set(_successful(tmp_path, config)) == {"ep|arm|target"}
    (tmp_path / "case_success.jsonl").write_text(
        json.dumps({**base, "cost_incomplete": True}) + "\n", encoding="utf-8"
    )
    assert _successful(tmp_path, config) == {}
    (tmp_path / "case_success.jsonl").write_text(
        json.dumps({**base, "candidate_mapping_hash": "changed"}) + "\n",
        encoding="utf-8",
    )
    assert _successful(tmp_path, config) == {}


def test_retry_classes_and_paid_path_do_not_read_gold() -> None:
    request = httpx.Request("POST", "https://example.test")
    assert _retryable(httpx.ReadTimeout("slow"))
    assert _retryable(
        httpx.HTTPStatusError(
            "rate",
            request=request,
            response=httpx.Response(429, request=request),
        )
    )
    assert not _retryable(
        httpx.HTTPStatusError(
            "auth",
            request=request,
            response=httpx.Response(401, request=request),
        )
    )
    from integrations.memebench import run_full100_endpoint_selector_rerun as runner

    paid_source = inspect.getsource(runner.run)
    assert "target_manifest" not in paid_source
    assert "_ledger_rows" not in paid_source
    assert "_mapping_risks" not in paid_source


def test_paired_summary_scores_only_completed_outputs(tmp_path: Path) -> None:
    prepare(tmp_path)
    runtime = json.loads((tmp_path / "runtime_cases.json").read_text(encoding="utf-8"))
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "target_manifest.json").read_text(encoding="utf-8"))
    by_episode = {row["episode_id"]: row for row in manifest["rows"]}
    for case in runtime["cases"]:
        target = by_episode[case["episode_id"]]
        expected_nodes = set(target["expected_source_identity"]["node_ids"])
        source = next(
            row for row in case["candidates"] if set(row["node_ids"]) & expected_nodes
        )
        append_jsonl(
            tmp_path / "case_success.jsonl",
            {
                "case_key": f"{case['episode_id']}|{ARM}|{case['target_evidence_id']}",
                "episode_id": case["episode_id"],
                "arm": ARM,
                "target_evidence_id": case["target_evidence_id"],
                "target_node_ids": case["target_node_ids"],
                "target_text": case["target_text"],
                "input_hash": case["input_hash"],
                "candidate_identity_hash": case["candidate_identity_hash"],
                "candidate_mapping_hash": case["candidate_mapping_hash"],
                "candidate_mapping": case["candidates"],
                "candidate_count": len(case["candidates"]),
                "history_candidate_count": case["history_candidate_count"],
                "same_session_candidate_count": case["same_session_candidate_count"],
                "candidate_tier": "full",
                "edge_tier": "cheap",
                "selected_sources": [
                    {
                        "source_evidence_id": source["evidence_id"],
                        "source_node_ids": source["node_ids"],
                        "source_origin": source["source_origin"],
                        "source_text": source["text"],
                    }
                ],
                "selected_source_count": 1,
                "model_calls": [
                    {
                        "model": MODEL,
                        "provider": "openlux",
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 10,
                            "total_tokens": 110,
                        },
                        "cost_incomplete": False,
                    }
                ],
                "cost_incomplete": False,
                "attempt_number": 1,
                "config_sha256": config["config_sha256"],
            },
        )
    summary = summarize(tmp_path)
    assert summary["paired"]["old_recall"]["numerator"] == 0
    assert summary["paired"]["new_recall"]["numerator"] == 6
    assert summary["paired"]["old_episode_graph_miss"] == 6
    assert summary["paired"]["new_episode_graph_miss"] == 0
    assert summary["paired"]["candidate_delta"] == 24
    assert summary["calls_and_cost"]["actual_model_calls"] == 6
    assert summary["gold_scoring_started_after_runtime_checkpoints"] is True
