"""Run the six-target P1 full100 endpoint selector development diagnostic."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import platform
import socket
import sys
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from contexthub.llm.chat_client import BaseChatClient, OpenAIChatClient
from contexthub.services.dependency_discovery_service import (
    DependencyDiscoveryService,
    _DISCOVERY_PROMPT,
)
from integrations.memebench.full100_endpoint_counterfactual import (
    generate_provenance_candidates,
    validate_ledger,
)
from integrations.memebench.gold_edge_audit import (
    append_jsonl,
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
    utc_timestamp,
)
from integrations.memebench.run_chronological_p1p2 import package_versions
from integrations.memebench.same_session_selector_intervention import (
    AuditedChatClient,
    run_selector_case,
    selector_case_key,
)
from integrations.memebench.systems import DEFAULT_PROVIDERS_PATH, load_provider


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
SHARED = RUNS / "p1_same_session_selector_full100_shared_20260824"
OLD = RUNS / "p1_same_session_selector_full100_cheap_20260824"
V3 = RUNS / "p1_full100_offline_adjudication_counterfactual_20260824_v3"
LEDGER = ROOT / "integrations" / "memebench" / "adjudications" / (
    "p1_full100_endpoint_adjudication_v1.json"
)
DEFAULT_OUT = RUNS / "p1_full100_endpoint_selector_rerun_20260824_v1"
ARM = "turn_full_cheap"
MODEL = "gpt-4o-mini"
PROVIDER = "openlux"
PRICE = {"prompt": 0.15, "completion": 0.60}
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (5.0, 15.0, 45.0)
EXPECTED_V3_ARTIFACT_SHA256 = (
    "6444eb730153aceaaaa219f8f1e49ad83e6f54c7d548c52e04d8ffc24c9ddf76"
)
TARGET_NODE_BY_EPISODE = {
    "pl_001": "node-1945513c6307b0cae950d6f092e88849079db70b48ea634eea316dea1435386a",
    "pl_011": "node-dfa65737bcc755cb8d9349c6ad1a0936735e11345505897e218c35a9b984fcb1",
    "pl_016": "node-11fe7d7a7b3cbebe8200c064fbfe66c05cf7d08b536f4393b5d27399ac1cb42d",
    "pl_019": "node-bb8977664198598937051eb0588e748a6e24108a98dd72b4e4aae866e2234971",
    "pl_024": "node-5e232b7b17a254dccaa333d52630dc41d4f4461fad540f869fcce7174c68bbbf",
    "pl_049": "node-41a2f70ccdd98859dba35f64dfb57bea65db3c4eeebff17aeeb4a5dde4e7ad6d",
}
SOURCE_FILES = (
    "integrations/memebench/run_full100_endpoint_selector_rerun.py",
    "integrations/memebench/full100_endpoint_counterfactual.py",
    "integrations/memebench/same_session_selector_intervention.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/services/cascade_router.py",
    "src/contexthub/services/dependency_discovery_service.py",
)


class FatalRunError(RuntimeError):
    pass


class ForbiddenStrongChat(BaseChatClient):
    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        raise FatalRunError("strong verification is forbidden in this cheap-only run")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FatalRunError(f"{path} must contain a JSON object")
    return value


def _load_v3_results() -> dict[str, dict[str, Any]]:
    output_hashes = _json(V3 / "output_hashes.json")
    if output_hashes.get("artifact_sha256") != EXPECTED_V3_ARTIFACT_SHA256:
        raise FatalRunError("counterfactual v3 artifact identity mismatch")
    for name, expected in output_hashes.get("files", {}).items():
        path = V3 / name
        if not path.is_file() or sha256_file(path) != expected:
            raise FatalRunError(f"counterfactual v3 file hash mismatch: {name}")
    rows = {
        str(row["episode_id"]): row
        for row in read_jsonl_tolerant(V3 / "alignment_results.jsonl")
        if str(row.get("episode_id")) in TARGET_NODE_BY_EPISODE
    }
    if set(rows) != set(TARGET_NODE_BY_EPISODE):
        raise FatalRunError("counterfactual v3 lacks one of the six episodes")
    return rows


def _shared_episode(episode_id: str) -> dict[str, Any]:
    return _json(SHARED / "episodes" / f"{episode_id}.json")


def _build_case(
    episode_id: str,
    target_node_id: str,
    result: Mapping[str, Any],
    shared: Mapping[str, Any],
) -> dict[str, Any]:
    node_by_id = {str(row["node_id"]): row for row in shared["nodes"]}
    alignment_by_node = {str(row["node_id"]): row for row in result["alignments"]}
    if target_node_id not in node_by_id or target_node_id not in alignment_by_node:
        raise FatalRunError(f"target node binding failed: {episode_id}")
    target_node = node_by_id[target_node_id]
    target_alignment = alignment_by_node[target_node_id]
    if target_alignment.get("status") == "quarantine":
        raise FatalRunError(f"target is quarantined: {episode_id}")
    incoming = [
        row for row in result["candidates"] if row["target_node_id"] == target_node_id
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for row in incoming:
        evidence_id = str(row["source_evidence_id"])
        source_node_id = str(row["source_node_id"])
        source_alignment = alignment_by_node[source_node_id]
        bucket = grouped.setdefault(
            evidence_id,
            {
                "evidence_id": evidence_id,
                "text": str(row["source_text"]),
                "node_ids": [],
                "source_origin": "counterfactual_provenance_envelope",
                "session_index": int(row["source_session_index"]),
                "turn_index": int(row["source_turn_index"]),
                "candidate_ids": [],
                "source_hypotheses": source_alignment["hypotheses"],
            },
        )
        if bucket["text"] != str(row["source_text"]):
            raise FatalRunError(f"evidence text collision: {episode_id}/{evidence_id}")
        bucket["node_ids"].append(source_node_id)
        bucket["candidate_ids"].append(str(row["candidate_id"]))
        bucket["turn_index"] = min(bucket["turn_index"], int(row["source_turn_index"]))
    candidates = []
    for bucket in grouped.values():
        bucket["node_ids"] = sorted(set(bucket["node_ids"]))
        bucket["candidate_ids"] = sorted(set(bucket["candidate_ids"]))
        candidates.append(bucket)
    candidates.sort(key=lambda row: row["evidence_id"])
    target_hypotheses = list(target_alignment["hypotheses"])
    case = {
        "episode_id": episode_id,
        "target_evidence_id": str(target_alignment["evidence_id"]),
        "target_text": str(target_node["text"]),
        "target_node_ids": [target_node_id],
        "target_session_index": int(target_node["session_index"]),
        "target_turn_index": min(int(row["turn_index"]) for row in target_hypotheses),
        "target_hypotheses": target_hypotheses,
        "candidates": candidates,
        "history_candidate_count": sum(
            int(row["session_index"]) < int(target_node["session_index"])
            for row in candidates
        ),
        "same_session_candidate_count": sum(
            int(row["session_index"]) == int(target_node["session_index"])
            for row in candidates
        ),
        "candidate_incidence_count": len(incoming),
        "candidate_identity_hash": canonical_sha256(
            [row["evidence_id"] for row in candidates]
        ),
        "candidate_mapping_hash": canonical_sha256(candidates),
    }
    case["input_hash"] = canonical_sha256(case)
    return case


def build_runtime_cases() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    results = _load_v3_results()
    cases = []
    for episode_id, target_node_id in TARGET_NODE_BY_EPISODE.items():
        shared = _shared_episode(episode_id)
        regenerated = generate_provenance_candidates(episode_id, shared["nodes"])
        if canonical_sha256(regenerated) != canonical_sha256(results[episode_id]):
            raise FatalRunError(
                f"counterfactual generation differs from frozen v3: {episode_id}"
            )
        cases.append(
            _build_case(
                episode_id, target_node_id, results[episode_id], shared
            )
        )
    return sorted(cases, key=lambda row: row["episode_id"]), results


def _old_rows() -> dict[str, dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    for row in read_jsonl_tolerant(OLD / "case_success.jsonl"):
        episode_id = str(row.get("episode_id"))
        target = TARGET_NODE_BY_EPISODE.get(episode_id)
        if target and target in set(map(str, row.get("target_node_ids", ()))):
            if episode_id in matches:
                raise FatalRunError(f"duplicate old target checkpoint: {episode_id}")
            matches[episode_id] = row
    if set(matches) != set(TARGET_NODE_BY_EPISODE):
        raise FatalRunError("old cheap checkpoint does not bind exactly six targets")
    return matches


def _ledger_rows() -> dict[str, dict[str, Any]]:
    rows = validate_ledger(_json(LEDGER))
    selected = {
        str(row["episode_id"]): row
        for row in rows
        if row["decision_class"] == "true_candidate_envelope_miss"
    }
    if set(selected) != set(TARGET_NODE_BY_EPISODE):
        raise FatalRunError("ledger affected-target set differs from frozen six")
    return selected


def build_target_manifest(
    cases: Sequence[Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    decisions = _ledger_rows()
    old = _old_rows()
    rows = []
    for case in cases:
        episode_id = str(case["episode_id"])
        decision = decisions[episode_id]
        expected_source_nodes = sorted(
            map(str, decision["source_evidence"]["node_ids"])
        )
        expected_target_nodes = sorted(
            map(str, decision["target_evidence"]["node_ids"])
        )
        if expected_target_nodes != case["target_node_ids"]:
            raise FatalRunError(f"ledger target-node binding mismatch: {episode_id}")
        alignments = {
            str(row["node_id"]): row for row in results[episode_id]["alignments"]
        }
        expected_source_evidence_ids = sorted(
            {
                str(alignments[node_id]["evidence_id"])
                for node_id in expected_source_nodes
                if alignments[node_id].get("evidence_id")
            }
        )
        expected_candidate_ids = sorted(
            row["candidate_id"]
            for row in results[episode_id]["candidates"]
            if row["target_node_id"] in expected_target_nodes
            and row["source_node_id"] in expected_source_nodes
        )
        visible = sorted(
            row["evidence_id"]
            for row in case["candidates"]
            if set(row["node_ids"]) & set(expected_source_nodes)
        )
        if not visible or not expected_candidate_ids:
            raise FatalRunError(f"expected source absent in preflight: {episode_id}")
        old_row = old[episode_id]
        rows.append(
            {
                "episode_id": episode_id,
                "decision_id": decision["decision_id"],
                "gold_identity": decision["gold_identity"],
                "target_identity": {
                    "endpoint": decision["target_gold_endpoint"],
                    "node_ids": expected_target_nodes,
                    "old_evidence_id": old_row["target_evidence_id"],
                    "counterfactual_evidence_id": case["target_evidence_id"],
                },
                "expected_source_identity": {
                    "endpoint": decision["source_gold_endpoint"],
                    "node_ids": expected_source_nodes,
                    "counterfactual_evidence_ids": expected_source_evidence_ids,
                    "candidate_ids": expected_candidate_ids,
                },
                "runtime_binding": {
                    "case_key": selector_case_key(case, ARM),
                    "input_hash": case["input_hash"],
                    "candidate_identity_hash": case["candidate_identity_hash"],
                    "candidate_mapping_hash": case["candidate_mapping_hash"],
                    "candidate_count": len(case["candidates"]),
                    "candidate_incidence_count": case["candidate_incidence_count"],
                    "expected_source_visible": True,
                },
                "old_checkpoint": {
                    "case_key": old_row["case_key"],
                    "input_hash": old_row["input_hash"],
                    "candidate_identity_hash": old_row["candidate_identity_hash"],
                    "candidate_mapping_hash": old_row["candidate_mapping_hash"],
                    "candidate_count": old_row["candidate_count"],
                    "selected_source_count": old_row["selected_source_count"],
                    "correct_endpoint_selected": any(
                        set(map(str, source["source_node_ids"]))
                        & set(expected_source_nodes)
                        for source in old_row["selected_sources"]
                    ),
                },
            }
        )
    manifest = {
        "schema_version": "p1-full100-endpoint-selector-target-manifest-v1",
        "development_intervention_only": True,
        "target_count": len(rows),
        "rows": rows,
        "gold_is_scoring_only": True,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    return manifest


def _provider_public() -> dict[str, Any]:
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    if not provider.get("api_key") or not provider.get("base_url"):
        raise FatalRunError("openlux credentials/config missing")
    models = provider.get("models")
    if isinstance(models, list) and MODEL not in set(map(str, models)):
        raise FatalRunError(f"provider does not offer fixed model {MODEL}")
    parsed = urlparse(str(provider["base_url"]))
    return {
        "label": PROVIDER,
        "required_model": MODEL,
        "base_url_origin": f"{parsed.scheme}://{parsed.netloc}",
        "provider_file_sha256": sha256_file(DEFAULT_PROVIDERS_PATH),
        "secrets_redacted": True,
    }


def _input_hashes() -> dict[str, str]:
    paths = {
        "ledger": LEDGER,
        "v3/output_hashes.json": V3 / "output_hashes.json",
        "v3/alignment_results.jsonl": V3 / "alignment_results.jsonl",
        "old/config.json": OLD / "config.json",
        "old/case_success.jsonl": OLD / "case_success.jsonl",
        "shared/gold_scoring_side_v2.json": SHARED / "gold_scoring_side_v2.json",
    }
    paths.update(
        {
            f"shared/episodes/{episode_id}.json": SHARED
            / "episodes"
            / f"{episode_id}.json"
            for episode_id in TARGET_NODE_BY_EPISODE
        }
    )
    return {name: sha256_file(path) for name, path in paths.items()}


def prepare(out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    cases, results = build_runtime_cases()
    manifest = build_target_manifest(cases, results)
    runtime_payload = {
        "schema_version": "p1-full100-endpoint-selector-runtime-cases-v1",
        "gold_free": True,
        "cases": cases,
    }
    runtime_payload["artifact_sha256"] = canonical_sha256(runtime_payload)
    expected = [
        {
            "case_key": selector_case_key(case, ARM),
            "episode_id": case["episode_id"],
            "target_evidence_id": case["target_evidence_id"],
            "input_hash": case["input_hash"],
            "candidate_identity_hash": case["candidate_identity_hash"],
            "candidate_mapping_hash": case["candidate_mapping_hash"],
        }
        for case in cases
    ]
    config: dict[str, Any] = {
        "schema_version": "p1-full100-endpoint-selector-rerun-v1",
        "experiment_label": "six-target full100 development intervention",
        "held_out_certification": False,
        "production_go": False,
        "model": MODEL,
        "provider": _provider_public(),
        "arm": ARM,
        "candidate_route": {"tau": 1.0, "k": 5, "recency": False},
        "edge_route": {"tau": 0.0, "lam": None},
        "forbidden_calls": [
            "strong verification",
            "extractor",
            "embedding",
            "answer",
            "judge",
            "P2",
        ],
        "expected_case_count": 6,
        "expected_cases": expected,
        "target_manifest_sha256": manifest["manifest_sha256"],
        "runtime_cases_sha256": runtime_payload["artifact_sha256"],
        "counterfactual_v3_artifact_sha256": EXPECTED_V3_ARTIFACT_SHA256,
        "pricing_per_million_usd": {MODEL: PRICE},
        "retry": {
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF_SECONDS),
            "retryable": ["timeout/connection", "429", "5xx"],
            "fatal": ["authentication/authorization/other 4xx", "config/schema"],
            "inner_chat_client_retry": "existing 60/90/120 read deadlines",
            "failed_retry_usage_policy": (
                "count provider usage when returned; otherwise mark cost incomplete"
            ),
        },
        "gold_isolation": {
            "paid_run_reads": ["config.json", "runtime_cases.json"],
            "paid_run_forbidden_reads": [
                "target_manifest.json",
                "adjudication ledger",
                "gold scoring sidecar",
            ],
            "scoring_runs_only_after_paid_checkpoints": True,
        },
        "input_hashes": _input_hashes(),
        "prompt_hashes": {
            "dependency_discovery": hashlib.sha256(
                _DISCOVERY_PROMPT.encode("utf-8")
            ).hexdigest()
        },
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "package_versions": package_versions(),
        "python": sys.version,
        "platform": platform.platform(),
    }
    config["config_sha256"] = canonical_sha256(config)
    immutable = {
        "target_manifest.json": manifest,
        "runtime_cases.json": runtime_payload,
        "config.json": config,
        "expected_cases.json": {
            "cases": expected,
            "artifact_sha256": canonical_sha256(expected),
        },
        "input_hashes_before.json": config["input_hashes"],
    }
    for name, payload in immutable.items():
        path = out / name
        if path.exists() and _json(path) != payload:
            raise FatalRunError(f"immutable preflight artifact mismatch: {name}")
        if not path.exists():
            if name == "config.json" and (
                (out / "attempts.jsonl").exists()
                or (out / "case_success.jsonl").exists()
            ):
                raise FatalRunError("unbound checkpoint exists")
            atomic_write_json(path, payload)
    preflight = {
        "status": "pass",
        "target_count": len(cases),
        "episode_ids": [case["episode_id"] for case in cases],
        "all_expected_sources_visible": True,
        "all_candidate_tiers_expected_full": True,
        "v3_regeneration_matches": True,
        "extractor_calls": 0,
        "embedding_calls": 0,
        "model_calls": 0,
        "config_sha256": config["config_sha256"],
    }
    atomic_write_json(out / "preflight.json", preflight)
    return preflight


def _runtime(out: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = _json(out / "config.json")
    runtime = _json(out / "runtime_cases.json")
    if canonical_sha256({k: v for k, v in config.items() if k != "config_sha256"}) != config[
        "config_sha256"
    ]:
        raise FatalRunError("config content hash mismatch")
    if canonical_sha256(
        {k: v for k, v in runtime.items() if k != "artifact_sha256"}
    ) != runtime["artifact_sha256"]:
        raise FatalRunError("runtime cases content hash mismatch")
    if runtime["artifact_sha256"] != config["runtime_cases_sha256"]:
        raise FatalRunError("runtime/config binding mismatch")
    return config, list(runtime["cases"])


def _successful(out: Path, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    expected = {row["case_key"]: row for row in config["expected_cases"]}
    rows: dict[str, dict[str, Any]] = {}
    for row in read_jsonl_tolerant(out / "case_success.jsonl"):
        identity = expected.get(str(row.get("case_key")))
        if (
            identity
            and row.get("config_sha256") == config["config_sha256"]
            and row.get("input_hash") == identity["input_hash"]
            and row.get("candidate_identity_hash")
            == identity["candidate_identity_hash"]
            and row.get("candidate_mapping_hash")
            == identity["candidate_mapping_hash"]
            and row.get("cost_incomplete") is False
            and isinstance(row.get("candidate_mapping"), list)
            and isinstance(row.get("model_calls"), list)
            and all(call.get("usage") for call in row["model_calls"])
        ):
            rows[str(row["case_key"])] = row
    return rows


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, ConnectionError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


@contextmanager
def run_lock(out: Path):
    handle = (out / ".run.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise FatalRunError("another process holds the run lock") from exc
    atomic_write_json(
        out / "RUN_STATE.json",
        {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "status": "running",
            "started_at": utc_timestamp(),
        },
    )
    try:
        yield
    finally:
        atomic_write_json(
            out / "RUN_STATE.json",
            {"pid": os.getpid(), "status": "stopped", "timestamp": utc_timestamp()},
        )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


async def run(out: Path) -> dict[str, Any]:
    # Gold isolation: this path reads only preregistered config + gold-free cases.
    config, cases = _runtime(out)
    successes = _successful(out, config)
    prior = read_jsonl_tolerant(out / "attempts.jsonl")
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    inner = OpenAIChatClient(
        api_key=provider["api_key"], base_url=provider["base_url"], model=MODEL
    )
    audit = AuditedChatClient(inner, MODEL, PROVIDER)
    cheap = DependencyDiscoveryService(audit)
    forbidden_audit = AuditedChatClient(
        ForbiddenStrongChat(), "FORBIDDEN", "FORBIDDEN"
    )
    forbidden = DependencyDiscoveryService(forbidden_audit)
    try:
        with run_lock(out):
            for case in cases:
                key = selector_case_key(case, ARM)
                if key in successes:
                    continue
                used = sum(
                    row.get("event") == "attempt_started"
                    and row.get("case_key") == key
                    and row.get("config_sha256") == config["config_sha256"]
                    for row in prior
                )
                for attempt_number in range(used + 1, MAX_ATTEMPTS + 1):
                    attempt_id = str(uuid.uuid4())
                    started = {
                        "event": "attempt_started",
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "case_key": key,
                        "episode_id": case["episode_id"],
                        "config_sha256": config["config_sha256"],
                        "timestamp": utc_timestamp(),
                    }
                    append_jsonl(out / "attempts.jsonl", started)
                    try:
                        result = await run_selector_case(
                            case,
                            ARM,
                            cheap=cheap,
                            strong=forbidden,
                            cheap_audit=audit,
                            strong_audit=forbidden_audit,
                        )
                        result.update(
                            {
                                "config_sha256": config["config_sha256"],
                                "attempt_id": attempt_id,
                                "attempt_number": attempt_number,
                                "candidate_mapping": case["candidates"],
                                "candidate_mapping_hash": case[
                                    "candidate_mapping_hash"
                                ],
                                "completed_at": utc_timestamp(),
                            }
                        )
                        if result["cost_incomplete"]:
                            append_jsonl(out / "case_cost_incomplete.jsonl", result)
                            append_jsonl(
                                out / "attempts.jsonl",
                                {
                                    **started,
                                    "event": "attempt_finished",
                                    "status": "cost_incomplete",
                                    "timestamp": utc_timestamp(),
                                },
                            )
                            if attempt_number < MAX_ATTEMPTS:
                                await asyncio.sleep(BACKOFF_SECONDS[attempt_number - 1])
                                continue
                            break
                        append_jsonl(out / "case_success.jsonl", result)
                        append_jsonl(
                            out / "attempts.jsonl",
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": "success",
                                "model_call_count": len(result["model_calls"]),
                                "timestamp": utc_timestamp(),
                            },
                        )
                        successes[key] = result
                        break
                    except Exception as exc:
                        retryable = _retryable(exc)
                        append_jsonl(
                            out / "attempts.jsonl",
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": (
                                    "retryable_error" if retryable else "fatal_error"
                                ),
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "usage_returned": False,
                                "cost_incomplete": True,
                                "timestamp": utc_timestamp(),
                            },
                        )
                        if not retryable:
                            raise FatalRunError(f"fatal case {key}: {exc}") from exc
                        if attempt_number == MAX_ATTEMPTS:
                            break
                        await asyncio.sleep(BACKOFF_SECONDS[attempt_number - 1])
    finally:
        await inner.close()
    return status(out)


def status(out: Path) -> dict[str, Any]:
    config, _ = _runtime(out)
    successes = _successful(out, config)
    expected = {row["case_key"] for row in config["expected_cases"]}
    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    attempted_by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in attempts:
        if row.get("event") == "attempt_finished":
            attempted_by_key[str(row.get("case_key"))].append(row)
    failed_keys = {
        key
        for key in expected - set(successes)
        if any(row.get("status") == "fatal_error" for row in attempted_by_key[key])
        or len(attempted_by_key[key]) >= MAX_ATTEMPTS
    }
    missing_keys = expected - set(successes) - failed_keys
    return {
        "expected": len(expected),
        "success": len(successes),
        "failed": len(failed_keys),
        "failed_case_keys": sorted(failed_keys),
        "missing": len(missing_keys),
        "missing_case_keys": sorted(missing_keys),
        "attempts_started": sum(
            row.get("event") == "attempt_started" for row in attempts
        ),
        "retryable_failures": sum(
            row.get("status") == "retryable_error" for row in attempts
        ),
        "fatal_failures": sum(row.get("status") == "fatal_error" for row in attempts),
        "cost_incomplete_attempts": sum(
            row.get("status") == "cost_incomplete" for row in attempts
        ),
        "failed_retry_cost_unknown": any(
            row.get("status") in {"retryable_error", "fatal_error"}
            and not row.get("usage_returned")
            for row in attempts
        ),
    }


def _known_incomplete_attempt_usage(out: Path) -> dict[str, int]:
    calls = [
        call
        for row in read_jsonl_tolerant(out / "case_cost_incomplete.jsonl")
        for call in row.get("model_calls", ())
        if call.get("usage")
    ]
    return {
        "calls": len(calls),
        "prompt_tokens": sum(
            int(call["usage"]["prompt_tokens"]) for call in calls
        ),
        "completion_tokens": sum(
            int(call["usage"]["completion_tokens"]) for call in calls
        ),
    }


def _mapping_risks() -> dict[str, dict[str, Any]]:
    side = _json(SHARED / "gold_scoring_side_v2.json")
    result = {}
    for episode in side["episodes"]:
        if episode["episode_id"] not in TARGET_NODE_BY_EPISODE:
            continue
        for row in episode["node_to_entity_mappings"]:
            result[f"{episode['episode_id']}|{row['node_id']}"] = row
    return result


def summarize(out: Path) -> dict[str, Any]:
    config, cases = _runtime(out)
    state = status(out)
    success_by_key = _successful(out, config)
    manifest = _json(out / "target_manifest.json")  # first gold-bearing read
    if manifest["manifest_sha256"] != config["target_manifest_sha256"]:
        raise FatalRunError("target manifest/config hash mismatch")
    old = _old_rows()
    risks = _mapping_risks()
    rows = []
    total_prompt = total_completion = total_cost = 0.0
    for target in manifest["rows"]:
        episode_id = target["episode_id"]
        case = next(row for row in cases if row["episode_id"] == episode_id)
        success = success_by_key.get(selector_case_key(case, ARM))
        expected_nodes = set(target["expected_source_identity"]["node_ids"])
        selected = list(success["selected_sources"]) if success else []
        correct = [
            row
            for row in selected
            if set(map(str, row["source_node_ids"])) & expected_nodes
        ]
        usages = [
            call["usage"] for call in (success["model_calls"] if success else [])
        ]
        prompt = sum(int(row["prompt_tokens"]) for row in usages)
        completion = sum(int(row["completion_tokens"]) for row in usages)
        cost = (prompt * PRICE["prompt"] + completion * PRICE["completion"]) / 1e6
        total_prompt += prompt
        total_completion += completion
        total_cost += cost
        selected_risks = []
        for source in selected:
            mappings = [
                risks.get(f"{episode_id}|{node_id}", {})
                for node_id in source["source_node_ids"]
            ]
            selected_risks.append(
                {
                    "source_evidence_id": source["source_evidence_id"],
                    "unmapped": any(row.get("unmapped") for row in mappings),
                    "multi_entity": any(
                        int(row.get("matched_entity_count", 0)) > 1 for row in mappings
                    ),
                    "source_identity_ambiguity": any(
                        row.get("source_identity_ambiguity") for row in mappings
                    ),
                }
            )
        old_row = old[episode_id]
        rows.append(
            {
                "episode_id": episode_id,
                "status": "success" if success else "missing_or_failed",
                "correct_source_candidate_in_input": True,
                "selector_verdict": (
                    success["edge_tier"] if success else "not_available"
                ),
                "selected_source_evidence_ids": [
                    row["source_evidence_id"] for row in selected
                ],
                "correct_endpoint_selected": bool(correct),
                "selected_extra_unmatched_count": len(selected) - len(correct),
                "source_identity_risks": selected_risks,
                "old": {
                    "candidate_count": old_row["candidate_count"],
                    "output_edge_count": old_row["selected_source_count"],
                    "correct_endpoint_selected": False,
                },
                "new": {
                    "candidate_count": len(case["candidates"]),
                    "candidate_incidence_count": case["candidate_incidence_count"],
                    "output_edge_count": len(selected),
                    "correct_endpoint_selected": bool(correct),
                },
                "candidate_delta": len(case["candidates"])
                - int(old_row["candidate_count"]),
                "output_edge_delta": len(selected)
                - int(old_row["selected_source_count"]),
                "model_calls": len(success["model_calls"]) if success else 0,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cost_usd": cost,
                "attempt_number": success["attempt_number"] if success else None,
            }
        )
    old_hits = sum(row["old"]["correct_endpoint_selected"] for row in rows)
    new_hits = sum(row["new"]["correct_endpoint_selected"] for row in rows)
    old_output = sum(row["old"]["output_edge_count"] for row in rows)
    new_output = sum(row["new"]["output_edge_count"] for row in rows)
    incomplete_usage = _known_incomplete_attempt_usage(out)
    total_prompt += incomplete_usage["prompt_tokens"]
    total_completion += incomplete_usage["completion_tokens"]
    total_cost += (
        incomplete_usage["prompt_tokens"] * PRICE["prompt"]
        + incomplete_usage["completion_tokens"] * PRICE["completion"]
    ) / 1e6
    summary = {
        "schema_version": "p1-full100-endpoint-selector-rerun-summary-v1",
        "experiment_label": "six-target full100 development intervention",
        "held_out_certification": False,
        "production_go": False,
        "checkpoint": state,
        "targets": rows,
        "paired": {
            "target_count": 6,
            "old_recall": {"numerator": old_hits, "denominator": 6},
            "new_recall": {"numerator": new_hits, "denominator": 6},
            "old_episode_graph_miss": 6 - old_hits,
            "new_episode_graph_miss": 6 - new_hits,
            "old_candidate_count": sum(row["old"]["candidate_count"] for row in rows),
            "new_candidate_count": sum(row["new"]["candidate_count"] for row in rows),
            "candidate_delta": sum(row["candidate_delta"] for row in rows),
            "old_output_edge_count": old_output,
            "new_output_edge_count": new_output,
            "output_edge_delta": new_output - old_output,
            "matched_expected_edge_count": new_hits,
            "selected_extra_unmatched_edge_count": new_output - new_hits,
        },
        "calls_and_cost": {
            "model": MODEL,
            "provider": PROVIDER,
            "actual_model_calls": sum(row["model_calls"] for row in rows)
            + incomplete_usage["calls"],
            "known_incomplete_attempt_calls": incomplete_usage["calls"],
            "outer_request_attempts_started": state["attempts_started"],
            "strong_calls": 0,
            "prompt_tokens": int(total_prompt),
            "completion_tokens": int(total_completion),
            "total_tokens": int(total_prompt + total_completion),
            "known_cost_usd": total_cost,
            "pricing_per_million_usd": PRICE,
            "retryable_failures": state["retryable_failures"],
            "fatal_failures": state["fatal_failures"],
            "cost_incomplete_attempts": state["cost_incomplete_attempts"],
            "failed_retry_cost_unknown": state["failed_retry_cost_unknown"],
        },
        "interpretation_limits": [
            "small known-failure development intervention",
            "cannot estimate population precision",
            "selected extra/unmatched is not automatically false positive",
            "not held-out certification",
            "not production Go",
            "even 6/6 only shows recoverability when the correct candidate is present",
        ],
        "config_sha256": config["config_sha256"],
        "gold_scoring_started_after_runtime_checkpoints": True,
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "summary.json", summary)
    after = _input_hashes()
    atomic_write_json(out / "input_hashes_after.json", after)
    if after != _json(out / "input_hashes_before.json"):
        raise FatalRunError("protected input hash changed")
    files = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name not in {"output_hashes.json", ".run.lock", "RUN_STATE.json"}
    }
    atomic_write_json(
        out / "output_hashes.json",
        {"files": files, "artifact_sha256": canonical_sha256(files)},
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run", "status", "summarize"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True)
    args = parser.parse_args(argv)
    if args.model != MODEL or args.provider != PROVIDER:
        raise FatalRunError("model/provider differ from fixed preregistration")
    if args.command == "preflight":
        result = prepare(args.out)
    elif args.command == "run":
        result = asyncio.run(run(args.out))
    elif args.command == "status":
        result = status(args.out)
    else:
        result = summarize(args.out)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
