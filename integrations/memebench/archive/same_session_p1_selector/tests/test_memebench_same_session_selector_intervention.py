from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.dependency_discovery_service import DependencyDiscoveryService
from integrations.memebench.gold_edge_audit import append_jsonl, read_jsonl_tolerant
from integrations.memebench.run_same_session_selector_intervention import (
    _fatal_error,
    _retryable,
    _successful_rows,
)
from integrations.memebench.same_session_selector_intervention import (
    ARM_CONFIGS,
    AuditedChatClient,
    build_selector_cases,
    run_selector_case,
    score_successes,
    selector_case_key,
    summarize_outputs,
)


ROOT = Path(__file__).resolve().parents[1]
FROZEN = (
    ROOT
    / "integrations"
    / "memebench"
    / "runs"
    / "p1_same_session_manual_validation_20260823"
)


class StaticChat(BaseChatClient):
    def __init__(self, answer: str):
        self.answer = answer
        self.last_usage = None
        self.prompts: list[str] = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        self.last_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }
        return self.answer


def _node(node_id: str, text: str, session: int, turn: int) -> dict:
    turns = [
        {"turn_index": index, "role": "user", "content": f"turn-{index}"}
        for index in range(turn + 1)
    ]
    turns[turn]["content"] = text
    return {
        "node_id": node_id,
        "text": text,
        "session_index": session,
        "original_session_id": f"s{session}",
        "original_turns": turns,
    }


def _record() -> dict:
    nodes = [
        _node("00000000-0000-0000-0000-000000000001", "history", 0, 0),
        _node("00000000-0000-0000-0000-000000000002", "same source", 1, 0),
        _node("00000000-0000-0000-0000-000000000003", "target", 1, 1),
    ]
    return {
        "episode_id": "ep",
        "nodes": nodes,
        "candidate_traces": [
            {
                "node_id": nodes[0]["node_id"],
                "candidate_snapshot_ids": [],
            },
            {
                "node_id": nodes[1]["node_id"],
                "candidate_snapshot_ids": [nodes[0]["node_id"]],
            },
            {
                "node_id": nodes[2]["node_id"],
                "candidate_snapshot_ids": [nodes[0]["node_id"]],
            },
        ],
    }


def test_union_preserves_history_and_strict_earlier_turn() -> None:
    cases = build_selector_cases([_record()])
    target = next(row for row in cases if row["target_text"] == "target")
    assert target["history_candidate_count"] == 1
    assert target["same_session_candidate_count"] == 1
    assert {row["source_origin"] for row in target["candidates"]} == {
        "history_snapshot",
        "same_session_turn_envelope",
    }
    same = next(
        row
        for row in target["candidates"]
        if row["source_origin"] == "same_session_turn_envelope"
    )
    assert same["turn_index"] < target["target_turn_index"]


def test_same_turn_and_ambiguous_are_quarantined() -> None:
    record = _record()
    duplicate = _node(
        "00000000-0000-0000-0000-000000000004",
        "repeated",
        1,
        0,
    )
    duplicate["original_turns"] = [
        {"turn_index": 0, "role": "user", "content": "repeated"},
        {"turn_index": 1, "role": "user", "content": "repeated"},
    ]
    record["nodes"].append(duplicate)
    record["candidate_traces"].append(
        {"node_id": duplicate["node_id"], "candidate_snapshot_ids": []}
    )
    cases = build_selector_cases([record])
    assert all(duplicate["node_id"] not in row["target_node_ids"] for row in cases)
    assert all(
        duplicate["node_id"] not in candidate["node_ids"]
        for row in cases
        for candidate in row["candidates"]
    )


