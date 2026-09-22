from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.memebench.gold_edge_audit import (
    AUDIT_SCHEMA_VERSION,
    append_jsonl,
    atomic_write_json,
    canonical_json_bytes,
    canonical_sha256,
    classify_gold_edge,
    completeness_report,
    formal_path_hashes,
    last_complete_successes,
    manifest_sha256,
    map_gold_entities_to_nodes,
    read_jsonl_tolerant,
    reclassify_distinct_same_session_records,
    recovery_events_for_stale,
    stale_attempts,
    summarize_audit,
    validate_checkpoint_config,
    validate_manifest_hash,
    validate_selection_evaluation_disjoint,
    verify_formal_artifact_hashes,
)
from integrations.memebench.run_gold_edge_audit import (
    FatalAuditError,
    _error_status,
    _trace_with_actual_persistence,
    audit_account,
    bind_run_config,
    case_key,
)


def _node(node_id: str, text: str, session: int) -> dict:
    return {
        "node_id": node_id,
        "text": text,
        "session_index": session,
        "embedding_present": True,
    }


def _trace(
    target: str,
    session: int,
    *,
    snapshot=(),
    hmax=(),
    routed=(),
    selected=(),
    persisted=(),
) -> dict:
    return {
        "node_id": target,
        "session_index": session,
        "candidate_snapshot_ids": list(snapshot),
        "hmax_candidate_ids": list(hmax),
        "routed_candidate_ids": list(routed),
        "final_selected_source_ids": list(selected),
        "persisted_source_ids": list(persisted),
        "candidate_tier": "full",
        "edge_tier": "cheap",
    }


def _classify(
    nodes: list[dict],
    traces: list[dict],
    *,
    source_value: str = "Alice",
    target_value: str = "Carol",
) -> dict:
    return classify_gold_edge(
        case_key="case",
        episode_id="ep",
        hop=1,
        target_entity="report_owner",
        policy="R_full_cheap",
        gold_source_entity="team_lead",
        gold_target_entity="report_owner",
        source_before_value=source_value,
        target_before_value=target_value,
        nodes=nodes,
        route_traces=traces,
    )


@pytest.mark.parametrize(
    ("nodes", "traces", "stage", "counts"),
    [
        ([], [], "extraction_both", (0, 0, 0, 0, 0, 0, 0)),
        (
            [_node("t", "Carol owns the report", 1)],
            [],
            "extraction_source",
            (0, 0, 0, 0, 0, 0, 0),
        ),
        (
            [_node("s", "Alice leads", 0)],
            [],
            "extraction_target",
            (0, 0, 0, 0, 0, 0, 0),
        ),
        (
            [
                _node("s", "Alice leads", 1),
                _node("t", "Carol owns the report", 1),
            ],
            [],
            "same_session_atomic_boundary",
            (1, 0, 0, 0, 0, 0, 0),
        ),
        (
            [
                _node("s", "Alice leads", 0),
                _node("t", "Carol owns the report", 1),
            ],
            [_trace("t", 1)],
            "arrival_snapshot",
            (1, 1, 0, 0, 0, 0, 0),
        ),
        (
            [
                _node("s", "Alice leads", 0),
                _node("t", "Carol owns the report", 1),
            ],
            [_trace("t", 1, snapshot=("s",))],
            "hmax_envelope",
            (1, 1, 1, 0, 0, 0, 0),
        ),
        (
            [
                _node("s", "Alice leads", 0),
                _node("t", "Carol owns the report", 1),
            ],
            [_trace("t", 1, snapshot=("s",), hmax=("s",))],
            "candidate_routing",
            (1, 1, 1, 1, 0, 0, 0),
        ),
        (
            [
                _node("s", "Alice leads", 0),
                _node("t", "Carol owns the report", 1),
            ],
            [_trace("t", 1, snapshot=("s",), hmax=("s",), routed=("s",))],
            "edge_discovery",
            (1, 1, 1, 1, 1, 0, 0),
        ),
        (
            [
                _node("s", "Alice leads", 0),
                _node("t", "Carol owns the report", 1),
            ],
            [
                _trace(
                    "t",
                    1,
                    snapshot=("s",),
                    hmax=("s",),
                    routed=("s",),
                    selected=("s",),
                )
            ],
            "persistence",
            (1, 1, 1, 1, 1, 1, 0),
        ),
        (
            [
                _node("s", "Alice leads", 0),
                _node("t", "Carol owns the report", 1),
            ],
            [
                _trace(
                    "t",
                    1,
                    snapshot=("s",),
                    hmax=("s",),
                    routed=("s",),
                    selected=("s",),
                    persisted=("s",),
                )
            ],
            "none",
            (1, 1, 1, 1, 1, 1, 1),
        ),
    ],
)
def test_stage_classifier_ladder(nodes, traces, stage, counts) -> None:
    record = _classify(nodes, traces)
    assert record["audit_schema_version"] == AUDIT_SCHEMA_VERSION
    assert record["first_failure_stage"] == stage
    assert record["gold_edge_recalled"] is (stage == "none")
    assert tuple(
        record[name]
        for name in (
            "n_pairs_after_extraction",
            "n_pairs_after_temporal_boundary",
            "n_pairs_after_snapshot",
            "n_pairs_after_hmax",
            "n_pairs_after_routing",
            "n_pairs_after_selection",
            "n_pairs_persisted",
        )
    ) == counts


