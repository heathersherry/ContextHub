from __future__ import annotations

import copy
from pathlib import Path

import pytest

from integrations.memebench.compare_same_session_selector_full100 import (
    AuditError,
    _independent_run_caveat,
    _validate_run,
    compare_distributions,
    compute_costs,
    score_arm,
)


def _mapping(evidence_id: str, entities: list[str]) -> dict:
    return {
        "evidence_id": evidence_id,
        "matched_entities": [{"entity": entity} for entity in entities],
        "matched_entity_count": len(entities),
        "unmapped": not entities,
        "entity_mapping_ambiguous": len(entities) > 1,
        "has_alignment_ambiguous_alias": False,
        "source_identity_ambiguity": False,
    }


def _case() -> dict:
    return {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "target_text": "target",
        "candidates": [
            {
                "evidence_id": "source-a",
                "source_origin": "same_session_turn_envelope",
            },
            {
                "evidence_id": "source-alias",
                "source_origin": "same_session_turn_envelope",
            },
        ],
    }


def _side() -> dict:
    return {
        "episodes": [
            {
                "episode_id": "ep",
                "evidence_to_entity_mappings": [
                    _mapping("source-a", ["source"]),
                    _mapping("source-alias", ["source"]),
                    _mapping("target", ["target"]),
                ],
                "scoring_records": [
                    {
                        "hop": 1,
                        "gold_edge_identity_v1_set": ["gold"],
                        "gold_edges": [
                            {
                                "gold_edge_identity_v1": "gold",
                                "source_entity": "source",
                                "target_entity": "target",
                            }
                        ],
                    },
                    {
                        "hop": 2,
                        "gold_edge_identity_v1_set": ["gold"],
                        "gold_edges": [
                            {
                                "gold_edge_identity_v1": "gold",
                                "source_entity": "source",
                                "target_entity": "target",
                            }
                        ],
                    },
                ],
            }
        ]
    }


def test_same_session_denominator_is_coverable_gold_not_all_gold() -> None:
    side = _side()
    side["episodes"][0]["scoring_records"][0]["gold_edge_identity_v1_set"].append(
        "uncoverable"
    )
    side["episodes"][0]["scoring_records"][0]["gold_edges"].append(
        {
            "gold_edge_identity_v1": "uncoverable",
            "source_entity": "absent",
            "target_entity": "target",
        }
    )
    success = {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "selected_sources": [
            {
                "source_evidence_id": "source-a",
                "source_origin": "same_session_turn_envelope",
            }
        ],
    }
    hop1 = score_arm([success], [_case()], side)["by_hop"]["hop1"]
    assert hop1["unique_gold_edge_count"] == 2
    assert hop1["same_session_coverable_unique_gold_edge_count"] == 1
    assert hop1["recalled_same_session_unique_gold_edge_count"] == 1
    assert hop1["same_session_unique_gold_edge_recall"] == 1.0


def test_gold_identity_deduplicates_aliases_and_cross_hop_views() -> None:
    success = {
        "episode_id": "ep",
        "target_evidence_id": "target",
        "selected_sources": [
            {
                "source_evidence_id": "source-a",
                "source_origin": "same_session_turn_envelope",
            },
            {
                "source_evidence_id": "source-alias",
                "source_origin": "same_session_turn_envelope",
            },
        ],
    }
    scored = score_arm([success], [_case()], _side())
    assert scored["by_hop"]["hop1"]["recalled_unique_gold_edge_count"] == 1
    assert scored["by_hop"]["hop2"]["recalled_unique_gold_edge_count"] == 1
    assert scored["output_graph"]["selected_edge_incidence_count"] == 2
    assert scored["output_graph"]["unique_evidence_edge_count"] == 2


def test_cost_comparison_uses_actual_per_episode_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    import integrations.memebench.compare_same_session_selector_full100 as module

    monkeypatch.setattr(
        module,
        "read_jsonl_tolerant",
        lambda _path: [
            {
                "episode_id": f"ep-{index}",
                "shared_preprocessing_total": {"known_usd": float(index)},
            }
            for index in range(1, 101)
        ],
    )
    usage = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 0,
        "total_tokens": 1_000_000,
    }
    successes = {
        "cheap": [
            {
                "episode_id": f"ep-{index}",
                "model_calls": [
                    {"model": "gpt-4o-mini", "usage": usage}
                ]
                if index == 1
                else [],
            }
            for index in range(1, 101)
        ],
        "verify": [
            {
                "episode_id": f"ep-{index}",
                "model_calls": [
                    {"model": "gpt-4.1-mini", "usage": usage}
                ]
                if index == 100
                else [],
            }
            for index in range(1, 101)
        ],
    }
    summary, rows = compute_costs(successes)
    assert rows["cheap"]["ep-1"]["deployment_total"] == 1.15
    assert rows["verify"]["ep-100"]["deployment_total"] == 100.4
    assert summary["cheap"]["selector"]["total"] == pytest.approx(0.15)
    assert summary["verify"]["selector"]["total"] == pytest.approx(0.40)
    comparison = compare_distributions(
        summary["cheap"]["selector"], summary["verify"]["selector"]
    )
    assert comparison["total"]["verify_minus_cheap"] == pytest.approx(0.25)


def test_independent_run_caveat_is_explicit_and_evidence_bound() -> None:
    runs = {
        "cheap": [
            {
                "episode_id": "ep",
                "target_evidence_id": "target",
                "model_calls": [
                    {
                        "prompt_sha256": "same-prompt",
                        "answer_sha256": "cheap-answer",
                    }
                ],
            }
        ],
        "verify": [
            {
                "episode_id": "ep",
                "target_evidence_id": "target",
                "model_calls": [
                    {
                        "prompt_sha256": "same-prompt",
                        "answer_sha256": "verify-answer",
                    }
                ],
            }
        ],
    }
    caveat = _independent_run_caveat(runs)
    assert caveat["cheap_llm_runs_are_independent"] is True
    assert caveat[
        "paired_episode_alignment_is_descriptive_not_deterministic_strong_only_counterfactual"
    ] is True
    assert caveat["same_prompt_hash_count"] == 1
    assert caveat["different_answer_hash_count"] == 1


def test_hash_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import integrations.memebench.compare_same_session_selector_full100 as module

    bad_config = {
        "config_sha256": "wrong",
        "arm": "turn_full_cheap",
        "model": "gpt-4o-mini",
        "expected_cases": [],
    }
    monkeypatch.setattr(module, "_json", lambda _path: copy.deepcopy(bad_config))
    with pytest.raises(AuditError, match="config hash/arm mismatch"):
        _validate_run("cheap", tmp_path, [])
