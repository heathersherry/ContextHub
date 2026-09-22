"""Full100 development re-evaluation of the frozen v3 candidate envelope.

The paid path consumes only the gold-free runtime artifact.  Gold, endpoint
adjudication, and scoring sidecars are opened only by ``analyze`` after every
eligible target has a complete checkpoint.
"""
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
from statistics import mean
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from contexthub.llm.chat_client import BaseChatClient, OpenAIChatClient
from contexthub.services.dependency_discovery_service import (
    DependencyDiscoveryService,
    _DISCOVERY_PROMPT,
)
from contexthub.planning.statistics import clopper_pearson_upper
from integrations.memebench.compare_same_session_selector_full100 import (
    EXPECTED_CONFIGS,
    EXPECTED_SHARED_CONTENT,
    EXPECTED_SHARED_INDEX,
    EXPECTED_V2_CANONICAL,
    EXPECTED_V2_FILE,
    EXPECTED_V2_MANIFEST_CANONICAL,
    EXPECTED_V2_MANIFEST_FILE,
    MAPPING_RULE,
)
from integrations.memebench.full100_endpoint_counterfactual import (
    audit_alignment_invariants,
    generate_provenance_candidates,
    validate_ledger,
    verify_alignment_permutation,
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
DEFAULT_OUT = RUNS / "p1_full100_candidate_envelope_reevaluation_20260824_v2"
ARM = "turn_full_cheap"
MODEL = "gpt-4o-mini"
PROVIDER = "openlux"
PRICE = {"prompt": 0.15, "completion": 0.60}
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (5.0, 15.0, 45.0)
STALE_SECONDS = 20 * 60
EXPECTED_V3_ARTIFACT_SHA256 = (
    "6444eb730153aceaaaa219f8f1e49ad83e6f54c7d548c52e04d8ffc24c9ddf76"
)
SOURCE_FILES = (
    "integrations/memebench/run_full100_candidate_envelope_reevaluation.py",
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
        raise FatalRunError("strong verification is forbidden")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FatalRunError(f"{path} must contain an object")
    return value


def _prompt(case: Mapping[str, Any]) -> str | None:
    if not case["candidates"]:
        return None
    numbered = "\n".join(
        f"{index + 1}. {row['text']}" for index, row in enumerate(case["candidates"])
    )
    return _DISCOVERY_PROMPT.format(
        new_fact=case["target_text"], candidates=numbered
    )


def prompt_hash(case: Mapping[str, Any]) -> str | None:
    prompt = _prompt(case)
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest() if prompt else None


def _load_shared() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    index = _json(SHARED / "shared_manifest_index.json")
    if (
        index.get("manifest_content_sha256") != EXPECTED_SHARED_CONTENT
        or index.get("index_sha256") != EXPECTED_SHARED_INDEX
        or index.get("episode_count") != 100
    ):
        raise FatalRunError("shared index identity mismatch")
    episodes = []
    for row in index["episodes"]:
        path = SHARED / row["manifest_path"]
        if sha256_file(path) != row["manifest_file_sha256"]:
            raise FatalRunError(f"shared shard hash mismatch: {row['episode_id']}")
        episodes.append(_json(path))
    if sum(len(row["selector_cases"]) for row in episodes) != 2209:
        raise FatalRunError("old shared target count changed")
    return index, episodes


def _load_v3() -> dict[str, dict[str, Any]]:
    output = _json(V3 / "output_hashes.json")
    if output.get("artifact_sha256") != EXPECTED_V3_ARTIFACT_SHA256:
        raise FatalRunError("v3 artifact identity mismatch")
    for name, expected in output["files"].items():
        if sha256_file(V3 / name) != expected:
            raise FatalRunError(f"v3 file hash mismatch: {name}")
    rows = {
        str(row["episode_id"]): row
        for row in read_jsonl_tolerant(V3 / "alignment_results.jsonl")
    }
    if len(rows) != 100:
        raise FatalRunError("v3 must contain 100 episodes")
    return rows


def _old_successes() -> dict[tuple[str, str], dict[str, Any]]:
    config = _json(OLD / "config.json")
    if (
        config.get("config_sha256") != EXPECTED_CONFIGS["cheap"]
        or config.get("model") != MODEL
        or config.get("arm") != ARM
        or config.get("provider", {}).get("label") != PROVIDER
    ):
        raise FatalRunError("old cheap config cannot prove reuse compatibility")
    rows = read_jsonl_tolerant(OLD / "case_success.jsonl")
    result = {}
    for row in rows:
        key = (str(row["episode_id"]), str(row["target_evidence_id"]))
        if (
            key in result
            or row.get("config_sha256") != config["config_sha256"]
            or row.get("cost_incomplete") is not False
            or not isinstance(row.get("candidate_mapping"), list)
            or not isinstance(row.get("model_calls"), list)
            or any(call.get("usage") is None for call in row["model_calls"])
        ):
            raise FatalRunError(f"old checkpoint is not reuse-safe: {key}")
        result[key] = row
    if len(result) != 2209:
        raise FatalRunError("old cheap success set is incomplete")
    return result


def _effective_evidence_ids(
    shared: Mapping[str, Any], v3: Mapping[str, Any]
) -> dict[str, str]:
    old = {
        str(row["node_id"]): str(row["evidence_id"])
        for row in shared["alignments"]
        if row.get("evidence_id")
    }
    result = {}
    for row in v3["alignments"]:
        node_id = str(row["node_id"])
        evidence_id = old.get(node_id) or str(row.get("evidence_id") or "")
        if not evidence_id:
            raise FatalRunError(f"v3 aligned node lacks identity: {node_id}")
        result[node_id] = evidence_id
    return result


def _new_candidate(
    source: Mapping[str, Any],
    source_nodes: Sequence[str],
    candidate_ids: Sequence[str],
    *,
    target_session: int,
) -> dict[str, Any]:
    same = int(source["session_index"]) == target_session
    row: dict[str, Any] = {
        "evidence_id": source["evidence_id"],
        "text": source["text"],
        "node_ids": sorted(set(source_nodes)),
        "source_origin": (
            "same_session_turn_envelope" if same else "history_snapshot"
        ),
        "session_index": int(source["session_index"]),
        "turn_index": int(source["turn_index"]),
    }
    if same:
        row["envelope_edge_id"] = (
            candidate_ids[0]
            if len(candidate_ids) == 1
            else f"pcand-group-{canonical_sha256(sorted(candidate_ids))}"
        )
    return row


def build_episode_cases(
    shared: Mapping[str, Any], v3: Mapping[str, Any]
) -> list[dict[str, Any]]:
    episode_id = str(shared["episode_id"])
    effective = _effective_evidence_ids(shared, v3)
    nodes = {str(row["node_id"]): row for row in shared["nodes"]}
    alignments = {str(row["node_id"]): row for row in v3["alignments"]}
    old_cases = {
        str(row["target_evidence_id"]): row for row in shared["selector_cases"]
    }
    old_candidates = {
        target: {str(row["evidence_id"]): row for row in case["candidates"]}
        for target, case in old_cases.items()
    }
    target_nodes: dict[str, list[str]] = defaultdict(list)
    for node_id, evidence_id in effective.items():
        target_nodes[evidence_id].append(node_id)
    incoming: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in v3["candidates"]:
        incoming[effective[str(edge["target_node_id"])]].append(edge)
    cases = []
    for target_id, aliases in sorted(target_nodes.items()):
        aliases = sorted(aliases)
        target_node = nodes[aliases[0]]
        if any(str(nodes[node_id]["text"]) != str(target_node["text"]) for node_id in aliases):
            raise FatalRunError(f"target evidence text collision: {episode_id}/{target_id}")
        target_session = int(target_node["session_index"])
        grouped: dict[str, dict[str, Any]] = {}
        for edge in incoming.get(target_id, ()):
            source_node_id = str(edge["source_node_id"])
            source_id = effective[source_node_id]
            source_node = nodes[source_node_id]
            source_alignment = alignments[source_node_id]
            bucket = grouped.setdefault(
                source_id,
                {
                    "evidence_id": source_id,
                    "text": str(source_node["text"]),
                    "session_index": int(source_node["session_index"]),
                    "turn_index": min(
                        int(row["turn_index"])
                        for row in source_alignment["hypotheses"]
                    ),
                    "node_ids": [],
                    "candidate_ids": [],
                },
            )
            bucket["node_ids"].append(source_node_id)
            bucket["candidate_ids"].append(str(edge["candidate_id"]))
        candidates = []
        for source_id, bucket in grouped.items():
            old = old_candidates.get(target_id, {}).get(source_id)
            proposed = _new_candidate(
                bucket,
                bucket["node_ids"],
                sorted(set(bucket["candidate_ids"])),
                target_session=target_session,
            )
            # Preserve byte-identical selector mapping for unchanged candidates.
            candidates.append(dict(old) if old == proposed else proposed)
        candidates.sort(key=lambda row: (row["source_origin"], row["evidence_id"]))
        hypotheses = [
            hypothesis
            for node_id in aliases
            for hypothesis in alignments[node_id]["hypotheses"]
        ]
        selector_input = {
            "episode_id": episode_id,
            "target_evidence_id": target_id,
            "target_text": str(target_node["text"]),
            "target_node_ids": aliases,
            "target_session_index": target_session,
            "target_turn_index": min(int(row["turn_index"]) for row in hypotheses),
            "candidates": candidates,
            "history_candidate_count": sum(
                row["source_origin"] == "history_snapshot" for row in candidates
            ),
            "same_session_candidate_count": sum(
                row["source_origin"] == "same_session_turn_envelope"
                for row in candidates
            ),
            "candidate_identity_hash": canonical_sha256(
                sorted(str(row["evidence_id"]) for row in candidates)
            ),
        }
        case = {
            **selector_input,
            "input_hash": canonical_sha256(selector_input),
            "candidate_incidence_count": len(incoming.get(target_id, ())),
            "candidate_mapping_hash": canonical_sha256(candidates),
        }
        case["prompt_sha256"] = prompt_hash(case)
        cases.append(case)
    return cases


def build_runtime_cases() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index, shared_rows = _load_shared()
    v3_rows = _load_v3()
    cases = []
    regenerated = []
    permutations = {}
    for shared in shared_rows:
        episode_id = str(shared["episode_id"])
        generated = generate_provenance_candidates(episode_id, shared["nodes"])
        if canonical_sha256(generated) != canonical_sha256(v3_rows[episode_id]):
            raise FatalRunError(f"v3 regeneration mismatch: {episode_id}")
        regenerated.append(generated)
        permutations[episode_id] = verify_alignment_permutation(
            episode_id, shared["nodes"]
        )
        cases.extend(build_episode_cases(shared, generated))
    invariants = audit_alignment_invariants(regenerated)
    safety = {
        **invariants,
        "permutation_failure_count": sum(not value for value in permutations.values()),
    }
    forbidden = (
        safety["future_to_past_count"]
        + safety["same_turn_directed_count"]
        + safety["self_loop_count"]
        + safety["directed_cycle_or_nontrivial_scc_count"]
        + safety["permutation_failure_count"]
    )
    if forbidden:
        raise FatalRunError(f"candidate safety invariant failed: {safety}")
    return sorted(
        cases, key=lambda row: (row["episode_id"], row["target_evidence_id"])
    ), {"index": index, "safety": safety}


def reuse_decision(
    case: Mapping[str, Any], old: Mapping[str, Any] | None
) -> tuple[bool, list[str]]:
    if old is None:
        return False, ["no_old_target_checkpoint"]
    reasons = []
    checks = {
        "input_hash": case["input_hash"],
        "candidate_identity_hash": case["candidate_identity_hash"],
        "candidate_mapping_hash": case["candidate_mapping_hash"],
        "prompt_sha256": case["prompt_sha256"],
    }
    observed = {
        "input_hash": old.get("input_hash"),
        "candidate_identity_hash": old.get("candidate_identity_hash"),
        "candidate_mapping_hash": old.get("candidate_mapping_hash"),
        "prompt_sha256": (
            old.get("model_calls", [{}])[0].get("prompt_sha256")
            if old.get("model_calls")
            else None
        ),
    }
    for name, expected in checks.items():
        if observed[name] != expected:
            reasons.append(f"{name}_changed")
    if canonical_sha256(old.get("candidate_mapping", ())) != case["candidate_mapping_hash"]:
        reasons.append("old_candidate_mapping_content_invalid")
    return not reasons, reasons


def _provider_public() -> dict[str, Any]:
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    if not provider.get("api_key") or not provider.get("base_url"):
        raise FatalRunError("provider credentials/config missing")
    parsed = urlparse(str(provider["base_url"]))
    return {
        "label": PROVIDER,
        "required_model": MODEL,
        "base_url_origin": f"{parsed.scheme}://{parsed.netloc}",
        "provider_file_sha256": sha256_file(DEFAULT_PROVIDERS_PATH),
        "secrets_redacted": True,
    }


def _protected_hashes() -> dict[str, str]:
    roots = {"shared": SHARED, "old": OLD, "v3": V3, "ledger": LEDGER.parent}
    return {
        f"{label}/{path.relative_to(root)}": sha256_file(path)
        for label, root in roots.items()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def prepare(out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    cases, evidence = build_runtime_cases()
    old = _old_successes()
    manifest = []
    for case in cases:
        prior = old.get((case["episode_id"], case["target_evidence_id"]))
        reuse, reasons = reuse_decision(case, prior)
        manifest.append(
            {
                "case_key": selector_case_key(case, ARM),
                "episode_id": case["episode_id"],
                "target_evidence_id": case["target_evidence_id"],
                "target_node_ids": case["target_node_ids"],
                "input_hash": case["input_hash"],
                "candidate_identity_hash": case["candidate_identity_hash"],
                "candidate_mapping_hash": case["candidate_mapping_hash"],
                "prompt_sha256": case["prompt_sha256"],
                "candidate_count": len(case["candidates"]),
                "reuse_action": "reused_unchanged" if reuse else "rerun_changed",
                "reuse_reasons": reasons,
                "old_case_key": prior.get("case_key") if prior else None,
            }
        )
    runtime = {
        "schema_version": "p1-full100-candidate-envelope-runtime-v1",
        "gold_free": True,
        "cases": cases,
    }
    runtime["artifact_sha256"] = canonical_sha256(runtime)
    target_manifest = {
        "schema_version": "p1-full100-candidate-envelope-target-manifest-v1",
        "rows": manifest,
    }
    target_manifest["manifest_sha256"] = canonical_sha256(target_manifest)
    protected = _protected_hashes()
    config: dict[str, Any] = {
        "schema_version": "p1-full100-candidate-envelope-reevaluation-v1",
        "experiment_label": "MEME full100 development re-evaluation",
        "held_out_certification": False,
        "model": MODEL,
        "provider": _provider_public(),
        "arm": ARM,
        "candidate_algorithm": "frozen v3 multi-hypothesis/interval-order",
        "candidate_route": {"tau": 1.0, "k": 5, "recency": False},
        "edge_route": {"tau": 0.0, "lam": None},
        "expected_episode_count": 100,
        "expected_case_count": len(cases),
        "expected_cases": manifest,
        "runtime_cases_sha256": runtime["artifact_sha256"],
        "target_manifest_sha256": target_manifest["manifest_sha256"],
        "v3_artifact_sha256": EXPECTED_V3_ARTIFACT_SHA256,
        "old_cheap_config_sha256": EXPECTED_CONFIGS["cheap"],
        "shared_hashes": {
            "content": EXPECTED_SHARED_CONTENT,
            "index": EXPECTED_SHARED_INDEX,
        },
        "pricing_per_million_usd": {MODEL: PRICE},
        "prompt_hashes": {
            "dependency_discovery": hashlib.sha256(
                _DISCOVERY_PROMPT.encode("utf-8")
            ).hexdigest()
        },
        "retry": {
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF_SECONDS),
            "inner_read_timeouts_seconds": [60, 90, 120],
            "retryable": ["timeout/connection", "429", "5xx"],
            "fatal_stop": ["authentication", "authorization", "quota/entitlement"],
        },
        "checkpoint": {
            "states": ["pending", "in_progress", "success", "failed"],
            "owner_fields": ["pid", "hostname", "heartbeat_at"],
            "stale_seconds": STALE_SECONDS,
        },
        "gold_isolation": {
            "paid_path_reads": ["config.json", "runtime_cases.json"],
            "paid_path_forbidden": ["gold sidecar", "ledger", "target manifest"],
            "scoring_after_complete_only": True,
        },
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "protected_input_hashes": protected,
        "package_versions": package_versions(),
        "python": sys.version,
        "platform": platform.platform(),
    }
    config["config_sha256"] = canonical_sha256(config)
    immutable = {
        "config.json": config,
        "runtime_cases.json": runtime,
        "target_manifest.json": target_manifest,
        "input_hashes_before.json": protected,
    }
    for name, payload in immutable.items():
        path = out / name
        if path.exists() and _json(path) != payload:
            raise FatalRunError(f"immutable artifact mismatch: {name}")
        if not path.exists():
            atomic_write_json(path, payload)
    preflight = {
        "status": "pass",
        "model_calls": 0,
        "episodes": 100,
        "targets": len(cases),
        "old_targets": 2209,
        "newly_eligible_targets": len(cases) - 2209,
        "reused_unchanged": sum(
            row["reuse_action"] == "reused_unchanged" for row in manifest
        ),
        "rerun_changed": sum(
            row["reuse_action"] == "rerun_changed" for row in manifest
        ),
        "candidate_incidence_count": sum(
            int(case["candidate_incidence_count"]) for case in cases
        ),
        "candidate_mapping_count": sum(len(case["candidates"]) for case in cases),
        "safety": evidence["safety"],
        "gold_isolation_pass": True,
        "v3_regeneration_matches": True,
        "config_sha256": config["config_sha256"],
    }
    atomic_write_json(out / "preflight.json", preflight)
    return preflight


def _runtime(out: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config, runtime = _json(out / "config.json"), _json(out / "runtime_cases.json")
    if canonical_sha256({k: v for k, v in config.items() if k != "config_sha256"}) != config[
        "config_sha256"
    ]:
        raise FatalRunError("config hash mismatch")
    if canonical_sha256({k: v for k, v in runtime.items() if k != "artifact_sha256"}) != runtime[
        "artifact_sha256"
    ]:
        raise FatalRunError("runtime hash mismatch")
    if runtime["artifact_sha256"] != config["runtime_cases_sha256"]:
        raise FatalRunError("runtime/config mismatch")
    return config, list(runtime["cases"])


def _checkpoint_path(out: Path, case_key: str) -> Path:
    return out / "checkpoints" / f"{hashlib.sha256(case_key.encode()).hexdigest()}.json"


def checkpoint_stale(
    checkpoint: Mapping[str, Any],
    *,
    now: float | None = None,
    hostname: str | None = None,
) -> bool:
    if checkpoint.get("status") != "in_progress":
        return False
    now = time.time() if now is None else now
    host = socket.gethostname() if hostname is None else hostname
    heartbeat = float(checkpoint.get("heartbeat_epoch", 0))
    if now - heartbeat <= STALE_SECONDS:
        return False
    if checkpoint.get("hostname") != host:
        return True
    pid = int(checkpoint.get("pid", -1))
    try:
        os.kill(pid, 0)
        return False
    except (ProcessLookupError, PermissionError):
        return True


def _valid_success(
    checkpoint: Mapping[str, Any], expected: Mapping[str, Any], config_hash: str
) -> bool:
    result = checkpoint.get("result")
    return bool(
        checkpoint.get("status") == "success"
        and checkpoint.get("config_sha256") == config_hash
        and isinstance(result, Mapping)
        and result.get("input_hash") == expected["input_hash"]
        and result.get("candidate_identity_hash") == expected["candidate_identity_hash"]
        and result.get("candidate_mapping_hash") == expected["candidate_mapping_hash"]
        and isinstance(result.get("candidate_mapping"), list)
        and canonical_sha256(result["candidate_mapping"])
        == expected["candidate_mapping_hash"]
        and isinstance(result.get("model_calls"), list)
        and result.get("cost_incomplete") is False
        and all(call.get("usage") for call in result["model_calls"])
    )


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, ConnectionError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


def _definitive_stop(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
        401,
        402,
        403,
    }


@contextmanager
def run_lock(out: Path):
    handle = (out / ".run.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise FatalRunError("another runner holds the process lock") from exc
    state = {
        "status": "running",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": utc_timestamp(),
        "heartbeat_at": utc_timestamp(),
    }
    atomic_write_json(out / "RUN_STATE.json", state)
    try:
        yield state
    finally:
        state.update({"status": "stopped", "heartbeat_at": utc_timestamp()})
        atomic_write_json(out / "RUN_STATE.json", state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _copy_reused(
    case: Mapping[str, Any],
    old: Mapping[str, Any],
    *,
    config_hash: str,
) -> dict[str, Any]:
    result = dict(old)
    result.update(
        {
            "config_sha256": config_hash,
            "reuse_action": "reused_unchanged",
            "reused_from_config_sha256": old["config_sha256"],
            "reused_from_case_key": old["case_key"],
            "candidate_mapping": case["candidates"],
            "candidate_mapping_hash": case["candidate_mapping_hash"],
            "completed_at": utc_timestamp(),
        }
    )
    return result


async def run(out: Path) -> dict[str, Any]:
    # Gold-isolated paid path: only these two immutable files are read.
    config, cases = _runtime(out)
    expected = {row["case_key"]: row for row in config["expected_cases"]}
    old = _old_successes()
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    inner = OpenAIChatClient(
        api_key=provider["api_key"], base_url=provider["base_url"], model=MODEL
    )
    audit = AuditedChatClient(inner, MODEL, PROVIDER)
    cheap = DependencyDiscoveryService(audit)
    forbidden_audit = AuditedChatClient(
        ForbiddenStrongChat(), "FORBIDDEN", "FORBIDDEN"
    )
    strong = DependencyDiscoveryService(forbidden_audit)
    (out / "checkpoints").mkdir(exist_ok=True)
    try:
        with run_lock(out) as run_state:
            for case in cases:
                key = selector_case_key(case, ARM)
                spec = expected[key]
                path = _checkpoint_path(out, key)
                if path.exists():
                    checkpoint = _json(path)
                    if _valid_success(checkpoint, spec, config["config_sha256"]):
                        continue
                    if checkpoint.get("status") == "in_progress" and not checkpoint_stale(
                        checkpoint
                    ):
                        continue
                if spec["reuse_action"] == "reused_unchanged":
                    prior = old[(case["episode_id"], case["target_evidence_id"])]
                    result = _copy_reused(
                        case, prior, config_hash=config["config_sha256"]
                    )
                    atomic_write_json(
                        path,
                        {
                            "status": "success",
                            "case_key": key,
                            "config_sha256": config["config_sha256"],
                            "reuse_action": "reused_unchanged",
                            "result": result,
                        },
                    )
                    run_state.update(
                        {"heartbeat_at": utc_timestamp(), "last_case_key": key}
                    )
                    atomic_write_json(out / "RUN_STATE.json", run_state)
                    continue
                prior_attempts = (
                    list(_json(path).get("attempts", ())) if path.exists() else []
                )
                for attempt_number in range(len(prior_attempts) + 1, MAX_ATTEMPTS + 1):
                    attempt_id = str(uuid.uuid4())
                    checkpoint = {
                        "status": "in_progress",
                        "case_key": key,
                        "config_sha256": config["config_sha256"],
                        "reuse_action": "rerun_changed",
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "heartbeat_at": utc_timestamp(),
                        "heartbeat_epoch": time.time(),
                        "attempts": prior_attempts,
                    }
                    atomic_write_json(path, checkpoint)
                    try:
                        result = await run_selector_case(
                            case,
                            ARM,
                            cheap=cheap,
                            strong=strong,
                            cheap_audit=audit,
                            strong_audit=forbidden_audit,
                        )
                        result.update(
                            {
                                "config_sha256": config["config_sha256"],
                                "reuse_action": "rerun_changed",
                                "attempt_id": attempt_id,
                                "attempt_number": attempt_number,
                                "candidate_mapping": case["candidates"],
                                "candidate_mapping_hash": case["candidate_mapping_hash"],
                                "completed_at": utc_timestamp(),
                            }
                        )
                        attempt = {
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_number,
                            "status": (
                                "cost_incomplete"
                                if result["cost_incomplete"]
                                else "success"
                            ),
                            "model_calls": result["model_calls"],
                            "timestamp": utc_timestamp(),
                        }
                        prior_attempts.append(attempt)
                        if result["cost_incomplete"]:
                            atomic_write_json(
                                path,
                                {
                                    **checkpoint,
                                    "status": "failed",
                                    "cost_incomplete": True,
                                    "attempts": prior_attempts,
                                },
                            )
                            break
                        atomic_write_json(
                            path,
                            {
                                "status": "success",
                                "case_key": key,
                                "config_sha256": config["config_sha256"],
                                "reuse_action": "rerun_changed",
                                "attempts": prior_attempts,
                                "result": result,
                            },
                        )
                        append_jsonl(out / "case_success.jsonl", result)
                        run_state.update(
                            {"heartbeat_at": utc_timestamp(), "last_case_key": key}
                        )
                        atomic_write_json(out / "RUN_STATE.json", run_state)
                        break
                    except Exception as exc:
                        attempt = {
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_number,
                            "status": (
                                "retryable_error" if _retryable(exc) else "fatal_error"
                            ),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "usage_returned": False,
                            "cost_incomplete": True,
                            "timestamp": utc_timestamp(),
                        }
                        prior_attempts.append(attempt)
                        atomic_write_json(
                            path,
                            {
                                **checkpoint,
                                "status": "failed",
                                "attempts": prior_attempts,
                                "cost_incomplete": True,
                            },
                        )
                        if _definitive_stop(exc):
                            raise FatalRunError(
                                f"definitive provider denial: {exc}"
                            ) from exc
                        if not _retryable(exc) or attempt_number == MAX_ATTEMPTS:
                            break
                        await asyncio.sleep(BACKOFF_SECONDS[attempt_number - 1])
    finally:
        await inner.close()
    return status(out)


def status(out: Path) -> dict[str, Any]:
    config, _ = _runtime(out)
    states = defaultdict(int)
    reused = rerun = calls = prompt = completion = retries = 0
    cost_incomplete = 0
    internal_retries = 0
    missing_keys = []
    for expected in config["expected_cases"]:
        path = _checkpoint_path(out, expected["case_key"])
        if not path.exists():
            states["pending"] += 1
            missing_keys.append(expected["case_key"])
            continue
        checkpoint = _json(path)
        state = str(checkpoint.get("status", "failed"))
        states[state] += 1
        if state == "success":
            result = checkpoint["result"]
            reused += result.get("reuse_action") == "reused_unchanged"
            rerun += result.get("reuse_action") == "rerun_changed"
            for attempt in checkpoint.get("attempts", ()):
                retries += attempt.get("status") != "success"
                for call in attempt.get("model_calls", ()):
                    calls += 1
                    usage = call.get("usage")
                    if usage:
                        prompt += int(usage["prompt_tokens"])
                        completion += int(usage["completion_tokens"])
                        if float(call.get("latency_seconds", 0)) > 60:
                            internal_retries += 1
                            calls += 1
                            prompt += int(usage["prompt_tokens"])
                            retries += 1
                            cost_incomplete += 1
                    else:
                        cost_incomplete += 1
        if checkpoint.get("cost_incomplete"):
            cost_incomplete += 1
    return {
        "expected": len(config["expected_cases"]),
        "pending": states["pending"],
        "in_progress": states["in_progress"],
        "success": states["success"],
        "failed": states["failed"],
        "missing": states["pending"],
        "missing_case_keys": missing_keys,
        "reused_unchanged": reused,
        "rerun_changed": rerun,
        "actual_model_calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "retry_or_failed_attempts": retries,
        "internal_readtimeout_retries": internal_retries,
        "cost_incomplete": cost_incomplete,
    }


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * q))]


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean": mean(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "total": sum(values),
    }


def _mapping_entities(mapping: Mapping[str, Any]) -> set[str]:
    return {str(row["entity"]) for row in mapping.get("matched_entities", ())}


def _selected_results(out: Path, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for expected in config["expected_cases"]:
        checkpoint = _json(_checkpoint_path(out, expected["case_key"]))
        if not _valid_success(checkpoint, expected, config["config_sha256"]):
            raise FatalRunError(f"incomplete checkpoint: {expected['case_key']}")
        rows.append(dict(checkpoint["result"]))
    return rows


def _score(
    results: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    side: Mapping[str, Any],
) -> dict[str, Any]:
    case_by_key = {
        (str(case["episode_id"]), str(case["target_evidence_id"])): case
        for case in cases
    }
    side_by_episode = {str(row["episode_id"]): row for row in side["episodes"]}
    edges = {}
    per_episode_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        episode_id = str(result["episode_id"])
        episode = side_by_episode[episode_id]
        node_maps = {
            str(row["node_id"]): row for row in episode["node_to_entity_mappings"]
        }
        case = case_by_key[(episode_id, str(result["target_evidence_id"]))]
        target_entities = set().union(
            *(_mapping_entities(node_maps[node]) for node in case["target_node_ids"])
        )
        for source in result["selected_sources"]:
            source_entities = set().union(
                *(
                    _mapping_entities(node_maps[node])
                    for node in source["source_node_ids"]
                )
            )
            matched = {
                str(edge["gold_edge_identity_v1"])
                for record in episode["scoring_records"]
                for edge in record["gold_edges"]
                if edge["source_entity"] in source_entities
                and edge["target_entity"] in target_entities
            }
            edge_id = canonical_sha256(
                (
                    episode_id,
                    source["source_evidence_id"],
                    result["target_evidence_id"],
                )
            )
            edge = {
                "edge_id": edge_id,
                "episode_id": episode_id,
                "source_evidence_id": source["source_evidence_id"],
                "source_node_ids": source["source_node_ids"],
                "source_text": source["source_text"],
                "source_origin": source["source_origin"],
                "target_evidence_id": result["target_evidence_id"],
                "target_node_ids": result["target_node_ids"],
                "target_text": result["target_text"],
                "source_entities": sorted(source_entities),
                "target_entities": sorted(target_entities),
                "matched_gold_edge_identities": sorted(matched),
            }
            edges[edge_id] = edge
            per_episode_edges[episode_id].append(edge)
    by_hop = {}
    per_episode = {}
    for episode in side["episodes"]:
        episode_id = str(episode["episode_id"])
        per_episode[episode_id] = {}
        for record in episode["scoring_records"]:
            hop = int(record["hop"])
            gold = set(map(str, record["gold_edge_identity_v1_set"]))
            hit = {
                identity
                for edge in per_episode_edges[episode_id]
                for identity in edge["matched_gold_edge_identities"]
                if identity in gold
            }
            per_episode[episode_id][f"hop{hop}"] = {
                "gold": sorted(gold),
                "hit": sorted(hit),
                "missed": sorted(gold - hit),
            }
    for hop in (1, 2):
        rows = [
            hops[f"hop{hop}"]
            for hops in per_episode.values()
            if f"hop{hop}" in hops
        ]
        gold = sum(len(row["gold"]) for row in rows)
        hit = sum(len(row["hit"]) for row in rows)
        misses = sum(bool(row["missed"]) for row in rows)
        by_hop[f"hop{hop}"] = {
            "episodes": len(rows),
            "gold_edges": gold,
            "hits": hit,
            "raw_approximate_recall": hit / gold,
            "episode_graph_miss": misses,
            "episode_graph_miss_rate": misses / len(rows),
            "episode_graph_miss_cp95_upper": clopper_pearson_upper(
                misses, len(rows), 0.05
            ),
        }
    return {
        "by_hop": by_hop,
        "per_episode": per_episode,
        "output_edges": list(edges.values()),
        "output_graph": {
            "selected_incidences": sum(
                len(result["selected_sources"]) for result in results
            ),
            "unique_evidence_edges": len(edges),
            "history": sum(
                edge["source_origin"] == "history_snapshot" for edge in edges.values()
            ),
            "same_session": sum(
                edge["source_origin"] == "same_session_turn_envelope"
                for edge in edges.values()
            ),
            "matched": sum(
                bool(edge["matched_gold_edge_identities"]) for edge in edges.values()
            ),
            "unmatched": sum(
                not edge["matched_gold_edge_identities"] for edge in edges.values()
            ),
        },
    }


def _endpoint_metrics(
    score: Mapping[str, Any],
    old_score: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    edges = list(score["output_edges"])
    old_edges_by_episode: dict[str, set[str]] = defaultdict(set)
    new_edges_by_episode: dict[str, set[str]] = defaultdict(set)
    for edge in old_score["output_edges"]:
        old_edges_by_episode[str(edge["episode_id"])].add(str(edge["edge_id"]))
    for edge in edges:
        new_edges_by_episode[str(edge["episode_id"])].add(str(edge["edge_id"]))
    selected: dict[str, bool] = {}
    for decision in ledger_rows:
        source_entity = str(decision["source_gold_endpoint"]["entity"])
        target_entity = str(decision["target_gold_endpoint"]["entity"])
        source_nodes = set(map(str, decision["source_evidence"].get("node_ids", ())))
        target_nodes = set(map(str, decision["target_evidence"].get("node_ids", ())))
        if source_nodes and target_nodes:
            value = any(
                edge["episode_id"] == decision["episode_id"]
                and source_nodes.intersection(map(str, edge["source_node_ids"]))
                and target_nodes.intersection(map(str, edge["target_node_ids"]))
                for edge in edges
            )
        elif decision["endpoint_verdict"] == "confirmed_hit":
            # The immutable ledger established the old selected endpoint.  It
            # remains established only when every physical old edge in that
            # episode survives byte-identically in the new graph.
            episode_id = str(decision["episode_id"])
            value = old_edges_by_episode[episode_id] <= new_edges_by_episode[episode_id]
        else:
            value = any(
                edge["episode_id"] == decision["episode_id"]
                and source_entity in edge["source_entities"]
                and target_entity in edge["target_entities"]
                for edge in edges
            )
        selected[str(decision["decision_id"])] = value
    result = {}
    for hop in (1, 2):
        raw = score["by_hop"][f"hop{hop}"]
        rows = [row for row in ledger_rows if hop in row["hop_views"]]
        excluded = [
            row for row in rows if row["endpoint_verdict"] in {"unscorable", "unknown"}
        ]
        observable = [
            row
            for row in rows
            if row["endpoint_verdict"] in {"confirmed_hit", "confirmed_miss"}
        ]
        per_episode = {
            episode_id: set(values[f"hop{hop}"]["missed"])
            for episode_id, values in score["per_episode"].items()
            if f"hop{hop}" in values
        }
        for row in rows:
            identity = str(row["gold_identity"])
            if row in excluded or selected[row["decision_id"]]:
                per_episode[str(row["episode_id"])].discard(identity)
        corrected_miss_edges = sum(len(values) for values in per_episode.values())
        corrected_miss_episodes = sum(bool(values) for values in per_episode.values())
        corrected_denominator = int(raw["gold_edges"]) - len(excluded)
        conservative_miss_edges = corrected_miss_edges + len(excluded)
        conservative_miss_episodes = set(
            episode_id for episode_id, values in per_episode.items() if values
        ) | {str(row["episode_id"]) for row in excluded}
        confirmed_pipeline = [
            row
            for row in observable
            if row["endpoint_verdict"] == "confirmed_miss"
            and not selected[row["decision_id"]]
        ]
        confirmed_episodes = {str(row["episode_id"]) for row in confirmed_pipeline}
        excluded_only_episodes = {
            str(row["episode_id"]) for row in excluded
        } - {
            episode_id for episode_id, misses in per_episode.items() if misses
        }
        observable_episodes = int(raw["episodes"]) - len(excluded_only_episodes)
        result[f"hop{hop}"] = {
            "corrected_observable": {
                "hits": corrected_denominator - corrected_miss_edges,
                "denominator": corrected_denominator,
                "recall": (
                    (corrected_denominator - corrected_miss_edges)
                    / corrected_denominator
                ),
                "episode_graph_miss": corrected_miss_episodes,
                "episode_denominator": observable_episodes,
                "episode_graph_miss_rate": corrected_miss_episodes
                / observable_episodes,
                "episode_graph_miss_cp95_upper": clopper_pearson_upper(
                    corrected_miss_episodes, observable_episodes, 0.05
                ),
            },
            "conservative": {
                "hits": int(raw["gold_edges"]) - conservative_miss_edges,
                "denominator": int(raw["gold_edges"]),
                "recall": (
                    (int(raw["gold_edges"]) - conservative_miss_edges)
                    / int(raw["gold_edges"])
                ),
                "episode_graph_miss": len(conservative_miss_episodes),
                "episode_denominator": int(raw["episodes"]),
                "episode_graph_miss_rate": len(conservative_miss_episodes)
                / int(raw["episodes"]),
                "episode_graph_miss_cp95_upper": clopper_pearson_upper(
                    len(conservative_miss_episodes), int(raw["episodes"]), 0.05
                ),
            },
            "confirmed_pipeline_graph_miss": {
                "edge_count": len(confirmed_pipeline),
                "episode_count": len(confirmed_episodes),
                "episode_denominator": int(raw["episodes"]),
                "episode_rate": len(confirmed_episodes) / int(raw["episodes"]),
                "episode_cp95_upper": clopper_pearson_upper(
                    len(confirmed_episodes), int(raw["episodes"]), 0.05
                ),
                "scope": "immutable 19-dependency endpoint ledger only",
            },
            "reviewed_endpoint_selected": {
                str(row["decision_id"]): selected[str(row["decision_id"])]
                for row in rows
            },
        }
    return result


def _paired(new: Mapping[str, Any], old: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for hop in ("hop1", "hop2"):
        rows = []
        for episode_id, new_hops in new["per_episode"].items():
            if hop not in new_hops or hop not in old["per_episode"][episode_id]:
                continue
            new_miss = set(new_hops[hop]["missed"])
            old_miss = set(old["per_episode"][episode_id][hop]["missed"])
            rows.append(
                {
                    "episode_id": episode_id,
                    "recovered_old_miss": sorted(old_miss - new_miss),
                    "new_miss": sorted(new_miss - old_miss),
                    "common_miss": sorted(old_miss & new_miss),
                }
            )
        result[hop] = {
            "recovered_old_miss_count": sum(
                len(row["recovered_old_miss"]) for row in rows
            ),
            "new_miss_count": sum(len(row["new_miss"]) for row in rows),
            "common_miss_count": sum(len(row["common_miss"]) for row in rows),
            "rows": rows,
        }
    return result


def _review_packet(new: Mapping[str, Any], old: Mapping[str, Any]) -> dict[str, Any]:
    maps = {
        label: {row["edge_id"]: row for row in score["output_edges"]}
        for label, score in (("new", new), ("old", old))
    }
    new_ids, old_ids = set(maps["new"]), set(maps["old"])
    strata = {
        "newly_added": [maps["new"][key] for key in new_ids - old_ids],
        "common_unmatched": [
            maps["new"][key]
            for key in new_ids & old_ids
            if not maps["new"][key]["matched_gold_edge_identities"]
        ],
        "removed_old": [maps["old"][key] for key in old_ids - new_ids],
    }
    seed = "p1-full100-v3-reevaluation-blind-review-v1"
    samples = {}
    for name, rows in strata.items():
        ranked = sorted(
            rows,
            key=lambda row: canonical_sha256(
                {"seed": seed, "stratum": name, "edge_id": row["edge_id"]}
            ),
        )
        selected, episode_counts = [], defaultdict(int)
        for row in ranked:
            if episode_counts[row["episode_id"]] >= 2:
                continue
            selected.append(row)
            episode_counts[row["episode_id"]] += 1
            if len(selected) >= 30:
                break
        samples[name] = selected
    packet = {
        "schema_version": "p1-full100-v3-blind-review-packet-v1",
        "status": "packet_only_no_decisions",
        "seed": seed,
        "hash_ranked": True,
        "max_per_episode_per_stratum": 2,
        "max_per_stratum": 30,
        "labels_if_independently_reviewed": ["yes", "no", "ambiguous"],
        "unmatched_is_not_false_positive": True,
        "population_counts": {name: len(rows) for name, rows in strata.items()},
        "samples": samples,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet


def _cost(
    results: Sequence[Mapping[str, Any]], old_results: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    shared = {
        str(row["episode_id"]): float(row["shared_preprocessing_total"]["known_usd"])
        for row in read_jsonl_tolerant(SHARED / "per_episode_cost.jsonl")
    }

    def rows(values: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for value in values:
            grouped[str(value["episode_id"])].append(value)
        answer = {}
        for episode_id in sorted(shared):
            calls = [
                call
                for value in grouped[episode_id]
                for call in value["model_calls"]
            ]
            prompt = sum(int(call["usage"]["prompt_tokens"]) for call in calls)
            completion = sum(int(call["usage"]["completion_tokens"]) for call in calls)
            # A >60s audited latency can only occur after the fixed first
            # ReadTimeout and an internal retry.  The retry repeats the same
            # prompt, so its input tokens are known; the lost completion is not.
            internal_retries = sum(
                float(call.get("latency_seconds", 0)) > 60 for call in calls
            )
            prompt += sum(
                int(call["usage"]["prompt_tokens"])
                for call in calls
                if float(call.get("latency_seconds", 0)) > 60
            )
            selector = (
                prompt * PRICE["prompt"] + completion * PRICE["completion"]
            ) / 1_000_000
            answer[episode_id] = {
                "shared": shared[episode_id],
                "selector": selector,
                "deployment": shared[episode_id] + selector,
                "calls": len(calls),
                "http_attempts": len(calls) + internal_retries,
                "internal_retry_count": internal_retries,
                "cost_incomplete_attempts": internal_retries,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
            }
        return answer

    new, old = rows(results), rows(old_results)
    summary = {}
    for label, values in (("new", new), ("old", old)):
        summary[label] = {
            name: _distribution([row[name] for row in values.values()])
            for name in ("shared", "selector", "deployment")
        }
    summary["paired"] = {}
    for name in ("shared", "selector", "deployment"):
        deltas = [new[key][name] - old[key][name] for key in sorted(new)]
        ratios = [
            new[key][name] / old[key][name]
            for key in sorted(new)
            if old[key][name]
        ]
        summary["paired"][name] = {
            "delta": _distribution(deltas),
            "ratio": _distribution(ratios),
        }
    rows_out = [
        {
            "episode_id": key,
            "old": old[key],
            "new": new[key],
            "delta": {
                name: new[key][name] - old[key][name]
                for name in ("shared", "selector", "deployment")
            },
            "ratio": {
                name: new[key][name] / old[key][name] if old[key][name] else None
                for name in ("shared", "selector", "deployment")
            },
        }
        for key in sorted(new)
    ]
    return summary, rows_out


def analyze(out: Path) -> dict[str, Any]:
    config, cases = _runtime(out)
    state = status(out)
    if state["failed"] or state["missing"] or state["in_progress"]:
        raise FatalRunError(f"cannot claim complete analysis: {state}")
    results = _selected_results(out, config)
    # First gold-bearing reads occur only after checkpoint completeness above.
    side = _json(SHARED / "gold_scoring_side_v2.json")
    side_manifest = _json(SHARED / "gold_scoring_side_v2_manifest.json")
    if (
        sha256_file(SHARED / "gold_scoring_side_v2.json") != EXPECTED_V2_FILE
        or side.get("canonical_content_sha256") != EXPECTED_V2_CANONICAL
        or sha256_file(SHARED / "gold_scoring_side_v2_manifest.json")
        != EXPECTED_V2_MANIFEST_FILE
        or side_manifest.get("manifest_sha256") != EXPECTED_V2_MANIFEST_CANONICAL
        or side.get("mapping_rule_version") != MAPPING_RULE
    ):
        raise FatalRunError("scoring sidecar identity mismatch")
    ledger_rows = validate_ledger(_json(LEDGER))
    old_results = list(_old_successes().values())
    _, shared_episodes = _load_shared()
    old_cases = [
        case for episode in shared_episodes for case in episode["selector_cases"]
    ]
    new_score = _score(results, cases, side)
    old_score = _score(old_results, old_cases, side)
    endpoint = _endpoint_metrics(new_score, old_score, ledger_rows)
    paired = _paired(new_score, old_score)
    cost, cost_rows = _cost(results, old_results)
    packet = _review_packet(new_score, old_score)
    atomic_write_json(out / "review_packet.json", packet)
    payload = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in cost_rows
    )
    (out / "per_episode_cost.jsonl").write_text(payload, encoding="utf-8")
    affected = [
        row for row in ledger_rows if row["decision_class"] == "true_candidate_envelope_miss"
    ]
    by_target_nodes = {
        (edge["episode_id"], node)
        for edge in new_score["output_edges"]
        for node in edge["target_node_ids"]
    }
    recovered_six = 0
    for decision in affected:
        sources = set(map(str, decision["source_evidence"]["node_ids"]))
        targets = set(map(str, decision["target_evidence"]["node_ids"]))
        if any(
            edge["episode_id"] == decision["episode_id"]
            and sources.intersection(map(str, edge["source_node_ids"]))
            and targets.intersection(map(str, edge["target_node_ids"]))
            for edge in new_score["output_edges"]
        ):
            recovered_six += 1
    sensitivity = {
        hop: [
            {
                "epsilon_graph": epsilon,
                "point_pass": metrics["confirmed_pipeline_graph_miss"][
                    "episode_rate"
                ]
                <= epsilon,
                "cp95_upper_pass": metrics["confirmed_pipeline_graph_miss"][
                    "episode_cp95_upper"
                ]
                <= epsilon,
            }
            for epsilon in (0.05, 0.10, 0.15)
        ]
        for hop, metrics in endpoint.items()
    }
    preflight = _json(out / "preflight.json")
    summary = {
        "schema_version": "p1-full100-v3-reevaluation-summary-v1",
        "development_reevaluation_only": True,
        "held_out_certification": False,
        "completion": state,
        "preflight": preflight,
        "quality": {
            "old_raw": old_score["by_hop"],
            "new_raw": new_score["by_hop"],
            "new_endpoint_corrected": endpoint,
            "paired_misses": paired,
            "known_six_recovered": recovered_six,
            "known_six_total": len(affected),
            "epsilon_sensitivity": sensitivity,
            "endpoint_adjudication_note": (
                "The immutable 19-dependency ledger is bound for corrected/"
                "conservative interpretation; raw new misses outside that ledger "
                "remain approximate and require independent adjudication."
            ),
        },
        "candidate_graph": {
            "old_target_count": 2209,
            "new_target_count": len(cases),
            "old_candidate_mapping_count": sum(
                len(case["candidates"]) for case in old_cases
            ),
            "new_candidate_mapping_count": sum(
                len(case["candidates"]) for case in cases
            ),
            "new_candidate_incidence_count": sum(
                int(case["candidate_incidence_count"]) for case in cases
            ),
            "old_output": old_score["output_graph"],
            "new_output": new_score["output_graph"],
        },
        "review_packet": {
            "status": packet["status"],
            "packet_sha256": packet["packet_sha256"],
            "population_counts": packet["population_counts"],
        },
        "cost_usd": cost,
        "cost_scope": (
            "shared preprocessing plus final cheap selector path only; not "
            "retrieval+answer full-system cost"
        ),
        "pricing_per_million_usd": {MODEL: PRICE},
        "model_usage": {
            "actual_new_calls": state["actual_model_calls"],
            "prompt_tokens": state["prompt_tokens"],
            "completion_tokens": state["completion_tokens"],
            "total_tokens": state["prompt_tokens"] + state["completion_tokens"],
            "known_new_call_cost_usd": (
                state["prompt_tokens"] * PRICE["prompt"]
                + state["completion_tokens"] * PRICE["completion"]
            )
            / 1_000_000,
            "retry_or_failed_attempts": state["retry_or_failed_attempts"],
            "known_internal_readtimeout_retries": state[
                "internal_readtimeout_retries"
            ],
            "cost_incomplete_attempts": state["cost_incomplete"],
            "strong_calls": 0,
        },
        "interpretation_limits": [
            "not held-out certification",
            "not production Go",
            "unmatched edges are not false positives",
            "no population precision claim without independent review decisions",
            "offline provenance approximation is not extraction-time provenance",
            "does not estimate retrieval+answer full-system cost",
        ],
        "config_sha256": config["config_sha256"],
        "gold_scoring_started_after_runtime_checkpoints": True,
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "metrics.json", {
        "new": {k: v for k, v in new_score.items() if k != "output_edges"},
        "old": {k: v for k, v in old_score.items() if k != "output_edges"},
        "paired": paired,
    })
    atomic_write_json(out / "paired_comparison.json", paired)
    atomic_write_json(out / "summary.json", summary)
    after = _protected_hashes()
    atomic_write_json(out / "input_hashes_after.json", after)
    if after != _json(out / "input_hashes_before.json"):
        raise FatalRunError("protected input changed")
    files = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name not in {"output_hashes.json"}
    }
    atomic_write_json(
        out / "output_hashes.json",
        {"files": files, "artifact_sha256": canonical_sha256(files)},
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("preflight", "run", "status", "analyze")
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True)
    args = parser.parse_args(argv)
    if args.model != MODEL or args.provider != PROVIDER:
        raise FatalRunError("model/provider differ from frozen configuration")
    if args.command == "preflight":
        result = prepare(args.out)
    elif args.command == "run":
        result = asyncio.run(run(args.out))
    elif args.command == "status":
        result = status(args.out)
    else:
        result = analyze(args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