def test_mapping_tiers_preserve_all_nodes_and_detect_many_to_many() -> None:
    nodes = [
        _node("exact", "The owner is Alice Smith.", 0),
        _node("normalized", "the owner is alice   smith.", 1),
        _node("shared", "Alice Smith and Carol", 2),
    ]
    mappings = map_gold_entities_to_nodes(
        {"lead": {"before": "Alice Smith"}, "owner": {"before": "Carol"}},
        nodes,
    )
    # Exact/raw substring wins globally, so normalized/fuzzy cannot overwrite it.
    assert mappings["lead"].match_method == "exact"
    assert mappings["lead"].node_ids == ("exact", "shared")
    assert mappings["lead"].all_session_indices == (0, 2)
    assert mappings["lead"].mapping_ambiguous
    assert mappings["owner"].node_ids == ("shared",)
    assert mappings["owner"].mapping_ambiguous

    normalized = map_gold_entities_to_nodes(
        {"lead": {"before": "ALICE SMITH"}}, nodes
    )["lead"]
    assert normalized.match_method == "normalized"
    assert set(normalized.node_ids) == {"exact", "normalized", "shared"}

    fuzzy = map_gold_entities_to_nodes(
        {"lead": {"before": "Alyce Smith"}}, nodes
    )["lead"]
    assert fuzzy.match_method == "fuzzy"
    assert fuzzy.node_ids


def test_multi_source_any_success_uses_existential_semantics() -> None:
    nodes = [
        _node("s0", "Alice leads", 0),
        _node("s1", "Alice repeated", 0),
        _node("t", "Carol owns the report", 1),
    ]
    record = _classify(
        nodes,
        [
            _trace(
                "t",
                1,
                snapshot=("s0", "s1"),
                hmax=("s0", "s1"),
                routed=("s0", "s1"),
                selected=("s1",),
                persisted=("s1",),
            )
        ],
    )
    assert record["mapping_ambiguous"]
    assert record["first_failure_stage"] == "none"
    assert record["n_pairs_after_selection"] == 1


def test_multi_target_existential_semantics() -> None:
    nodes = [
        _node("s", "Alice leads", 0),
        _node("t0", "Carol owns old report", 1),
        _node("t1", "Carol owns new report", 2),
    ]
    record = _classify(
        nodes,
        [
            _trace("t0", 1),
            _trace(
                "t1",
                2,
                snapshot=("s",),
                hmax=("s",),
                routed=("s",),
                selected=("s",),
                persisted=("s",),
            ),
        ],
    )
    assert record["first_failure_stage"] == "none"
    assert record["n_pairs_after_extraction"] == 2
    assert record["n_pairs_persisted"] == 1


