"""Deterministic offline aggregation for the legacy MEME oracle-layer audit.

Case labels live in an immutable decision artifact.  This module only validates
and joins frozen evidence; it never infers labels from benchmark identities.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

STAGES = (
    "extract/state-model",
    "P1 graph",
    "P2 enqueue/execution",
    "P2 semantic verdict",
    "stale-content isolation",
    "retrieval",
    "answer",
    "judge/scoring",
    "unknown/insufficient artifact",
)
STRENGTHS = {"confirmed", "likely", "unknown"}
ORACLE_VERDICTS = {"necessary_opportunity", "sufficient_from_existing_evidence", "unknown"}


class AuditError(RuntimeError):
    pass


def canonical_bytes(value: Any, *, exclude: Iterable[str] = ()) -> bytes:
    excluded = set(exclude)

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: clean(v) for k, v in sorted(item.items()) if k not in excluded}
        if isinstance(item, list):
            return [clean(v) for v in item]
        return item

    return json.dumps(clean(value), ensure_ascii=False, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any, *, exclude: Iterable[str] = ()) -> str:
    return hashlib.sha256(canonical_bytes(value, exclude=exclude)).hexdigest()


def old_taxonomy(case: Mapping[str, Any]) -> str:
    """Reproduce the published mutually-exclusive A-G ordering."""
    if not case.get("before_ok"):
        return "A"
    if not case.get("gate_events"):
        return "B"
    if case.get("edge_missed"):
        return "C"
    if all(event.get("final") == "fresh" for event in case.get("gate_events", ())):
        return "D"
    if case.get("on_leak_notes"):
        return "E"
    if case.get("on_answer") == case.get("off_answer"):
        return "F"
    return "G"


def load_failures(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for case in json.loads(path.read_text(encoding="utf-8")):
            # Published taxonomy uses the stricter MEME trivial-pass endpoint.
            if case.get("on_trivial_pass") is False:
                row = dict(case)
                row["case_id"] = f"{case['episode_id']}|hop{case['hop']}|{case['target_entity']}"
                row["old_taxonomy"] = old_taxonomy(case)
                rows.append(row)
    if len(rows) != 37:
        raise AuditError(f"expected 37 failures, found {len(rows)}")
    ids = [row["case_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise AuditError("case identity is not unique")
    return rows


def validate_decisions(artifact: Mapping[str, Any], failures: list[Mapping[str, Any]]) -> list[dict]:
    if artifact.get("schema_version") != "meme-oracle-layer-decisions-v1":
        raise AuditError("unsupported decision schema")
    if artifact.get("immutable") is not True:
        raise AuditError("decisions must declare immutable=true")
    rows = list(artifact.get("decisions") or ())
    expected = {row["case_id"] for row in failures}
    actual = {row.get("case_id") for row in rows}
    if len(rows) != 37 or actual != expected:
        raise AuditError("decision ledger must exactly cover the 37 failures")
    required = {
        "case_id", "old_primary", "earliest_stage", "secondary_stages",
        "evidence_strength", "evidence_fields", "oracle_assessments",
        "rationale", "paid_followup_required", "v3_p1_effect",
    }
    for row in rows:
        if required - set(row):
            raise AuditError(f"incomplete decision {row.get('case_id')}")
        if row["earliest_stage"] not in STAGES:
            raise AuditError("invalid earliest stage")
        if row["evidence_strength"] not in STRENGTHS:
            raise AuditError("invalid evidence strength")
        if not set(row["secondary_stages"]) <= set(STAGES):
            raise AuditError("invalid secondary stage")
        for assessment in row["oracle_assessments"]:
            if assessment.get("stage") not in STAGES or assessment.get("verdict") not in ORACLE_VERDICTS:
                raise AuditError("invalid oracle assessment")
        necessary = {a["stage"] for a in row["oracle_assessments"] if a["verdict"] == "necessary_opportunity"}
        sufficient = {a["stage"] for a in row["oracle_assessments"] if a["verdict"] == "sufficient_from_existing_evidence"}
        if sufficient - necessary:
            raise AuditError("sufficient evidence must also be a necessary opportunity")
    expected_hash = canonical_sha256(artifact, exclude=("decisions_canonical_sha256",))
    if artifact.get("decisions_canonical_sha256") != expected_hash:
        raise AuditError("decision canonical hash mismatch")
    by_id = {row["case_id"]: row for row in failures}
    for row in rows:
        if row["old_primary"] != by_id[row["case_id"]]["old_taxonomy"]:
            raise AuditError("old taxonomy binding mismatch")
    return rows


def evidence_projection(case: Mapping[str, Any]) -> dict[str, Any]:
    """Bind all evidence available in the frozen E2E case without embellishment."""
    wanted = (
        "episode_id", "hop", "domain", "target_entity", "before_question",
        "after_question", "before_gold", "gold_answer", "before_answer",
        "off_answer", "on_answer", "before_ok", "off_after_ok", "on_after_ok",
        "gate_events", "edge_missed", "on_leak_notes", "retrieved_before",
        "retrieved_off", "retrieved_on", "timings", "tokens", "oracle_calls",
        "edge_n_gold", "edge_n_pred", "edge_n_tp", "edge_precision", "edge_recall",
    )
    result = {key: case.get(key) for key in wanted}
    result["artifact_limitations"] = {
        "raw_dialogue_text": "absent_from_cases",
        "change_text": "absent_from_cases",
        "old_graph_edge_text_or_path": "only edge_missed summaries and aggregate counts retained",
        "retrieved_node_content": "only URI lists retained",
        "judge_input_output": "booleans retained; prompt/verdict text absent",
        "pending_execution_checkpoint": "gate_events and timings retained; durable queue state absent",
    }
    return result


def aggregate(decisions: list[Mapping[str, Any]]) -> dict[str, Any]:
    earliest: dict[str, Counter] = {"hop1": Counter(), "hop2": Counter()}
    ids: dict[str, dict[str, list[str]]] = {"hop1": defaultdict(list), "hop2": defaultdict(list)}
    opportunities: dict[str, Counter] = {stage: Counter() for stage in STAGES}
    for row in decisions:
        hop = "hop1" if "|hop1|" in row["case_id"] else "hop2"
        earliest[hop][row["earliest_stage"]] += 1
        ids[hop][row["earliest_stage"]].append(row["case_id"])
        seen: set[tuple[str, str]] = set()
        for assessment in row["oracle_assessments"]:
            key = (assessment["stage"], assessment["verdict"])
            if key not in seen:
                opportunities[assessment["stage"]][assessment["verdict"]] += 1
                opportunities[assessment["stage"]][
                    f"{assessment.get('strength', 'unknown')}_{assessment['verdict']}"
                ] += 1
                seen.add(key)
    return {
        "earliest_stage": {
            hop: {
                stage: {"count": earliest[hop][stage], "case_ids": sorted(ids[hop][stage])}
                for stage in STAGES if earliest[hop][stage]
            }
            for hop in ("hop1", "hop2")
        },
        "multi_label_oracle_opportunities_non_additive": {
            stage: {
                "confirmed_necessary_opportunity": counts["confirmed_necessary_opportunity"],
                "likely_opportunity": sum(
                    value for key, value in counts.items() if key.startswith("likely_")
                ),
                "sufficient_from_existing_evidence": counts["sufficient_from_existing_evidence"],
                "unknown": sum(
                    value for key, value in counts.items() if key.startswith("unknown_")
                ),
                "theoretical_max_cases_with_any_recorded_opportunity": sum(
                    value for key, value in counts.items()
                    if key in {"necessary_opportunity", "unknown"}
                ),
            }
            for stage, counts in opportunities.items()
        },
    }

