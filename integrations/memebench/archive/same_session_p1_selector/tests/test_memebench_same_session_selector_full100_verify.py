from __future__ import annotations

import json
from pathlib import Path

import httpx

from integrations.memebench.run_same_session_selector_full100_verify import (
    ARM,
    EXPECTED_SHARED_CONTENT,
    EXPECTED_SHARED_INDEX,
    _expected,
    _retryable,
    _score,
    _validate_shared_index,
    validate_sidecar,
)


def _case() -> dict:
    candidates = [
        {
            "evidence_id": "source",
            "text": "source text",
            "node_ids": ["source-node"],
            "source_origin": "same_session_turn_envelope",
            "session_index": 0,
            "turn_index": 0,
        }
    ]
    return {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "target_text": "target text",
        "target_node_ids": ["target-node"],
        "target_session_index": 0,
        "target_turn_index": 1,
        "candidates": candidates,
        "history_candidate_count": 0,
        "same_session_candidate_count": 1,
        "candidate_identity_hash": "candidate-hash",
        "input_hash": "input-hash",
    }


def _mapping(evidence_id: str, entity: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "matched_entities": [{"entity": entity}],
        "unmapped": False,
        "entity_mapping_ambiguous": False,
        "has_alignment_ambiguous_alias": False,
        "source_identity_ambiguity": False,
    }


def test_frozen_shared_input_has_exact_preregistered_shape() -> None:
    index, cases = _validate_shared_index()
    assert index["manifest_content_sha256"] == EXPECTED_SHARED_CONTENT
    assert index["index_sha256"] == EXPECTED_SHARED_INDEX
    assert len(index["episodes"]) == 100
    assert len(cases) == 2209
    assert sum(row["hop2_applicable"] for row in index["episodes"]) == 64


def test_v2_hashes_completeness_and_v1_round_trip() -> None:
    result = validate_sidecar()
    assert result["episodes"] == 100
    assert result["hop1"] == 100
    assert result["hop2"] == 64
    assert result["v1_round_trip"] is True


def test_expected_case_preregisters_full_candidate_mapping_hash() -> None:
    row = _expected([_case()])[0]
    assert row["case_key"] == f"ep|{ARM}|target"
    assert row["candidate_mapping_sha256"]
    assert row["input_hash"] == "input-hash"


def test_approximate_scoring_uses_evidence_entity_mapping_and_origin() -> None:
    case = _case()
    success = {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "selected_sources": [
            {
                "source_evidence_id": "source",
                "source_origin": "same_session_turn_envelope",
                "source_text": "source text",
            }
        ],
    }
    side = {
        "episodes": [
            {
                "episode_id": "ep",
                "evidence_to_entity_mappings": [
                    _mapping("source", "source_entity"),
                    _mapping("target", "target_entity"),
                ],
                "scoring_records": [
                    {
                        "hop": 1,
                        "gold_edge_identity_v1_set": ["gold-edge"],
                        "gold_edges": [
                            {
                                "gold_edge_identity_v1": "gold-edge",
                                "source_entity": "source_entity",
                                "target_entity": "target_entity",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    scoring, diagnostics, non_gold = _score([success], [case], side)
    hop1 = scoring["by_hop"]["hop1"]
    assert hop1["all_gold_edge_recall"] == 1.0
    assert hop1["same_session_gold_recall"] == 1.0
    assert hop1["episode_graph_miss_count"] == 0
    assert diagnostics[0]["matched_gold_edge_identities"] == ["gold-edge"]
    assert non_gold == []
    assert scoring["approximate_diagnostic_only"] is True
    assert scoring["exact_precision_claim"] is False


def test_unmatched_is_not_automatically_false() -> None:
    case = _case()
    success = {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "selected_sources": [
            {
                "source_evidence_id": "source",
                "source_origin": "same_session_turn_envelope",
                "source_text": "source text",
            }
        ],
    }
    side = {
        "episodes": [
            {
                "episode_id": "ep",
                "evidence_to_entity_mappings": [
                    _mapping("source", "other"),
                    _mapping("target", "target_entity"),
                ],
                "scoring_records": [
                    {
                        "hop": 1,
                        "gold_edge_identity_v1_set": ["gold-edge"],
                        "gold_edges": [
                            {
                                "gold_edge_identity_v1": "gold-edge",
                                "source_entity": "source_entity",
                                "target_entity": "target_entity",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    scoring, _, non_gold = _score([success], [case], side)
    assert scoring["by_hop"]["hop1"]["all_gold_edge_recall"] == 0.0
    assert len(non_gold) == 1
    assert "false_positive" not in non_gold[0]
    assert non_gold[0]["matched_gold_edge_count"] == 0


def test_retry_classification_is_bounded_to_transient_errors() -> None:
    request = httpx.Request("POST", "https://example.test")
    rate = httpx.HTTPStatusError(
        "rate", request=request, response=httpx.Response(429, request=request)
    )
    auth = httpx.HTTPStatusError(
        "auth", request=request, response=httpx.Response(401, request=request)
    )
    assert _retryable(httpx.ReadTimeout("slow"))
    assert _retryable(rate)
    assert not _retryable(auth)


def test_scoring_sidecar_paths_are_not_embedded_in_expected_cases() -> None:
    index, cases = _validate_shared_index()
    rendered = json.dumps(_expected(cases[:2]), sort_keys=True)
    assert "gold" not in rendered
    assert "sidecar" not in rendered
    assert index["read_only_input_for_arms"] is True