def test_earlier_duplicate_crosses_same_session_boundary() -> None:
    nodes = [
        _node("earlier", "Alice leads", 0),
        _node("same", "Alice repeated", 1),
        _node("target", "Carol owns report", 1),
    ]
    record = _classify(nodes, [_trace("target", 1)])
    assert record["earlier_source_duplicate_exists"]
    assert not record["same_session_only"]
    assert record["first_failure_stage"] == "arrival_snapshot"


def test_temporal_inversion_with_ambiguous_mapping_is_explicit() -> None:
    nodes = [
        _node("target", "Carol owns report", 0),
        _node("source1", "Alice leads", 1),
        _node("source2", "Alice repeated", 2),
    ]
    record = _classify(nodes, [])
    assert record["mapping_ambiguous"]
    assert record["first_failure_stage"] == "ambiguous"


def test_one_node_claimed_by_both_entities_is_ambiguous() -> None:
    record = _classify(
        [_node("shared", "Alice handed the report to Carol", 0)],
        [],
    )
    assert record["mapping_ambiguous"]
    assert record["source_target_node_identity_overlap"]
    assert record["first_failure_stage"] == "ambiguous"


def test_shared_identity_with_distinct_same_session_pair_is_boundary() -> None:
    record = _classify(
        [
            _node("source", "Alice leads", 1),
            _node("shared", "Alice assigned Carol", 1),
        ],
        [],
    )
    assert record["mapping_ambiguous"]
    assert record["source_target_node_identity_overlap"]
    assert record["first_failure_stage"] == "same_session_atomic_boundary"