def test_aliases_dedupe_and_permutation_is_invariant() -> None:
    record = _record()
    alias = copy.deepcopy(record["nodes"][1])
    alias["node_id"] = "00000000-0000-0000-0000-000000000004"
    record["nodes"].append(alias)
    record["candidate_traces"].append(
        {
            "node_id": alias["node_id"],
            "candidate_snapshot_ids": [record["nodes"][0]["node_id"]],
        }
    )
    baseline = build_selector_cases([record])
    permuted = copy.deepcopy(record)
    permuted["nodes"].reverse()
    permuted["candidate_traces"].reverse()
    observed = build_selector_cases([permuted])
    assert baseline == observed
    target = next(row for row in baseline if row["target_text"] == "target")
    same = [
        row
        for row in target["candidates"]
        if row["source_origin"] == "same_session_turn_envelope"
    ]
    assert len(same) == 1
    assert len(same[0]["node_ids"]) == 2


def test_real_router_and_discovery_service_are_called_without_gold_prompt() -> None:
    case = next(row for row in build_selector_cases([_record()]) if row["target_text"] == "target")
    cheap_inner = StaticChat("1")
    strong_inner = StaticChat("2")
    cheap_audit = AuditedChatClient(cheap_inner, "gpt-4o-mini", "openlux")
    strong_audit = AuditedChatClient(strong_inner, "gpt-4.1-mini", "openlux")
    result = asyncio.run(
        run_selector_case(
            case,
            "turn_full_verify",
            cheap=DependencyDiscoveryService(cheap_audit),
            strong=DependencyDiscoveryService(strong_audit),
            cheap_audit=cheap_audit,
            strong_audit=strong_audit,
        )
    )
    assert result["candidate_tier"] == "full"
    assert result["candidate_count"] == 2
    assert result["edge_tier"] == "strong"
    assert len(result["model_calls"]) == 2
    prompt = "\n".join(row["prompt"] for row in result["model_calls"])
    assert "same source" in prompt and "history" in prompt
    assert "gold" not in prompt and "manual" not in prompt


def test_cheap_arm_never_calls_strong() -> None:
    case = next(row for row in build_selector_cases([_record()]) if row["target_text"] == "target")
    cheap_inner = StaticChat("1")
    strong_inner = StaticChat("1")
    cheap_audit = AuditedChatClient(cheap_inner, "gpt-4o-mini", "openlux")
    strong_audit = AuditedChatClient(strong_inner, "gpt-4.1-mini", "openlux")
    result = asyncio.run(
        run_selector_case(
            case,
            "turn_full_cheap",
            cheap=DependencyDiscoveryService(cheap_audit),
            strong=DependencyDiscoveryService(strong_audit),
            cheap_audit=cheap_audit,
            strong_audit=strong_audit,
        )
    )
    assert result["edge_tier"] == "cheap"
    assert len(cheap_inner.prompts) == 1
    assert strong_inner.prompts == []


def test_empty_union_is_a_successful_zero_call_case() -> None:
    case = next(row for row in build_selector_cases([_record()]) if not row["candidates"])
    cheap_inner = StaticChat("1")
    strong_inner = StaticChat("1")
    cheap_audit = AuditedChatClient(cheap_inner, "gpt-4o-mini", "openlux")
    strong_audit = AuditedChatClient(strong_inner, "gpt-4.1-mini", "openlux")
    result = asyncio.run(
        run_selector_case(
            case,
            "turn_full_cheap",
            cheap=DependencyDiscoveryService(cheap_audit),
            strong=DependencyDiscoveryService(strong_audit),
            cheap_audit=cheap_audit,
            strong_audit=strong_audit,
        )
    )
    assert result["candidate_tier"] == "block"
    assert result["candidate_count"] == 0
    assert result["model_calls"] == []


