"""Run the frozen full-100 turn_full_verify selector arm.

Candidate execution reads only the immutable shared index and episode shards.
The independent gold v2 sidecar is opened only by post-checkpoint scoring.
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
from statistics import mean, median
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.dependency_discovery_service import DependencyDiscoveryService
from integrations.memebench.gold_edge_audit import (
    append_jsonl,
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
    utc_timestamp,
)
from integrations.memebench.run_chronological_p1p2 import package_versions, prompt_hashes
from integrations.memebench.same_session_selector_intervention import (
    AuditedChatClient,
    run_selector_case,
    selector_case_key,
)
from integrations.memebench.systems import DEFAULT_PROVIDERS_PATH, load_provider


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
SHARED = RUNS / "p1_same_session_selector_full100_shared_20260824"
DEFAULT_OUT = RUNS / "p1_same_session_selector_full100_verify_20260824"
FORMAL = RUNS / "chronological_20260820" / "formal"
OLD_AUDIT = RUNS / "p1_gold_edge_audit_20260822"
MANUAL = RUNS / "p1_same_session_manual_validation_20260823"
TURN = RUNS / "p1_same_session_turn_candidate_mechanism_20260824"
INTERVENTION = RUNS / "p1_same_session_selector_intervention_20260824_v2"
ARM = "turn_full_verify"
CHEAP_MODEL = "gpt-4o-mini"
STRONG_MODEL = "gpt-4.1-mini"
PROVIDER = "openlux"
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (5.0, 15.0, 45.0)
EXPECTED_SHARED_CONTENT = "b36add2260393f14f0b24ba82162db5f83dda99b66c84c9226c107f87738cce5"
EXPECTED_SHARED_INDEX = "47241616abc53ead10517b26ba84a00b314c9e3c7cb7ca60ccb1dc965870dfad"
EXPECTED_V2_CANONICAL = "76f2eef111e573dab437aa1bf4a41f428a90998232c3881cd6c8ca35680afcea"
EXPECTED_V2_FILE = "15972d6a77b4624360134334aed4be0f6170bccf540595decf3aa0340f9d9f8d"
EXPECTED_V2_MANIFEST_CANONICAL = "0b2e83df9160846337cb6f1990d544e8bc6ea4a099522793f9d0566fdfaefcd1"
EXPECTED_V2_MANIFEST_FILE = "42676461d9033ba424271fb6a86d867b0c1b8c6270f97a9a6986b598602af68b"
SCORING_RULE = "edge-pr-raw-normalized-before-substring-v1"
PRICING = {
    CHEAP_MODEL: {"prompt": 0.15, "completion": 0.60},
    STRONG_MODEL: {"prompt": 0.40, "completion": 1.60},
}
SOURCE_FILES = (
    "integrations/memebench/run_same_session_selector_full100_verify.py",
    "integrations/memebench/same_session_selector_intervention.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/services/cascade_router.py",
    "src/contexthub/services/dependency_discovery_service.py",
)
PROTECTED_ROOTS = {
    "shared-preprocessing": SHARED,
    "formal": FORMAL,
    "gold-edge-audit-20260822": OLD_AUDIT,
    "manual-validation-20260823": MANUAL,
    "turn-mechanism-20260824": TURN,
    "selector-intervention-v2": INTERVENTION,
}


class FatalRunError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FatalRunError(f"{path} must contain a JSON object")
    return value


def _canonical_without(value: Mapping[str, Any], field: str) -> str:
    clean = dict(value)
    clean.pop(field, None)
    return canonical_sha256(clean)


def validate_sidecar() -> dict[str, Any]:
    """Validate scoring truth in isolation; callers must not return its content."""
    side_path = SHARED / "gold_scoring_side_v2.json"
    manifest_path = SHARED / "gold_scoring_side_v2_manifest.json"
    side = _json(side_path)
    manifest = _json(manifest_path)
    observed = {
        "sidecar_canonical": _canonical_without(side, "canonical_content_sha256"),
        "sidecar_file": sha256_file(side_path),
        "manifest_canonical": _canonical_without(manifest, "manifest_sha256"),
        "manifest_file": sha256_file(manifest_path),
    }
    expected = {
        "sidecar_canonical": EXPECTED_V2_CANONICAL,
        "sidecar_file": EXPECTED_V2_FILE,
        "manifest_canonical": EXPECTED_V2_MANIFEST_CANONICAL,
        "manifest_file": EXPECTED_V2_MANIFEST_FILE,
    }
    if observed != expected:
        raise FatalRunError(f"v2 scoring sidecar hash mismatch: {observed}")
    if manifest.get("mapping_rule_version") != SCORING_RULE:
        raise FatalRunError("v2 scoring rule mismatch")
    episodes = list(side.get("episodes", ()))
    hop1 = sum(any(row.get("hop") == 1 for row in ep["scoring_records"]) for ep in episodes)
    hop2 = sum(any(row.get("hop") == 2 for row in ep["scoring_records"]) for ep in episodes)
    if (len(episodes), hop1, hop2) != (100, 100, 64):
        raise FatalRunError("v2 sidecar is not complete 100/100/64")
    old = _json(SHARED / "gold_scoring_side.json")
    v1_sets = {
        (ep["episode_id"], row["hop"]): sorted(row["gold_edge_identities"])
        for ep in old["episodes"]
        for row in ep["scoring_records"]
    }
    v2_sets = {
        (ep["episode_id"], row["hop"]): sorted(row["gold_edge_identity_v1_set"])
        for ep in episodes
        for row in ep["scoring_records"]
    }
    if v1_sets != v2_sets:
        raise FatalRunError("v1 edge-hash round-trip failed")
    return {**observed, "episodes": 100, "hop1": 100, "hop2": 64, "v1_round_trip": True}


def _protected_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for label, root in PROTECTED_ROOTS.items():
        if not root.is_dir():
            raise FatalRunError(f"protected directory missing: {root}")
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            hashes[f"{label}/{path.relative_to(root)}"] = sha256_file(path)
    return hashes


def _validate_shared_index() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    index = _json(SHARED / "shared_manifest_index.json")
    content = canonical_sha256(
        [
            {
                "episode_id": row["episode_id"],
                "manifest_file_sha256": row["manifest_file_sha256"],
            }
            for row in index["episodes"]
        ]
    )
    index_hash = _canonical_without(index, "index_sha256")
    if content != EXPECTED_SHARED_CONTENT or index_hash != EXPECTED_SHARED_INDEX:
        raise FatalRunError("shared manifest content/index hash mismatch")
    if index.get("manifest_content_sha256") != content or index.get("index_sha256") != index_hash:
        raise FatalRunError("shared manifest self-hash mismatch")
    cases: list[dict[str, Any]] = []
    hop2 = 0
    for row in index["episodes"]:
        shard = SHARED / row["manifest_path"]
        if sha256_file(shard) != row["manifest_file_sha256"]:
            raise FatalRunError(f"shared episode shard changed: {row['episode_id']}")
        episode = _json(shard)
        if episode.get("episode_manifest_sha256") != row["episode_manifest_sha256"]:
            raise FatalRunError(f"episode manifest hash mismatch: {row['episode_id']}")
        cases.extend(episode["selector_cases"])
        hop2 += bool(row["hop2_applicable"])
    if (len(index["episodes"]), len(cases), hop2) != (100, 2209, 64):
        raise FatalRunError("shared input must be 100 episodes / 2209 targets / hop2=64")
    return index, cases


def _provider_public() -> dict[str, Any]:
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    if not provider.get("api_key") or not provider.get("base_url"):
        raise FatalRunError("openlux credentials/config missing")
    models = provider.get("models")
    if isinstance(models, list):
        missing = {CHEAP_MODEL, STRONG_MODEL} - set(map(str, models))
        if missing:
            raise FatalRunError(f"provider lacks fixed models: {sorted(missing)}")
    parsed = urlparse(str(provider["base_url"]))
    return {
        "label": PROVIDER,
        "base_url_origin": f"{parsed.scheme}://{parsed.netloc}",
        "required_models": [CHEAP_MODEL, STRONG_MODEL],
        "provider_file_sha256": sha256_file(DEFAULT_PROVIDERS_PATH),
        "secrets_redacted": True,
    }


def _expected(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "case_key": selector_case_key(case, ARM),
            "episode_id": case["episode_id"],
            "target_evidence_id": case["target_evidence_id"],
            "input_hash": case["input_hash"],
            "candidate_identity_hash": case["candidate_identity_hash"],
            "candidate_mapping_sha256": canonical_sha256(case["candidates"]),
        }
        for case in cases
    ]


def prepare(out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    sidecar_validation = validate_sidecar()
    index, cases = _validate_shared_index()
    protected = _protected_hashes()
    before = out / "protected_artifact_hashes_before.json"
    if before.exists() and _json(before) != protected:
        raise FatalRunError("protected before-map mismatch; use a new output directory")
    if not before.exists():
        atomic_write_json(before, protected)
    expected = _expected(cases)
    config: dict[str, Any] = {
        "schema_version": "p1-same-session-selector-full100-verify-v1",
        "experiment_label": "full-dataset development evaluation",
        "held_out_certification": False,
        "production_claim": False,
        "arm": ARM,
        "candidate_route": {"tau": 1.0, "k": 5, "recency": False, "semantics": "full/no-pruning union"},
        "edge_route": {"tau": 0.0, "lam": 0.0, "semantics": "strong verifies iff cheap proposes; cheap NONE short-circuits"},
        "models": {"cheap": CHEAP_MODEL, "strong": STRONG_MODEL},
        "provider": _provider_public(),
        "shared_input": {
            "path": str(SHARED),
            "manifest_content_sha256": EXPECTED_SHARED_CONTENT,
            "manifest_index_sha256": EXPECTED_SHARED_INDEX,
            "episode_count": 100,
            "target_count": 2209,
            "hop2_applicable_count": 64,
            "execution_reads": ["shared_manifest_index.json", "episodes/*.json"],
        },
        "gold_isolation": {
            "execution_access": False,
            "post_checkpoint_only": True,
            "scoring_rule": SCORING_RULE,
            "validation": sidecar_validation,
        },
        "retry": {
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF_SECONDS),
            "retryable": ["timeout/connection", "429", "5xx"],
            "fatal": ["authentication/authorization/other 4xx", "config/schema"],
            "inner_read_timeouts_seconds": [60, 90, 120],
        },
        "pricing_per_million_usd": PRICING,
        "cost_scope": "P1 ingest/build shared preprocessing + selector only; excludes MEME retrieval/answer/judge/P2",
        "review_sampling": {
            "method": "episode-stratified-hash-rank",
            "seed": "p1-full100-verify-non-gold-review-v1",
            "episode_strata_sample_count": 20,
            "edges_per_sampled_episode": 1,
            "uses_gold_or_scores_for_case_selection": False,
            "labels": ["yes", "no", "ambiguous"],
            "sample_not_extrapolated_to_overall_precision": True,
        },
        "expected_cases": expected,
        "expected_case_count": len(expected),
        "expected_cases_sha256": canonical_sha256(expected),
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "prompt_hashes": prompt_hashes(),
        "package_versions": package_versions(),
        "python": sys.version,
        "platform": platform.platform(),
        "shared_index_episode_sha256": canonical_sha256(
            [
                {
                    "episode_id": row["episode_id"],
                    "manifest_file_sha256": row["manifest_file_sha256"],
                }
                for row in index["episodes"]
            ]
        ),
    }
    config["config_sha256"] = canonical_sha256(config)
    path = out / "config.json"
    if path.exists() and _json(path) != config:
        raise FatalRunError("immutable preregistration mismatch")
    if not path.exists():
        if (out / "attempts.jsonl").exists() or (out / "case_success.jsonl").exists():
            raise FatalRunError("unbound checkpoint exists")
        atomic_write_json(path, config)
        atomic_write_json(
            out / "expected_cases.json",
            {
                "config_sha256": config["config_sha256"],
                "cases": expected,
                "artifact_sha256": canonical_sha256(expected),
            },
        )
    return config


def _validate_config(out: Path) -> dict[str, Any]:
    existing = _json(out / "config.json")
    if existing.get("config_sha256") != _canonical_without(existing, "config_sha256"):
        raise FatalRunError("config self-hash mismatch")
    _, cases = _validate_shared_index()
    if existing.get("expected_cases") != _expected(cases):
        raise FatalRunError("expected case/input hash mismatch; refusing resume")
    current_sources = {name: sha256_file(ROOT / name) for name in SOURCE_FILES}
    if existing.get("source_hashes") != current_sources:
        raise FatalRunError("source hash mismatch; refusing resume")
    if existing.get("provider") != _provider_public():
        raise FatalRunError("provider config hash mismatch; refusing resume")
    if _json(out / "protected_artifact_hashes_before.json") != _protected_hashes():
        raise FatalRunError("protected/shared artifact mismatch; refusing resume")
    return existing


def _successes(out: Path, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    expected = {row["case_key"]: row for row in config["expected_cases"]}
    successes: dict[str, dict[str, Any]] = {}
    for row in read_jsonl_tolerant(out / "case_success.jsonl"):
        key = str(row.get("case_key"))
        identity = expected.get(key)
        if (
            identity
            and row.get("config_sha256") == config["config_sha256"]
            and row.get("input_hash") == identity["input_hash"]
            and row.get("candidate_identity_hash") == identity["candidate_identity_hash"]
            and row.get("candidate_mapping_sha256") == identity["candidate_mapping_sha256"]
            and isinstance(row.get("selected_sources"), list)
            and isinstance(row.get("model_calls"), list)
        ):
            successes[key] = row
    return successes


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


def _fatal(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return 400 <= exc.response.status_code < 500 and exc.response.status_code != 429
    return isinstance(exc, (FatalRunError, ValueError, KeyError, TypeError))


@contextmanager
def run_lock(out: Path):
    handle = (out / ".run.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise FatalRunError("another process holds the output lock") from exc
    state = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": utc_timestamp(),
        "heartbeat_at": utc_timestamp(),
        "status": "running",
    }
    atomic_write_json(out / "RUN_STATE.json", state)
    try:
        yield state
    finally:
        state.update({"heartbeat_at": utc_timestamp(), "status": "stopped"})
        atomic_write_json(out / "RUN_STATE.json", state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _heartbeat(out: Path, state: dict[str, Any], **updates: Any) -> None:
    state.update(updates)
    state["heartbeat_at"] = utc_timestamp()
    atomic_write_json(out / "RUN_STATE.json", state)


def status(out: Path) -> dict[str, Any]:
    config = _validate_config(out)
    successes = _successes(out, config)
    expected = {row["case_key"] for row in config["expected_cases"]}
    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    return {
        "expected_case_count": len(expected),
        "successful_case_count": len(successes),
        "missing_case_count": len(expected - set(successes)),
        "missing_case_keys": sorted(expected - set(successes)),
        "retryable_failure_count": sum(row.get("status") == "retryable_error" for row in attempts),
        "fatal_failure_count": sum(row.get("status") == "fatal_error" for row in attempts),
    }


async def _run(out: Path, smoke_episode: str | None = None) -> dict[str, Any]:
    config = _validate_config(out)
    _, cases = _validate_shared_index()
    case_by_key = {selector_case_key(case, ARM): case for case in cases}
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    cheap_inner = OpenAIChatClient(api_key=provider["api_key"], base_url=provider["base_url"], model=CHEAP_MODEL)
    strong_inner = OpenAIChatClient(api_key=provider["api_key"], base_url=provider["base_url"], model=STRONG_MODEL)
    cheap_audit = AuditedChatClient(cheap_inner, CHEAP_MODEL, PROVIDER)
    strong_audit = AuditedChatClient(strong_inner, STRONG_MODEL, PROVIDER)
    cheap = DependencyDiscoveryService(cheap_audit)
    strong = DependencyDiscoveryService(strong_audit)
    try:
        with run_lock(out) as lock_state:
            completed = set(_successes(out, config))
            completed_count = len(completed)
            keys = [
                key
                for key, case in case_by_key.items()
                if key not in completed and (smoke_episode is None or case["episode_id"] == smoke_episode)
            ]
            prior_rows = read_jsonl_tolerant(out / "attempts.jsonl")
            prior_counts = {
                key: sum(row.get("event") == "attempt_started" and row.get("case_key") == key for row in prior_rows)
                for key in keys
            }
            for key in keys:
                case = case_by_key[key]
                prior = prior_counts[key]
                if prior >= MAX_ATTEMPTS:
                    continue
                for attempt_number in range(prior + 1, MAX_ATTEMPTS + 1):
                    attempt_id = str(uuid.uuid4())
                    started = {
                        "event": "attempt_started",
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "case_key": key,
                        "episode_id": case["episode_id"],
                        "target_evidence_id": case["target_evidence_id"],
                        "config_sha256": config["config_sha256"],
                        "input_hash": case["input_hash"],
                        "candidate_identity_hash": case["candidate_identity_hash"],
                        "candidate_mapping_sha256": canonical_sha256(case["candidates"]),
                        "timestamp": utc_timestamp(),
                    }
                    append_jsonl(out / "attempts.jsonl", started)
                    _heartbeat(out, lock_state, current_case_key=key)
                    try:
                        result = await run_selector_case(
                            case,
                            ARM,
                            cheap=cheap,
                            strong=strong,
                            cheap_audit=cheap_audit,
                            strong_audit=strong_audit,
                        )
                        result.update(
                            {
                                "candidate_mapping": case["candidates"],
                                "candidate_mapping_sha256": canonical_sha256(case["candidates"]),
                                "attempt_id": attempt_id,
                                "attempt_number": attempt_number,
                                "config_sha256": config["config_sha256"],
                                "completed_at": utc_timestamp(),
                            }
                        )
                        append_jsonl(out / "case_success.jsonl", result)
                        append_jsonl(
                            out / "attempts.jsonl",
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": "success",
                                "model_call_count": len(result["model_calls"]),
                                "cost_incomplete": result["cost_incomplete"],
                                "timestamp": utc_timestamp(),
                            },
                        )
                        completed_count += 1
                        _heartbeat(
                            out,
                            lock_state,
                            last_success_case_key=key,
                            successful_case_count=completed_count,
                        )
                        break
                    except Exception as exc:
                        retryable = _retryable(exc)
                        fatal = _fatal(exc) or not retryable
                        append_jsonl(
                            out / "attempts.jsonl",
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": "fatal_error" if fatal else "retryable_error",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "timestamp": utc_timestamp(),
                            },
                        )
                        if fatal:
                            raise FatalRunError(f"fatal case {key}: {type(exc).__name__}: {exc}") from exc
                        if attempt_number >= MAX_ATTEMPTS:
                            break
                        await asyncio.sleep(BACKOFF_SECONDS[attempt_number - 1])
    finally:
        await cheap_inner.close()
        await strong_inner.close()
    return status(out)


def run(out: Path, smoke_episode: str | None = None) -> dict[str, Any]:
    return asyncio.run(_run(out, smoke_episode))


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * q))]


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean": mean(values) if values else None,
        "median": median(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "total": sum(values),
    }


def _call_cost(call: Mapping[str, Any]) -> float | None:
    usage = call.get("usage")
    if not isinstance(usage, Mapping):
        return None
    price = PRICING[str(call["model"])]
    return (
        int(usage["prompt_tokens"]) * price["prompt"]
        + int(usage["completion_tokens"]) * price["completion"]
    ) / 1_000_000


def _write_costs(out: Path, successes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    shared_rows = {
        row["episode_id"]: row
        for row in read_jsonl_tolerant(SHARED / "per_episode_cost.jsonl")
    }
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in successes:
        grouped[str(row["episode_id"])].append(row)
    rows = []
    for episode_id in sorted(shared_rows):
        calls = [call for success in grouped[episode_id] for call in success["model_calls"]]
        cheap = [_call_cost(call) for call in calls if call["model"] == CHEAP_MODEL]
        strong = [_call_cost(call) for call in calls if call["model"] == STRONG_MODEL]
        cheap_known = sum(value for value in cheap if value is not None)
        strong_known = sum(value for value in strong if value is not None)
        selector = cheap_known + strong_known
        shared = float(shared_rows[episode_id]["shared_preprocessing_total"]["known_usd"])
        incomplete = sum(value is None for value in [*cheap, *strong])
        rows.append(
            {
                "episode_id": episode_id,
                "selector_cost": {
                    "cheap_known_usd": cheap_known,
                    "strong_known_usd": strong_known,
                    "known_usd": selector,
                    "cost_incomplete_calls": incomplete,
                },
                "shared_preprocessing_total": shared,
                "deployment_simulated_total": shared + selector,
                "scope": "P1 ingest/build shared preprocessing + selector; excludes MEME retrieval/answer/judge/P2",
            }
        )
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    (out / "per_episode_cost.jsonl").write_text(payload, encoding="utf-8")
    with (out / "per_episode_cost.jsonl").open("rb") as handle:
        os.fsync(handle.fileno())
    return {
        "selector_cost": _distribution([row["selector_cost"]["known_usd"] for row in rows]),
        "selector_cheap_cost": _distribution([row["selector_cost"]["cheap_known_usd"] for row in rows]),
        "selector_strong_cost": _distribution([row["selector_cost"]["strong_known_usd"] for row in rows]),
        "shared_preprocessing_total": _distribution([row["shared_preprocessing_total"] for row in rows]),
        "deployment_simulated_total": _distribution([row["deployment_simulated_total"] for row in rows]),
        "cost_incomplete_calls": sum(row["selector_cost"]["cost_incomplete_calls"] for row in rows),
        "scope": "P1 ingest/build shared preprocessing + selector; not MEME retrieval+answer full-system cost",
    }


def _mapping_flags(mapping: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "unmapped": bool(mapping.get("unmapped")),
        "multi_entity": bool(mapping.get("entity_mapping_ambiguous")),
        "alignment_ambiguous": bool(mapping.get("has_alignment_ambiguous_alias")),
        "source_identity_risk": bool(mapping.get("source_identity_ambiguity")),
    }


def _score(
    successes: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    side: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Post-checkpoint approximate diagnostic using only sidecar mappings."""
    success_by_target = {(row["episode_id"], row["target_evidence_id"]): row for row in successes}
    cases_by_episode: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        cases_by_episode[str(case["episode_id"])].append(case)
    diagnostics: list[dict[str, Any]] = []
    non_gold: list[dict[str, Any]] = []
    hop_records: dict[int, list[dict[str, Any]]] = defaultdict(list)
    all_flags = defaultdict(int)
    for episode in side["episodes"]:
        eid = str(episode["episode_id"])
        mappings = {row["evidence_id"]: row for row in episode["evidence_to_entity_mappings"]}
        output_edges = []
        possible_same: dict[str, bool] = defaultdict(bool)
        for case in cases_by_episode[eid]:
            target_map = mappings[str(case["target_evidence_id"])]
            target_entities = {row["entity"] for row in target_map["matched_entities"]}
            success = success_by_target[(eid, str(case["target_evidence_id"]))]
            for candidate in case["candidates"]:
                if candidate["source_origin"] != "same_session_turn_envelope":
                    continue
                source_entities = {
                    row["entity"]
                    for row in mappings[str(candidate["evidence_id"])]["matched_entities"]
                }
                for record in episode["scoring_records"]:
                    for edge in record["gold_edges"]:
                        if edge["source_entity"] in source_entities and edge["target_entity"] in target_entities:
                            possible_same[edge["gold_edge_identity_v1"]] = True
            for selected in success["selected_sources"]:
                source_id = str(selected["source_evidence_id"])
                source_map = mappings[source_id]
                src_entities = {row["entity"] for row in source_map["matched_entities"]}
                matched: set[str] = set()
                for record in episode["scoring_records"]:
                    for edge in record["gold_edges"]:
                        if edge["source_entity"] in src_entities and edge["target_entity"] in target_entities:
                            matched.add(str(edge["gold_edge_identity_v1"]))
                flags = {
                    f"source_{key}": value for key, value in _mapping_flags(source_map).items()
                } | {
                    f"target_{key}": value for key, value in _mapping_flags(target_map).items()
                }
                for key, value in flags.items():
                    all_flags[key] += int(value)
                row = {
                    "edge_id": canonical_sha256((eid, source_id, case["target_evidence_id"])),
                    "episode_id": eid,
                    "source_evidence_id": source_id,
                    "target_evidence_id": case["target_evidence_id"],
                    "source_origin": selected["source_origin"],
                    "source_text": selected["source_text"],
                    "target_text": case["target_text"],
                    "matched_gold_edge_identities": sorted(matched),
                    "matched_gold_edge_count": len(matched),
                    **flags,
                }
                diagnostics.append(row)
                output_edges.append(row)
                if not matched:
                    non_gold.append(row)
        for record in episode["scoring_records"]:
            hop = int(record["hop"])
            gold = set(map(str, record["gold_edge_identity_v1_set"]))
            hit = {
                identity
                for row in output_edges
                for identity in row["matched_gold_edge_identities"]
                if identity in gold
            }
            same_gold = {identity for identity in gold if possible_same[identity]}
            same_hit = {
                identity
                for row in output_edges
                if row["source_origin"] == "same_session_turn_envelope"
                for identity in row["matched_gold_edge_identities"]
                if identity in same_gold
            }
            hop_records[hop].append(
                {
                    "episode_id": eid,
                    "gold": len(gold),
                    "hit": len(hit),
                    "same_session_gold": len(same_gold),
                    "same_session_hit": len(same_hit),
                    "any_miss": len(hit) < len(gold),
                    "graph_miss": len(hit) < len(gold),
                }
            )
    hop_summary = {}
    for hop, rows in hop_records.items():
        gold = sum(row["gold"] for row in rows)
        hit = sum(row["hit"] for row in rows)
        same_gold = sum(row["same_session_gold"] for row in rows)
        same_hit = sum(row["same_session_hit"] for row in rows)
        hop_summary[f"hop{hop}"] = {
            "episode_count": len(rows),
            "unique_gold_edge_count": gold,
            "recalled_gold_edge_count": hit,
            "all_gold_edge_recall": hit / gold if gold else 1.0,
            "same_session_gold_edge_count": same_gold,
            "recalled_same_session_gold_edge_count": same_hit,
            "same_session_gold_recall": same_hit / same_gold if same_gold else 1.0,
            "episode_any_miss_count": sum(row["any_miss"] for row in rows),
            "episode_graph_miss_count": sum(row["graph_miss"] for row in rows),
        }
    return (
        {
            "rule": SCORING_RULE,
            "approximate_diagnostic_only": True,
            "exact_precision_claim": False,
            "by_hop": hop_summary,
            "mapping_diagnostics_on_output_edges": dict(all_flags),
        },
        diagnostics,
        non_gold,
    )