def test_formal_hash_mapping_and_mismatch(tmp_path: Path) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    (formal / "a.txt").write_text("a", encoding="utf-8")
    nested = formal / "nested"
    nested.mkdir()
    (nested / "b.txt").write_text("b", encoding="utf-8")
    before = formal_path_hashes(formal)
    assert list(before) == ["a.txt", "nested/b.txt"]
    verify_formal_artifact_hashes(before, dict(before))
    (formal / "a.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="formal artifact contamination"):
        verify_formal_artifact_hashes(before, formal_path_hashes(formal))


def test_overlap_and_manifest_hash_guards() -> None:
    validate_selection_evaluation_disjoint(["a"], ["b"])
    with pytest.raises(ValueError, match="overlap"):
        validate_selection_evaluation_disjoint(["a", "b"], ["b", "c"])
    manifest = {"manifest_version": "v1", "selected": ["a"]}
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    assert validate_manifest_hash(manifest) == manifest["manifest_sha256"]
    manifest["selected"].append("b")
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        validate_manifest_hash(manifest)
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_atomic_json_jsonl_fsync_and_torn_last_line(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "integrations.memebench.gold_edge_audit.os.fsync",
        lambda fd: calls.append(fd),
    )
    output = tmp_path / "out.json"
    atomic_write_json(output, {"ok": True})
    assert json.loads(output.read_text(encoding="utf-8")) == {"ok": True}
    journal = tmp_path / "events.jsonl"
    append_jsonl(journal, {"n": 1})
    append_jsonl(journal, {"n": 2})
    with journal.open("ab") as handle:
        handle.write(b'{"n":')
    assert read_jsonl_tolerant(journal) == [{"n": 1}, {"n": 2}]
    assert len(calls) == 3


def _audit_record(
    case_key: str,
    attempt_id: str,
    *,
    edge_id: str = "edge",
    stage: str = "none",
    config: str = "cfg",
) -> dict:
    return {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "case_key": case_key,
        "attempt_id": attempt_id,
        "run_config_hash": config,
        "episode_id": "ep",
        "hop": 1,
        "target_entity": "target",
        "policy": "policy",
        "gold_edge_id": edge_id,
        "gold_source_entity": "source",
        "gold_target_entity": "target",
        "first_failure_stage": stage,
        "gold_edge_recalled": stage == "none",
        "n_pairs_after_extraction": 1,
        "n_pairs_after_temporal_boundary": 1,
        "n_pairs_after_snapshot": 1,
        "n_pairs_after_hmax": 1,
        "n_pairs_after_routing": 1,
        "n_pairs_after_selection": 1,
        "n_pairs_persisted": 1 if stage == "none" else 0,
    }


def _started(case: str, attempt: str, config: str = "cfg") -> dict:
    return {
        "event": "attempt_started",
        "case_key": case,
        "attempt_id": attempt,
        "attempt_number": 1,
        "run_config_hash": config,
    }


def _finished(
    case: str, attempt: str, status: str = "success", config: str = "cfg"
) -> dict:
    return {
        "event": "attempt_finished",
        "case_key": case,
        "attempt_id": attempt,
        "attempt_number": 1,
        "run_config_hash": config,
        "status": status,
    }


def test_journal_stale_retryable_and_config_mismatch_are_not_done() -> None:
    journal = [
        _started("stale", "a"),
        _started("retry", "b"),
        _finished("retry", "b", "retryable_error"),
        _started("wrong", "c", "old"),
        _finished("wrong", "c", config="old"),
    ]
    assert [item["attempt_id"] for item in stale_attempts(journal, run_config_hash="cfg")] == [
        "a"
    ]
    recovery = recovery_events_for_stale(journal, run_config_hash="cfg")
    assert recovery[0]["status"] == "retryable_error"
    assert recovery[0]["cost_incomplete"]
    report = completeness_report(
        ["stale", "retry", "wrong"],
        journal,
        [_audit_record("wrong", "c", config="old")],
        run_config_hash="cfg",
        expected_gold_edges_by_case={"stale": 1, "retry": 1, "wrong": 1},
    )
    assert report["success_cases"] == 0
    assert report["retryable_case_keys"] == ["retry"]
    assert report["stale_cases"] == 1
    with pytest.raises(ValueError, match="run config hash mismatch"):
        validate_checkpoint_config(journal, run_config_hash="cfg")


def test_last_complete_success_skips_missing_record_and_uses_later_complete() -> None:
    journal = [
        _started("case", "first"),
        _finished("case", "first"),
        _started("case", "missing"),
        _finished("case", "missing"),
        _started("case", "last"),
        _finished("case", "last"),
    ]
    records = [
        _audit_record("case", "first", edge_id="edge-1"),
        _audit_record("case", "first", edge_id="edge-2"),
        _audit_record("case", "missing", edge_id="edge-1"),
        _audit_record("case", "last", edge_id="edge-1", stage="edge_discovery"),
        _audit_record("case", "last", edge_id="edge-2"),
    ]
    successes = last_complete_successes(
        journal,
        records,
        run_config_hash="cfg",
        expected_gold_edges_by_case={"case": 2},
    )
    assert successes["case"]["attempt"]["attempt_id"] == "last"
    assert {item["gold_edge_id"] for item in successes["case"]["records"]} == {
        "edge-1",
        "edge-2",
    }
    summary = summarize_audit(successes["case"]["records"])
    assert summary["gold_edge_total"] == 2
    assert summary["stage_counts"] == {"edge_discovery": 1, "none": 1}
    assert summary["mapping_ambiguous_edges"] == 0


def test_success_requires_cleanup_record_when_case_success_journal_is_present() -> None:
    journal = [_started("case", "a"), _finished("case", "a")]
    record = _audit_record("case", "a")
    result = last_complete_successes(
        journal,
        [record],
        run_config_hash="cfg",
        expected_gold_edges_by_case={"case": 1},
        case_success_records=[
            {
                "case_key": "case",
                "attempt_id": "a",
                "run_config_hash": "cfg",
                "account_cleanup_complete": False,
            }
        ],
    )
    assert result == {}


def test_audit_account_namespace_cannot_collide_with_formal_accounts() -> None:
    account = audit_account(1, "R_full_cheap", "pl_001", "medication")
    assert account.startswith("p1audit-v1-")
    assert not account.startswith(("ch1", "ch2"))
    assert account == audit_account(1, "R_full_cheap", "pl_001", "medication")
    assert account != audit_account(2, "R_full_cheap", "pl_001", "medication")
    assert (
        case_key(1, "pl_001", "medication", "R_full_cheap")
        != case_key(2, "pl_001", "medication", "R_full_cheap")
    )


def test_retryable_and_fatal_errors_are_distinguished() -> None:
    assert _error_status(TimeoutError("slow")) == ("retryable_error", True)
    assert _error_status(ConnectionError("connection reset")) == (
        "retryable_error",
        True,
    )
    assert _error_status(ValueError("schema validation error")) == (
        "fatal_error",
        False,
    )
    assert _error_status(RuntimeError("HTTP 401 unauthorized")) == (
        "fatal_error",
        False,
    )


def test_run_config_change_and_frozen_plan_change_refuse_resume(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    first = {"run_config_hash": "one", "plan_hashes": {"R_full_cheap": "a"}}
    assert bind_run_config(run_dir, first) == "one"
    assert bind_run_config(run_dir, first) == "one"
    with pytest.raises(FatalAuditError, match="run_config hash mismatch"):
        bind_run_config(
            run_dir,
            {"run_config_hash": "two", "plan_hashes": {"R_full_cheap": "b"}},
        )

    unbound = tmp_path / "unbound"
    unbound.mkdir()
    (unbound / "attempts.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(FatalAuditError, match="unbound"):
        bind_run_config(unbound, first)


def test_actual_persistence_trace_requires_nested_sets() -> None:
    trace = _trace("target", 1, snapshot=("source",), hmax=("source",))
    trace["routed_candidate_ids"] = ["source"]
    trace["final_selected_source_ids"] = ["source"]
    merged = _trace_with_actual_persistence(
        [trace], {"target": ["source"]}
    )
    assert merged[0]["persisted_source_ids"] == ["source"]

    broken = dict(trace)
    broken["routed_candidate_ids"] = ["outside"]
    with pytest.raises(FatalAuditError, match="envelope invariant"):
        _trace_with_actual_persistence([broken], {})


def test_checkpoint_requires_exact_gold_edge_identity_hash_and_case_metadata() -> None:
    journal = [_started("case", "a"), _finished("case", "a")]
    records = [
        _audit_record("case", "a", edge_id="edge-1"),
        _audit_record("case", "a", edge_id="edge-2"),
    ]
    expected_ids = {"case": {"edge-1", "edge-2"}}
    expected_hashes = {"case": canonical_sha256(["edge-1", "edge-2"])}
    metadata = {
        "case": {
            "episode_id": "ep",
            "hop": 1,
            "policy": "policy",
            "target_entity": "target",
        }
    }
    assert last_complete_successes(
        journal,
        records,
        run_config_hash="cfg",
        expected_gold_edges_by_case={"case": 2},
        expected_gold_edge_ids_by_case=expected_ids,
        expected_gold_edge_ids_hash_by_case=expected_hashes,
        expected_case_metadata=metadata,
    )
    records[0]["gold_edge_id"] = "wrong"
    assert not last_complete_successes(
        journal,
        records,
        run_config_hash="cfg",
        expected_gold_edges_by_case={"case": 2},
        expected_gold_edge_ids_by_case=expected_ids,
        expected_gold_edge_ids_hash_by_case=expected_hashes,
        expected_case_metadata=metadata,
    )


def test_reclassification_rebuilds_frozen_v11_bytes() -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "integrations/memebench/runs/p1_gold_edge_audit_20260822"
    )
    rebuilt = reclassify_distinct_same_session_records(
        read_jsonl_tolerant(root / "gold_edge_audit.jsonl")
    )
    rendered = b"".join(canonical_json_bytes(row) + b"\n" for row in rebuilt)
    assert rendered == (root / "gold_edge_audit_reclassified.jsonl").read_bytes()


def test_stage_and_oracle_episode_counts_are_hop_aware() -> None:
    hop1 = _audit_record("c1", "a", edge_id="e1", stage="edge_discovery")
    hop2 = _audit_record("c2", "b", edge_id="e2", stage="edge_discovery")
    hop2["hop"] = 2
    summary = summarize_audit([hop1, hop2])
    assert summary["stage_episode_counts"]["edge_discovery"] == 2
    assert (
        summary["oracle_repair_upper_bounds"]["edge_discovery"][
            "max_recoverable_episodes"
        ]
        == 2
    )
