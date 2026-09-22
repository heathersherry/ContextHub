from __future__ import annotations

import copy
import json
from pathlib import Path

from integrations.memebench.gold_edge_audit import read_jsonl_tolerant
from integrations.memebench.turn_candidate_mechanism import (
    align_node_to_raw_turn,
    audit_candidate_invariants,
    candidate_inflation,
    generate_turn_candidates,
    score_with_manual_gold,
    verify_permutation_invariance,
)


ROOT = Path(__file__).resolve().parents[1]
FROZEN = (
    ROOT
    / "integrations"
    / "memebench"
    / "runs"
    / "p1_same_session_manual_validation_20260823"
)


def _turns(*contents: str) -> list[dict]:
    return [
        {"turn_index": index, "role": "user", "content": content}
        for index, content in enumerate(contents)
    ]


def _node(
    node_id: str,
    text: str,
    turns: list[dict],
    *,
    session_index: int = 3,
    session_id: str = "raw-session",
) -> dict:
    return {
        "node_id": node_id,
        "text": text,
        "session_index": session_index,
        "original_session_id": session_id,
        "original_turns": turns,
    }


def test_strict_turn_candidates_are_order_invariant_and_traceable() -> None:
    turns = _turns("source fact", "middle fact", "target fact")
    nodes = [
        _node("target-uuid", "target fact", turns),
        _node("source-uuid", "source fact", turns),
        _node("middle-uuid", "middle fact", turns),
    ]
    result = generate_turn_candidates("episode", nodes)
    assert len(result["candidates"]) == 3
    assert all(
        row["source_turn_index"] < row["target_turn_index"]
        and row["source_span"]["turn_index"] is not None
        and row["target_span"]["turn_index"] is not None
        for row in result["candidates"]
    )
    check = verify_permutation_invariance("episode", nodes)
    assert check["permutation_count"] >= 9
    assert check["all_identical"] is True


def test_same_turn_is_unresolved_and_never_directed() -> None:
    turns = _turns("alpha and beta")
    result = generate_turn_candidates(
        "episode",
        [
            _node("a", "alpha", turns),
            _node("b", "beta", turns),
        ],
    )
    assert result["candidates"] == []
    assert len(result["same_turn_unresolved"]) == 1
    assert audit_candidate_invariants([result])["same_turn_directed_count"] == 0


def test_future_session_and_future_turn_are_never_sources() -> None:
    early_turns = _turns("early")
    future_turns = _turns("future")
    result = generate_turn_candidates(
        "episode",
        [
            _node("future", "future", future_turns, session_index=4, session_id="s4"),
            _node("early", "early", early_turns, session_index=3, session_id="s3"),
        ],
    )
    assert result["candidates"] == []
    assert audit_candidate_invariants([result])["future_to_past_count"] == 0


def test_ambiguous_alignment_is_quarantined() -> None:
    turns = _turns("same repeated text", "same repeated text", "target")
    ambiguous = _node("ambiguous", "same repeated text", turns)
    alignment = align_node_to_raw_turn("episode", ambiguous)
    assert alignment["status"] == "ambiguous"
    assert alignment["reason"] == "multiple_exact_turns"
    result = generate_turn_candidates(
        "episode",
        [ambiguous, _node("target", "target", turns)],
    )
    assert result["candidates"] == []
    assert audit_candidate_invariants([result])["ambiguous_alignment_count"] == 1


def test_unique_token_overlap_is_separate_stratum() -> None:
    turns = _turns(
        "Our office is located at Thornvale Crossing. Any thoughts?",
        "The commute is walking because of the office.",
    )
    node = _node("source", "The office is in Thornvale Crossing", turns)
    alignment = align_node_to_raw_turn("episode", node)
    assert alignment["status"] == "aligned"
    assert alignment["method"] == "token-overlap"
    assert alignment["turn_index"] == 0


def test_graph_has_no_self_loop_or_cycle() -> None:
    turns = _turns("a", "b", "c", "d")
    result = generate_turn_candidates(
        "episode",
        [_node(value, value, turns) for value in ("a", "b", "c", "d")],
    )
    audit = audit_candidate_invariants([result])
    assert audit["self_loop_count"] == 0
    assert audit["directed_cycle_or_nontrivial_scc_count"] == 0