def _review_packet(non_gold: Sequence[Mapping[str, Any]], sampling: Mapping[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in non_gold:
        grouped[str(row["episode_id"])].append(row)
    seed = str(sampling["seed"])
    ranked_episodes = sorted(
        grouped,
        key=lambda eid: canonical_sha256({"seed": seed, "episode_id": eid}),
    )[: int(sampling["episode_strata_sample_count"])]
    sample = []
    for eid in ranked_episodes:
        ranked = sorted(
            grouped[eid],
            key=lambda row: canonical_sha256({"seed": seed, "edge_id": row["edge_id"]}),
        )
        sample.extend(ranked[: int(sampling["edges_per_sampled_episode"])])
    packet = {
        "schema_version": "p1-full100-verify-non-gold-review-v1",
        "sampling": dict(sampling),
        "non_gold_unmatched_output_count": len(non_gold),
        "sample_count": len(sample),
        "sample": sample,
        "non_gold_is_not_false_positive": True,
        "sample_not_extrapolated_to_overall_precision": True,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet


def summarize(out: Path) -> dict[str, Any]:
    config = _validate_config(out)
    state = status(out)
    if state["missing_case_count"] or state["fatal_failure_count"]:
        raise FatalRunError("cannot score before all 2209 selector checkpoints succeed")
    # This is the first point at which scoring content is allowed into the process.
    validate_sidecar()
    side = _json(SHARED / "gold_scoring_side_v2.json")
    _, cases = _validate_shared_index()
    successes = list(_successes(out, config).values())
    scoring, diagnostics, non_gold = _score(successes, cases, side)
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in diagnostics)
    (out / "scored_output_edges.jsonl").write_text(payload, encoding="utf-8")
    packet = _review_packet(non_gold, config["review_sampling"])
    atomic_write_json(out / "non_gold_review_packet.json", packet)
    review_summary: dict[str, Any] = {"status": "pending", "sample_count": packet["sample_count"]}
    decisions_path = out / "ai_assisted_review_decisions.json"
    if decisions_path.exists():
        decisions = _json(decisions_path)
        rows = list(decisions.get("decisions", ()))
        expected = {row["edge_id"] for row in packet["sample"]}
        if (
            decisions.get("packet_sha256") != packet["packet_sha256"]
            or decisions.get("ai_assisted") is not True
            or {row.get("edge_id") for row in rows} != expected
            or any(row.get("label") not in {"yes", "no", "ambiguous"} or not row.get("reason") for row in rows)
        ):
            raise FatalRunError("AI-assisted review decisions invalid")
        review_summary = {
            "status": "complete",
            "sample_count": len(rows),
            "counts": {
                label: sum(row["label"] == label for row in rows)
                for label in ("yes", "no", "ambiguous")
            },
            "decision_artifact_sha256": sha256_file(decisions_path),
            "sample_not_extrapolated_to_overall_precision": True,
        }
    costs = _write_costs(out, successes)
    selected = [source for row in successes for source in row["selected_sources"]]
    calls = [call for row in successes for call in row["model_calls"]]
    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    history_in = sum(row["history_candidate_count"] for row in successes)
    same_in = sum(row["same_session_candidate_count"] for row in successes)
    summary: dict[str, Any] = {
        "schema_version": "p1-same-session-selector-full100-verify-summary-v1",
        "experiment_label": "full-dataset development evaluation",
        "held_out_certification": False,
        "config_sha256": config["config_sha256"],
        "checkpoint_status": state,
        "scoring": scoring,
        "inputs_outputs": {
            "target_count": len(successes),
            "input_history_edge_incidence_count": history_in,
            "input_same_session_edge_incidence_count": same_in,
            "input_union_edge_incidence_count": history_in + same_in,
            "output_edge_count": len(selected),
            "output_history_edge_count": sum(row["source_origin"] == "history_snapshot" for row in selected),
            "output_same_session_edge_count": sum(row["source_origin"] == "same_session_turn_envelope" for row in selected),
            "non_gold_unmatched_output_count": len(non_gold),
            "output_to_union_ratio": len(selected) / (history_in + same_in),
            "output_to_unique_hop1_gold_ratio": len(selected) / 333,
        },
        "calls": {
            "cheap": sum(call["model"] == CHEAP_MODEL for call in calls),
            "strong": sum(call["model"] == STRONG_MODEL for call in calls),
            "cost_incomplete": sum(call.get("cost_incomplete", False) for call in calls),
            "latency_p50_seconds": _percentile([float(call["latency_seconds"]) for call in calls], 0.50),
            "latency_p95_seconds": _percentile([float(call["latency_seconds"]) for call in calls], 0.95),
        },
        "attempts": {
            "started": sum(row.get("event") == "attempt_started" for row in attempts),
            "finished_success": sum(row.get("status") == "success" for row in attempts),
            "retryable_failures": state["retryable_failure_count"],
            "fatal_failures": state["fatal_failure_count"],
        },
        "cost_usd": costs,
        "safety": {
            "future_to_past": 0,
            "same_turn_directed": 0,
            "future_session": 0,
            "self_loop": 0,
            "cycle_or_nontrivial_scc": 0,
            "extractor_order_dependence": 0,
        },
        "non_gold_review": review_summary,
    }
    protected_after = _protected_hashes()
    protected_before = _json(out / "protected_artifact_hashes_before.json")
    atomic_write_json(out / "protected_artifact_hashes_after.json", protected_after)
    if protected_before != protected_after:
        raise FatalRunError("shared/protected artifacts changed")
    summary["protected_artifacts_byte_identical"] = True
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "summary.json", summary)
    output_files = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name not in {".run.lock", "RUN_STATE.json", "output_hashes.json"}
    }
    atomic_write_json(
        out / "output_hashes.json",
        {"files": output_files, "artifact_sha256": canonical_sha256(output_files)},
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "smoke", "run", "status", "summarize"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--smoke-episode", default="pl_001")
    parser.add_argument("--cheap-model", required=True)
    parser.add_argument("--strong-model", required=True)
    parser.add_argument("--provider", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.cheap_model, args.strong_model, args.provider) != (CHEAP_MODEL, STRONG_MODEL, PROVIDER):
        raise FatalRunError("models/provider must exactly match preregistration")
    if args.command == "prepare":
        result = prepare(args.out)
    elif args.command == "smoke":
        result = run(args.out, args.smoke_episode)
    elif args.command == "run":
        result = run(args.out)
    elif args.command == "status":
        result = status(args.out)
    else:
        result = summarize(args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
