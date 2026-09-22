"""Fail-closed harness for the legacy MEME paid layer-oracle experiment.

This module is benchmark-only.  It materializes preflight evidence and refuses
paid work when the historical state needed for a strict one-layer intervention
is absent.  Production runtime code is never imported or modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class PreflightError(RuntimeError):
    pass


RETRIEVAL_CASES = (
    "pl_004|hop1|travel_plan", "pl_008|hop1|work_project",
    "pl_015|hop1|living_arrangement", "pl_034|hop1|exercise_routine",
    "pl_037|hop1|family_event", "pl_038|hop1|travel_plan",
    "pl_042|hop1|exercise_routine", "pl_045|hop1|travel_plan",
    "sw_044|hop1|test_framework", "sw_047|hop1|auth_method",
    "pl_042|hop2|fitness_facility",
)
P2_EXECUTION_CASES = (
    "pl_018|hop1|commute_method", "pl_039|hop1|skill_acquisition",
    "pl_043|hop1|travel_plan", "sw_038|hop2|test_command",
)
P2_VERDICT_CASES = ("pl_022|hop1|financial_goal",)
STATE_CASES = (
    "pl_006|hop1|regular_appointment", "pl_021|hop1|work_schedule",
    "pl_011|hop2|fitness_facility", "pl_040|hop2|fitness_facility",
    "sw_007|hop2|model_syntax", "sw_018|hop2|model_syntax",
    "sw_027|hop2|model_syntax", "sw_029|hop2|model_syntax",
)
JUDGE_CASES = (
    "sw_023|hop1|approval_authority", "sw_026|hop1|project_structure",
    "sw_038|hop1|project_structure",
)

REQUIRED_SUCCESS_FIELDS = {
    "case_id", "stage", "input_sha256", "response", "attempts", "tokens",
    "cost_usd", "answer", "score",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exact_case_manifest(ledger: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    ledger_ids = {row["case_id"] for row in ledger["decisions"]}
    groups = {
        "stage0_retrieval": RETRIEVAL_CASES,
        "stage3_p2_execution": P2_EXECUTION_CASES,
        "stage4_p2_verdict": P2_VERDICT_CASES,
        "stage5_state": STATE_CASES,
        "judge": JUDGE_CASES,
    }
    missing = {case for cases in groups.values() for case in cases if case not in ledger_ids}
    if missing:
        raise PreflightError(f"case manifest not bound to ledger: {sorted(missing)}")
    return groups


def prompt_has_gold_leak(prompt: str, *, gold: str, before_gold: str | None = None) -> bool:
    folded = prompt.casefold()
    forbidden = [gold, before_gold, "gold answer", "reference answer", "correct option",
                 "expected conclusion", "错误case", "错误 case"]
    return any(str(item).strip().casefold() in folded for item in forbidden if item)


def assert_single_layer_change(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], *, allowed: Iterable[str]
) -> None:
    allowed_set = set(allowed)
    keys = set(baseline) | set(candidate)
    changed = {key for key in keys if baseline.get(key) != candidate.get(key)}
    if not changed or not changed <= allowed_set:
        raise PreflightError(
            f"single-layer isolation failed: changed={sorted(changed)}, allowed={sorted(allowed_set)}"
        )


def validate_success(record: Mapping[str, Any]) -> bool:
    if record.get("status") != "success" or REQUIRED_SUCCESS_FIELDS - set(record):
        return False
    tokens = record.get("tokens")
    return (
        isinstance(record.get("input_sha256"), str)
        and len(record["input_sha256"]) == 64
        and bool(record.get("response"))
        and isinstance(tokens, Mapping)
        and tokens.get("tokens_are_real") is True
        and isinstance(record.get("cost_usd"), (int, float))
    )


def merge_attempt_usage(attempts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(attempts)
    return {
        "calls": len(rows),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in rows),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in rows),
        "cost_usd": sum(float(row.get("cost_usd") or 0) for row in rows),
        "cost_complete": all(row.get("cost_complete") is True for row in rows),
        "retry_count": max(0, len(rows) - 1),
    }


def case_union(stage_results: Mapping[str, Iterable[Mapping[str, Any]]]) -> list[str]:
    return sorted({
        row["case_id"]
        for rows in stage_results.values()
        for row in rows
        if row.get("recovered") is True and row.get("status") == "success"
    })


@dataclass
class RunLock:
    path: Path
    stale_after_s: float = 900.0

    def acquire(self) -> dict[str, Any]:
        now = time.time()
        owner = {"pid": os.getpid(), "hostname": socket.gethostname(), "heartbeat": now}
        if self.path.exists():
            prior = json.loads(self.path.read_text(encoding="utf-8"))
            if now - float(prior.get("heartbeat", 0)) <= self.stale_after_s:
                raise PreflightError(f"active run lock: {prior}")
        self.path.write_text(json.dumps(owner, sort_keys=True) + "\n", encoding="utf-8")
        return owner

    def heartbeat(self) -> None:
        owner = json.loads(self.path.read_text(encoding="utf-8"))
        owner["heartbeat"] = time.time()
        self.path.write_text(json.dumps(owner, sort_keys=True) + "\n", encoding="utf-8")

    def release(self) -> None:
        if self.path.exists():
            self.path.unlink()


def load_checkpoint(path: Path, *, run_config_sha256: str) -> dict[tuple[str, str], dict]:
    done: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("run_config_sha256") != run_config_sha256:
            raise PreflightError("checkpoint belongs to another run config")
        if validate_success(row):
            done[(row["stage"], row["case_id"])] = row
    return done


def build_retrieval_manifest(
    cases_by_id: Mapping[str, Mapping[str, Any]],
    frozen_nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rows = []
    for case_id in RETRIEVAL_CASES:
        case = cases_by_id[case_id]
        uris = list(case.get("retrieved_on") or ())
        nodes = []
        missing = []
        for uri in uris:
            node = frozen_nodes.get(uri)
            if not node:
                missing.append(uri)
                continue
            if node.get("source") != "frozen_historical_artifact":
                raise PreflightError(f"oracle node is not frozen historical evidence: {uri}")
            nodes.append(dict(node))
        recoverable = len(nodes) == len(uris) and bool(uris)
        rows.append({
            "case_id": case_id,
            "retrieved_on": uris,
            "recovered_nodes": nodes,
            "missing_uris": missing,
            "recoverable": recoverable,
            "stage1_eligible": recoverable and bool(case.get("gold_answer")),
            "status": "recovered" if recoverable else "unrecoverable",
            "reason": None if recoverable else "no immutable historical node snapshot found",
        })
    return {
        "schema_version": "meme-retrieval-oracle-manifest-v1",
        "immutable": True,
        "cases": rows,
        "case_count": len(rows),
        "uri_count": sum(len(row["retrieved_on"]) for row in rows),
        "recovered_uri_count": sum(len(row["recovered_nodes"]) for row in rows),
        "eligible_case_count": sum(row["stage1_eligible"] for row in rows),
    }


def stage_gate_summary(retrieval_manifest: Mapping[str, Any]) -> dict[str, Any]:
    stage1 = [row["case_id"] for row in retrieval_manifest["cases"] if row["stage1_eligible"]]
    return {
        "stage0": {"eligible": 11, "run": 11, "success": len(stage1),
                   "failed": 0, "missing": 11 - len(stage1), "cost_incomplete": 0},
        "stage1_retrieval": {"eligible": len(stage1), "run": 0, "success": 0,
                             "failed": 0, "missing": len(stage1), "cost_incomplete": 0},
        "stage2_answer_only": {"eligible": 0, "run": 0, "success": 0,
                               "failed": 0, "missing": 0, "cost_incomplete": 0},
        "stage3_p2_execution": {"eligible": 0, "run": 0, "success": 0,
                                "failed": 0, "missing": 4, "cost_incomplete": 0},
        "stage4_p2_verdict": {"eligible": 0, "run": 0, "success": 0,
                              "failed": 0, "missing": 1, "cost_incomplete": 0},
        "stage5_state_model": {"eligible": 0, "run": 0, "success": 0,
                               "failed": 0, "missing": 8, "cost_incomplete": 0},
    }