def test_audit_distinguishes_same_turn_from_future_to_past() -> None:
    candidate = {
        "source_evidence_id": "source",
        "target_evidence_id": "target",
        "source_turn_index": 2,
        "target_turn_index": 2,
        "source_span": {"turn_index": 2},
        "target_span": {"turn_index": 2},
    }
    audit = audit_candidate_invariants(
        [{"candidates": [candidate], "same_turn_unresolved": [], "alignments": []}]
    )
    assert audit["same_turn_directed_count"] == 1
    assert audit["future_to_past_count"] == 0

    candidate["source_turn_index"] = 3
    audit = audit_candidate_invariants(
        [{"candidates": [candidate], "same_turn_unresolved": [], "alignments": []}]
    )
    assert audit["same_turn_directed_count"] == 0
    assert audit["future_to_past_count"] == 1


def test_reason_clause_match_never_counts_as_gold_source_recovery() -> None:
    turns = _turns(
        "A downstream reason clause mentions source-value.",
        "The reviewed target proposition.",
    )
    result = generate_turn_candidates(
        "episode",
        [
            _node("reason-only", "reason clause mentions source-value", turns),
            _node("target", "reviewed target proposition", turns),
        ],
    )
    decision = {
        "semantic_edge_id": "episode|source|target",
        "episode_id": "episode",
        "reviewed_valid_source_node_id": "independent-source-not-present",
        "reviewed_valid_target_node_id": "target",
    }
    manual = {
        "semantic_edge_id": "episode|source|target",
        "substring_only_source_match_node_ids": ["reason-only"],
    }
    scored = score_with_manual_gold([result], [decision], [manual])
    edge = scored["edge_results"][0]
    assert edge["recovered"] is False
    assert edge["substring_only_outgoing_candidate_count"] == 1
    assert edge["substring_only_matches_counted_as_recovery"] == 0


def test_generator_ignores_gold_and_manual_decorations() -> None:
    turns = _turns("source", "target")
    nodes = [_node("s", "source", turns), _node("t", "target", turns)]
    baseline = generate_turn_candidates("episode", nodes)
    decorated = copy.deepcopy(nodes)
    decorated[0].update(
        {
            "gold_entity": "forbidden",
            "manual_label": "yes",
            "extractor_array_index": 999,
        }
    )
    observed = generate_turn_candidates("episode", decorated)
    assert baseline == observed


def test_zero_history_baseline_reports_absolute_delta_without_fake_multiplier() -> None:
    turns = _turns("source", "target")
    nodes = [_node("s", "source", turns), _node("t", "target", turns)]
    result = generate_turn_candidates("episode", nodes)
    evidence = {
        "episode_id": "episode",
        "candidate_traces": [
            {"node_id": "s", "candidate_snapshot_size": 0},
            {"node_id": "t", "candidate_snapshot_size": 0},
        ],
    }
    inflation = candidate_inflation([result], [evidence])
    assert inflation["absolute_delta"] == 1
    assert inflation["candidate_multiplier"] is None
    assert inflation["added_share_of_combined"] == 1.0


def test_frozen_development_sample_recovers_13_edges_in_9_episode_clusters() -> None:
    evidence = read_jsonl_tolerant(FROZEN / "case_evidence.jsonl")
    decisions = json.loads(
        (FROZEN / "manual_decisions_v2.json").read_text(encoding="utf-8")
    )["decisions"]
    manual = read_jsonl_tolerant(FROZEN / "manual_validation_v2.jsonl")
    results = [
        generate_turn_candidates(str(row["episode_id"]), row["nodes"])
        for row in evidence
    ]
    scored = score_with_manual_gold(results, decisions, manual)
    assert scored["semantic_edge_count"] == 13
    assert scored["semantic_edge_recovered_count"] == 13
    assert scored["episode_count"] == 9
    assert scored["episode_all_edges_recovered_count"] == 9
    audit = audit_candidate_invariants(results)
    assert audit["future_to_past_count"] == 0
    assert audit["same_turn_directed_count"] == 0
    assert audit["self_loop_count"] == 0
    assert audit["directed_cycle_or_nontrivial_scc_count"] == 0
