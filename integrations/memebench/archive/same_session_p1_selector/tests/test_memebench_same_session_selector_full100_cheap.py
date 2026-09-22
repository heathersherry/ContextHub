from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.dependency_discovery_service import DependencyDiscoveryService
from integrations.memebench.gold_edge_audit import append_jsonl, canonical_sha256
from integrations.memebench.run_same_session_selector_full100_cheap import (
    ARM,
    EXPECTED_CONTENT_HASH,
    EXPECTED_INDEX_HASH,
    ForbiddenStrongChat,
    MAPPING_RULE,
    _mapping_entities,
    _successful,
    validate_scoring_sidecar,
    validate_shared,
)
from integrations.memebench.same_session_selector_intervention import (
    AuditedChatClient,
    run_selector_case,
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
            "completion_tokens": 1,
            "total_tokens": 11,
        }
        return self.answer


def _case() -> dict:
    candidates = [
        {
            "evidence_id": "source",
            "text": "source fact",
            "node_ids": ["source-node"],
            "source_origin": "same_session_turn_envelope",
            "session_index": 0,
            "turn_index": 0,
        }
    ]
    case = {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "target_text": "target fact",
        "target_node_ids": ["target-node"],
        "target_session_index": 0,
        "target_turn_index": 1,
        "candidates": candidates,
        "history_candidate_count": 0,
        "same_session_candidate_count": 1,
        "candidate_identity_hash": canonical_sha256(["source"]),
    }
    case["input_hash"] = canonical_sha256(case)
    return case


def test_shared_input_and_v2_round_trip_are_complete() -> None:
    index, manifests = validate_shared(allow_scoring=False)
    assert index["manifest_content_sha256"] == EXPECTED_CONTENT_HASH
    assert index["index_sha256"] == EXPECTED_INDEX_HASH
    assert len(manifests) == 100
    assert sum(len(row["selector_cases"]) for row in manifests) == 2209
    side, manifest = validate_scoring_sidecar()
    assert side["episode_count"] == side["hop1_episode_count"] == 100
    assert side["hop2_episode_count"] == 64
    assert side["mapping_rule_version"] == MAPPING_RULE
    assert manifest["mapping_rule_version"] == MAPPING_RULE


def test_fixed_arm_calls_only_cheap_real_selector_path() -> None:
    inner = StaticChat("1")
    audit = AuditedChatClient(inner, "gpt-4o-mini", "openlux")
    service = DependencyDiscoveryService(audit)
    forbidden_audit = AuditedChatClient(ForbiddenStrongChat(), "FORBIDDEN", "FORBIDDEN")
    result = asyncio.run(
        run_selector_case(
            _case(),
            ARM,
            cheap=service,
            strong=DependencyDiscoveryService(forbidden_audit),
            cheap_audit=audit,
            strong_audit=forbidden_audit,
        )
    )
    assert result["candidate_tier"] == "full"
    assert result["edge_tier"] == "cheap"
    assert result["selected_source_count"] == 1
    assert len(result["model_calls"]) == 1
    assert "source fact" in inner.prompts[0]
    assert "gold" not in inner.prompts[0].casefold()


def test_resume_deduplicates_and_rejects_hash_mismatch(tmp_path: Path) -> None:
    expected = {
        "case_key": "ep|turn_full_cheap|target",
        "input_hash": "input",
        "candidate_identity_hash": "identity",
        "candidate_mapping_hash": "mapping",
    }
    config = {"config_sha256": "config", "expected_cases": [expected]}
    success = {
        **expected,
        "config_sha256": "config",
        "selected_sources": [],
        "model_calls": [],
    }
    append_jsonl(tmp_path / "case_success.jsonl", success)
    append_jsonl(tmp_path / "case_success.jsonl", success)
    assert len(_successful(tmp_path, config)) == 1
    bad = copy.deepcopy(success)
    bad["candidate_mapping_hash"] = "changed"
    (tmp_path / "case_success.jsonl").write_text(json.dumps(bad) + "\n")
    assert _successful(tmp_path, config) == {}


def test_mapping_diagnostic_keeps_all_matches() -> None:
    mapping = {
        "matched_entities": [{"entity": "a"}, {"entity": "b"}],
        "matched_entity_count": 2,
    }
    assert _mapping_entities(mapping) == {"a", "b"}