def test_resume_accepts_only_hash_bound_success_and_dedupes(tmp_path: Path) -> None:
    config = {
        "config_sha256": "cfg",
        "expected_cases": [
            {
                "case_key": "ep|arm|target",
                "input_hash": "input",
                "candidate_identity_hash": "cands",
            }
        ],
    }
    good = {
        "case_key": "ep|arm|target",
        "config_sha256": "cfg",
        "input_hash": "input",
        "candidate_identity_hash": "cands",
        "selected_sources": [],
        "model_calls": [],
    }
    append_jsonl(tmp_path / "case_success.jsonl", good)
    append_jsonl(tmp_path / "case_success.jsonl", good)
    assert len(_successful_rows(tmp_path, config)) == 1
    bad = {**good, "input_hash": "changed"}
    (tmp_path / "case_success.jsonl").write_text(
        json.dumps(bad) + "\n", encoding="utf-8"
    )
    assert _successful_rows(tmp_path, config) == []


def test_retry_and_fatal_errors_are_distinguished() -> None:
    request = httpx.Request("POST", "https://example.test")
    response_429 = httpx.Response(429, request=request)
    response_401 = httpx.Response(401, request=request)
    rate = httpx.HTTPStatusError("rate", request=request, response=response_429)
    auth = httpx.HTTPStatusError("auth", request=request, response=response_401)
    assert _retryable(httpx.ReadTimeout("slow")) is True
    assert _retryable(rate) is True
    assert _fatal_error(rate) is False
    assert _retryable(auth) is False
    assert _fatal_error(auth) is True
    assert _fatal_error(ValueError("schema")) is True


def test_source_identity_violation_is_scoring_side_only() -> None:
    case = next(row for row in build_selector_cases([_record()]) if row["target_text"] == "target")
    source = next(row for row in case["candidates"] if row["text"] == "same source")
    success = {
        "case_key": selector_case_key(case, "turn_full_cheap"),
        "episode_id": "ep",
        "arm": "turn_full_cheap",
        "target_evidence_id": case["target_evidence_id"],
        "selected_sources": [
            {
                "source_evidence_id": source["evidence_id"],
                "source_node_ids": source["node_ids"],
                "source_origin": source["source_origin"],
                "source_text": source["text"],
            }
        ],
    }
    decision = {
        "semantic_edge_id": "edge",
        "episode_id": "ep",
        "reviewed_valid_source_node_id": "missing-valid-source",
        "reviewed_valid_target_node_id": case["target_node_ids"][0],
    }
    manual = {
        "semantic_edge_id": "edge",
        "substring_only_source_match_node_ids": source["node_ids"],
    }
    scored = score_successes([success], [case], [decision], [manual])
    row = next(
        row
        for row in scored["edge_results"]
        if row["arm"] == "turn_full_cheap"
    )
    assert row["selected"] is False
    assert row["source_identity_violation_count"] == 1


def test_summary_counts_success_rows_only() -> None:
    cases = build_selector_cases([_record()])
    case = next(row for row in cases if row["target_text"] == "target")
    success = {
        "case_key": selector_case_key(case, "turn_full_cheap"),
        "episode_id": "ep",
        "arm": "turn_full_cheap",
        "target_evidence_id": case["target_evidence_id"],
        "history_candidate_count": case["history_candidate_count"],
        "same_session_candidate_count": case["same_session_candidate_count"],
        "selected_sources": [],
        "model_calls": [],
    }
    summary = summarize_outputs([success], cases)
    assert summary["turn_full_cheap"]["successful_case_count"] == 1
    assert summary["turn_full_verify"]["successful_case_count"] == 0


def test_frozen_union_keeps_all_13_gold_edges_visible() -> None:
    evidence = read_jsonl_tolerant(FROZEN / "case_evidence.jsonl")
    decisions = json.loads(
        (FROZEN / "manual_decisions_v2.json").read_text(encoding="utf-8")
    )["decisions"]
    manual = read_jsonl_tolerant(FROZEN / "manual_validation_v2.jsonl")
    cases = build_selector_cases(evidence)
    scored = score_successes([], cases, decisions, manual)
    for arm in ARM_CONFIGS:
        assert scored["arms"][arm]["gold_edge_count"] == 13
        assert scored["arms"][arm]["candidate_routing_visible_count"] == 13
    assert sum(row["same_session_candidate_count"] for row in cases) == 707
