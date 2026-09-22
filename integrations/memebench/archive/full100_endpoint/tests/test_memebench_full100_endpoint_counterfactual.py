from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from integrations.memebench.full100_endpoint_counterfactual import (
    CounterfactualError,
    audit_alignment_invariants,
    generate_provenance_candidates,
    recompute_metrics,
    validate_ledger,
    verify_alignment_permutation,
)
from integrations.memebench.gold_edge_audit import canonical_sha256
from integrations.memebench.run_full100_endpoint_counterfactual import (
    LEDGER,
    RAW,
    _run_alignment_without_gold,
    _stage_manifest,
)


def _ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _turns(*values: tuple[str, str]) -> list[dict]:
    return [
        {
            "turn_index": index,
            "role": role,
            "content": content,
            "turn_sha256": f"turn-{index}",
        }
        for index, (role, content) in enumerate(values)
    ]


def _node(node_id: str, text: str, turns: list[dict], session: int = 0) -> dict:
    return {
        "node_id": node_id,
        "text": text,
        "session_index": session,
        "original_session_id": f"session-{session}",
        "original_turns": turns,
    }


def test_ledger_schema_uniqueness_hash_and_class_exclusivity() -> None:
    ledger = _ledger()
    rows = validate_ledger(ledger)
    assert len(rows) == 19
    assert len({(row["episode_id"], row["gold_identity"]) for row in rows}) == 19
    assert sum(row["decision_class"] == "confirmed_scoring_false_miss" for row in rows) == 10
    assert sum(row["decision_class"] == "true_candidate_envelope_miss" for row in rows) == 6
    assert sum(
        row["endpoint_verdict"] in {"unscorable", "unknown"} for row in rows
    ) == 3
    pl016 = next(row for row in rows if row["decision_id"] == "pl_016:0b2fd907")
    assert pl016["decision_class"] == "true_candidate_envelope_miss"
    assert "target_surface_mapping_mismatch" in pl016["secondary_flags"]


def test_ledger_tampering_and_duplicate_identity_are_rejected() -> None:
    tampered = _ledger()
    tampered["decisions"][0]["evidence_explanation"] = "tampered"
    with pytest.raises(CounterfactualError, match="content hash"):
        validate_ledger(tampered)
    duplicate = _ledger()
    duplicate["decisions"][1]["episode_id"] = duplicate["decisions"][0]["episode_id"]
    duplicate["decisions"][1]["gold_identity"] = duplicate["decisions"][0]["gold_identity"]
    duplicate["decisions_sha256"] = canonical_sha256(
        duplicate, exclude_fields=("decisions_sha256",)
    )
    with pytest.raises(CounterfactualError, match="duplicate"):
        validate_ledger(duplicate)


def test_hop_views_deduplicate_underlying_dependencies() -> None:
    rows = validate_ledger(_ledger())
    assert len(rows) == 19
    assert sum(1 in row["hop_views"] for row in rows) == 19
    assert sum(2 in row["hop_views"] for row in rows) == 12


def test_corrected_denominator_cp_and_epsilon_sensitivity() -> None:
    metrics = recompute_metrics(validate_ledger(_ledger()), raw=RAW)
    hop1 = metrics["by_hop_edge_and_episode"]["hop1"]
    assert hop1["raw_approximate"]["edge_recall"] == {
        "numerator": 314,
        "denominator": 333,
        "rate": pytest.approx(314 / 333),
    }
    corrected = hop1["corrected_observable_denominator"]
    assert corrected["edge_recall"]["numerator"] == 324
    assert corrected["edge_recall"]["denominator"] == 330
    primary = metrics["episode_cluster_unit"]["confirmed_pipeline_miss_all_100"]
    assert primary["numerator"] == 6
    assert primary["denominator"] == 100
    assert primary["rate"] == 0.06
    assert primary["clopper_pearson_upper"] == pytest.approx(0.1149852556)
    sensitivity = metrics["epsilon_sensitivity_primary_confirmed_pipeline_all_100"]
    assert [(row["point_estimate_pass"], row["cp_upper_pass"]) for row in sensitivity] == [
        (False, False),
        (True, False),
        (True, True),
    ]


def test_multihypothesis_alignment_preserves_temporal_safety_and_order() -> None:
    turns = _turns(
        ("assistant", "The user is single."),
        ("user", "I am single."),
        ("user", "My home is a condo because I am single."),
    )
    nodes = [
        _node("target", "My home is a condo because I am single.", turns),
        _node("source", "The user is single.", turns),
    ]
    result = generate_provenance_candidates("ep", nodes)
    assert result["alignments"][0]["status"] in {"aligned", "multi_candidate"}
    assert result["candidates"]
    audit = audit_alignment_invariants([result])
    assert audit["future_to_past_count"] == 0
    assert audit["same_turn_directed_count"] == 0
    assert audit["self_loop_count"] == 0
    assert audit["directed_cycle_or_nontrivial_scc_count"] == 0
    assert verify_alignment_permutation("ep", nodes) is True


def test_cross_session_history_candidate_is_temporally_allowed() -> None:
    source = _node(
        "source",
        "I work at Acme.",
        _turns(("user", "I work at Acme.")),
        session=0,
    )
    target = _node(
        "target",
        "My project depends on Acme.",
        _turns(("user", "My project depends on Acme.")),
        session=3,
    )
    rows = generate_provenance_candidates("ep", [target, source])["candidates"]
    assert any(
        row["source_node_id"] == "source" and row["target_node_id"] == "target"
        for row in rows
    )
    assert not any(
        row["source_node_id"] == "target" and row["target_node_id"] == "source"
        for row in rows
    )


def test_gold_isolation_runtime_stage_accepts_only_shared_shards() -> None:
    shard = {
        "episode_id": "ep",
        "nodes": [
            _node(
                "source",
                "source fact",
                _turns(("user", "source fact"), ("user", "target fact")),
            ),
            _node(
                "target",
                "target fact",
                _turns(("user", "source fact"), ("user", "target fact")),
            ),
        ],
        "selector_cases": [{"candidates": []}],
        "gold_scoring_side": {"forbidden": True},
        "adjudication": {"forbidden": True},
    }
    results, summary = _run_alignment_without_gold([shard], min_overlap=0.2)
    assert results[0]["candidates"]
    assert "gold sidecar" in summary["runtime_forbidden_inputs"]
    clean = copy.deepcopy(shard)
    clean.pop("gold_scoring_side")
    clean.pop("adjudication")
    assert _run_alignment_without_gold([clean], min_overlap=0.2)[0] == results


def test_stage_manifest_binds_hashes_and_code_fingerprint() -> None:
    manifest = _stage_manifest(
        "test",
        config_sha256="config",
        inputs={"input": "a"},
        outputs={"output": "b"},
        code_hashes={"code.py": "c"},
    )
    assert manifest["input_content_hashes"] == {"input": "a"}
    assert manifest["code_fingerprint"] == {"code.py": "c"}
    assert manifest["manifest_sha256"] == canonical_sha256(
        manifest, exclude_fields=("manifest_sha256",)
    )
