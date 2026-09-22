from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from integrations.memebench.oracle_layer_audit import (
    AuditError,
    STAGES,
    aggregate,
    canonical_sha256,
    evidence_projection,
    load_failures,
    validate_decisions,
)
from integrations.memebench.run_oracle_layer_audit import DECISIONS, INPUTS


def failures() -> list[dict]:
    return load_failures(INPUTS[:2])


def ledger() -> dict:
    return json.loads(DECISIONS.read_text(encoding="utf-8"))


def test_exact_37_case_coverage_uniqueness_and_hop_counts() -> None:
    rows = failures()
    assert len(rows) == 37
    assert len({row["case_id"] for row in rows}) == 37
    assert Counter(row["hop"] for row in rows) == {1: 28, 2: 9}


def test_repeated_episodes_are_distinct_hop_case_identities() -> None:
    rows = failures()
    assert len({row["episode_id"] for row in rows}) < 37
    assert len({(row["episode_id"], row["hop"], row["target_entity"]) for row in rows}) == 37


def test_stage_enum_and_published_old_taxonomy_counts() -> None:
    rows = validate_decisions(ledger(), failures())
    assert all(row["earliest_stage"] in STAGES for row in rows)
    assert Counter(row["old_primary"] for row in rows) == {
        "A": 11, "B": 4, "C": 6, "D": 1, "E": 4, "F": 2, "G": 9,
    }


def test_necessary_and_sufficient_cannot_be_conflated() -> None:
    bad = ledger()
    bad["decisions"][0]["oracle_assessments"] = [{
        "stage": "answer",
        "verdict": "sufficient_from_existing_evidence",
        "strength": "confirmed",
    }]
    bad["decisions_canonical_sha256"] = canonical_sha256(
        bad, exclude=("decisions_canonical_sha256",)
    )
    with pytest.raises(AuditError, match="sufficient"):
        validate_decisions(bad, failures())


def test_multi_label_opportunities_are_not_summed_as_failure_total() -> None:
    rows = validate_decisions(ledger(), failures())
    summary = aggregate(rows)
    totals = [
        value["theoretical_max_cases_with_any_recorded_opportunity"]
        for value in summary["multi_label_oracle_opportunities_non_additive"].values()
    ]
    assert sum(totals) > 37
    assert sum(v["count"] for v in summary["earliest_stage"]["hop1"].values()) == 28
    assert sum(v["count"] for v in summary["earliest_stage"]["hop2"].values()) == 9


def test_v3_edge_path_join_marks_all_six_legacy_c_cases_restored() -> None:
    rows = validate_decisions(ledger(), failures())
    c_rows = [row for row in rows if row["old_primary"] == "C"]
    assert len(c_rows) == 6
    assert {row["v3_p1_effect"] for row in c_rows} == {
        "necessary_graph_path_restored_not_answer_proven"
    }


def test_retrieval_evidence_parser_preserves_uri_lists_and_limitations() -> None:
    projected = evidence_projection(failures()[0])
    assert isinstance(projected["retrieved_on"], list)
    assert projected["artifact_limitations"]["retrieved_node_content"] == "only URI lists retained"


def test_unknown_is_preserved_for_f_and_g_cases() -> None:
    rows = validate_decisions(ledger(), failures())
    unresolved = [row for row in rows if row["old_primary"] in {"F", "G"}]
    assert len(unresolved) == 11
    assert all(row["earliest_stage"] == "unknown/insufficient artifact" for row in unresolved)


def test_input_hashes_unchanged_by_loading_and_validation() -> None:
    before = {path: path.read_bytes() for path in INPUTS}
    validate_decisions(ledger(), failures())
    assert all(path.read_bytes() == content for path, content in before.items())


def test_decision_tampering_is_rejected() -> None:
    bad = copy.deepcopy(ledger())
    bad["decisions"][0]["rationale"] = "tampered"
    with pytest.raises(AuditError, match="canonical hash"):
        validate_decisions(bad, failures())

