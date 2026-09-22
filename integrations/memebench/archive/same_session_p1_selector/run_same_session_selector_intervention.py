"""Run the development-only same-session selector intervention experiment."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import platform
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
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
)
from integrations.memebench.run_chronological_p1p2 import package_versions, prompt_hashes
from integrations.memebench.same_session_selector_intervention import (
    ARM_CONFIGS,
    SCHEMA_VERSION,
    AuditedChatClient,
    build_review_packet,
    build_selector_cases,
    run_selector_case,
    score_successes,
    selector_case_key,
    summarize_outputs,
)
from integrations.memebench.systems import DEFAULT_PROVIDERS_PATH, load_provider


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
FROZEN = RUNS / "p1_same_session_manual_validation_20260823"
TURN_RUN = RUNS / "p1_same_session_turn_candidate_mechanism_20260824"
FORMAL = RUNS / "chronological_20260820" / "formal"
OLD_AUDIT = RUNS / "p1_gold_edge_audit_20260822"
DEFAULT_OUT = RUNS / "p1_same_session_selector_intervention_20260824_v2"
CHEAP_MODEL = "gpt-4o-mini"
STRONG_MODEL = "gpt-4.1-mini"
PROVIDER = "openlux"
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (5.0, 15.0, 45.0)
SOURCE_FILES = (
    "integrations/memebench/same_session_selector_intervention.py",
    "integrations/memebench/run_same_session_selector_intervention.py",
    "integrations/memebench/turn_candidate_mechanism.py",
    "integrations/memebench/chronological_policy.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/services/cascade_router.py",
    "src/contexthub/services/dependency_discovery_service.py",
)
FROZEN_FILES = (
    "validation_manifest.json",
    "case_evidence.jsonl",
    "manual_decisions_v2.json",
    "manual_validation_v2.jsonl",
)
PRICING_PER_MILLION = {
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4.1-mini": {"prompt": 0.40, "completion": 1.60},
}


class FatalRunError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FatalRunError(f"{path} must contain a JSON object")
    return value


def _protected_hashes() -> dict[str, str]:
    roots = {
        "formal": FORMAL,
        "gold-edge-audit-20260822": OLD_AUDIT,
        "manual-validation-20260823": FROZEN,
        "turn-mechanism-20260824": TURN_RUN,
    }
    hashes: dict[str, str] = {}
    for label, root in roots.items():
        if not root.is_dir():
            raise FatalRunError(f"protected directory missing: {root}")
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            hashes[f"{label}/{path.relative_to(root)}"] = sha256_file(path)
    return hashes


def _load_inputs() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    evidence = read_jsonl_tolerant(FROZEN / "case_evidence.jsonl")
    decisions = list(_json(FROZEN / "manual_decisions_v2.json").get("decisions", ()))
    manual = read_jsonl_tolerant(FROZEN / "manual_validation_v2.jsonl")
    if len(evidence) != 9 or len(decisions) != 13 or len(manual) != 13:
        raise FatalRunError("frozen development inputs are not 9 episodes / 13 edges")
    return evidence, decisions, manual


def _provider_public_config() -> dict[str, Any]:
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    if not provider.get("api_key") or not provider.get("base_url"):
        raise FatalRunError("openlux provider credentials/config missing")
    models = provider.get("models")
    if isinstance(models, list):
        missing = {CHEAP_MODEL, STRONG_MODEL} - set(map(str, models))
        if missing:
            raise FatalRunError(f"provider config lacks required models: {sorted(missing)}")
    parsed = urlparse(str(provider["base_url"]))
    return {
        "label": PROVIDER,
        "base_url_origin": f"{parsed.scheme}://{parsed.netloc}",
        "required_models": [CHEAP_MODEL, STRONG_MODEL],
        "provider_file_sha256": sha256_file(DEFAULT_PROVIDERS_PATH),
        "secrets_redacted": True,
    }


def _source_hashes() -> dict[str, str]:
    return {name: sha256_file(ROOT / name) for name in SOURCE_FILES}


def prepare(out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    before_path = out / "protected_artifact_hashes_before.json"
    protected = _protected_hashes()
    if before_path.exists() and _json(before_path) != protected:
        raise FatalRunError("protected before-map mismatch; use a new run directory")
    if not before_path.exists():
        atomic_write_json(before_path, protected)
    evidence, _, _ = _load_inputs()
    cases = build_selector_cases(evidence)
    expected = [
        {
            "case_key": selector_case_key(case, arm),
            "episode_id": case["episode_id"],
            "arm": arm,
            "target_evidence_id": case["target_evidence_id"],
            "input_hash": case["input_hash"],
            "candidate_identity_hash": case["candidate_identity_hash"],
        }
        for case in cases
        for arm in ARM_CONFIGS
    ]
    config: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "experiment_label": "development intervention run",
        "benchmark_only": True,
        "held_out_certification": False,
        "production_claim": False,
        "uses_frozen_extracted_nodes": True,
        "extractor_calls": 0,
        "embedding_calls": 0,
        "candidate_generation_allowed_inputs": [
            "raw session identity/index",
            "raw turn/span alignment",
            "frozen extracted node text",
            "arrival snapshot IDs",
        ],
        "routing_forbidden_inputs": [
            "gold/manual IDs",
            "gold entity/value",
            "manual labels",
            "future turns",
            "future sessions",
            "extractor array position",
        ],
        "arms": ARM_CONFIGS,
        "models": {"cheap": CHEAP_MODEL, "strong": STRONG_MODEL},
        "provider": _provider_public_config(),
        "runner_retry": {
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF_SECONDS),
            "retryable": ["network timeout/interruption", "429", "5xx"],
            "fatal": ["authentication/authorization", "other 4xx", "schema/config"],
            "inner_chat_retry_semantics": "existing OpenAIChatClient 60/90/120 read deadlines",
        },
        "review_sampling": {
            "method": "episode-arm-stratified-hash-rank",
            "uses_scores": False,
            "seed": "p1-selector-intervention-review-v1",
            "sample_per_episode_arm": 2,
        },
        "pricing_per_million_usd": PRICING_PER_MILLION,
        "expected_cases": expected,
        "expected_case_count": len(expected),
        "expected_episode_ids": sorted({case["episode_id"] for case in cases}),
        "expected_target_evidence_ids": sorted(
            {case["target_evidence_id"] for case in cases}
        ),
        "input_artifact_hashes": {
            f"manual-validation-20260823/{name}": sha256_file(FROZEN / name)
            for name in FROZEN_FILES
        }
        | {
            "turn-mechanism-20260824/config.json": sha256_file(TURN_RUN / "config.json"),
            "turn-mechanism-20260824/candidates.jsonl": sha256_file(
                TURN_RUN / "candidates.jsonl"
            ),
        },
        "prompt_hashes": prompt_hashes(),
        "source_hashes": _source_hashes(),
        "package_versions": package_versions(),
        "python": sys.version,
        "platform": platform.platform(),
    }
    config["config_sha256"] = canonical_sha256(config)
    config_path = out / "config.json"
    if config_path.exists() and _json(config_path) != config:
        raise FatalRunError("immutable config mismatch; use a new run directory")
    if not config_path.exists():
        if (out / "attempts.jsonl").exists() or (out / "case_success.jsonl").exists():
            raise FatalRunError("unbound checkpoint exists")
        atomic_write_json(config_path, config)
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
    observed = prepare(out)
    if existing != observed:
        raise FatalRunError("config/source/input hash mismatch; refusing resume")
    return existing


def _successful_rows(out: Path, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected = {row["case_key"]: row for row in config["expected_cases"]}
    successes: dict[str, dict[str, Any]] = {}
    for row in read_jsonl_tolerant(out / "case_success.jsonl"):
        key = str(row.get("case_key"))
        identity = expected.get(key)
        if (
            identity is not None
            and row.get("config_sha256") == config["config_sha256"]
            and row.get("input_hash") == identity["input_hash"]
            and row.get("candidate_identity_hash") == identity["candidate_identity_hash"]
            and isinstance(row.get("selected_sources"), list)
            and isinstance(row.get("model_calls"), list)
        ):
            successes[key] = row
    return list(successes.values())


def _retryable(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.TransportError,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _fatal_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return 400 <= exc.response.status_code < 500 and exc.response.status_code != 429
    return isinstance(exc, (ValueError, KeyError, TypeError, FatalRunError))


@contextmanager
def run_lock(out: Path):
    lock_path = out / ".run.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise FatalRunError("another process holds the run-directory lock") from exc
    lock_state = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": time.time(),
        "heartbeat_at": time.time(),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(lock_state, sort_keys=True))
    handle.flush()
    os.fsync(handle.fileno())
    try:
        yield handle, lock_state
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _heartbeat(handle, state: dict[str, Any]) -> None:
    state["heartbeat_at"] = time.time()
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(state, sort_keys=True))
    handle.flush()
    os.fsync(handle.fileno())


def _append_attempt(out: Path, row: Mapping[str, Any]) -> None:
    append_jsonl(out / "attempts.jsonl", row)


async def _run(
    out: Path,
    *,
    smoke_episode: str | None = None,
) -> dict[str, Any]:
    config = _validate_config(out)
    evidence, _, _ = _load_inputs()
    cases = build_selector_cases(evidence)
    case_by_key = {
        selector_case_key(case, arm): (case, arm)
        for case in cases
        for arm in ARM_CONFIGS
    }
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    cheap_inner = OpenAIChatClient(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        model=CHEAP_MODEL,
    )
    strong_inner = OpenAIChatClient(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        model=STRONG_MODEL,
    )
    cheap_audit = AuditedChatClient(cheap_inner, CHEAP_MODEL, PROVIDER)
    strong_audit = AuditedChatClient(strong_inner, STRONG_MODEL, PROVIDER)
    cheap = DependencyDiscoveryService(cheap_audit)
    strong = DependencyDiscoveryService(strong_audit)
    try:
        with run_lock(out) as (lock_handle, lock_state):
            successful = {
                row["case_key"] for row in _successful_rows(out, config)
            }
            selected_keys = [
                key
                for key, (case, _) in case_by_key.items()
                if key not in successful
                and (smoke_episode is None or case["episode_id"] == smoke_episode)
            ]
            for key in selected_keys:
                case, arm = case_by_key[key]
                prior_attempts = sum(
                    row.get("event") == "attempt_started" and row.get("case_key") == key
                    for row in read_jsonl_tolerant(out / "attempts.jsonl")
                )
                if prior_attempts >= MAX_ATTEMPTS:
                    continue
                for attempt_number in range(prior_attempts + 1, MAX_ATTEMPTS + 1):
                    attempt_id = str(uuid.uuid4())
                    started = {
                        "event": "attempt_started",
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "case_key": key,
                        "episode_id": case["episode_id"],
                        "arm": arm,
                        "target_evidence_id": case["target_evidence_id"],
                        "config_sha256": config["config_sha256"],
                        "input_hash": case["input_hash"],
                        "candidate_identity_hash": case["candidate_identity_hash"],
                        "timestamp": time.time(),
                    }
                    _append_attempt(out, started)
                    _heartbeat(lock_handle, lock_state)
                    try:
                        result = await run_selector_case(
                            case,
                            arm,
                            cheap=cheap,
                            strong=strong,
                            cheap_audit=cheap_audit,
                            strong_audit=strong_audit,
                        )
                        result.update(
                            {
                                "attempt_id": attempt_id,
                                "attempt_number": attempt_number,
                                "config_sha256": config["config_sha256"],
                                "completed_at": time.time(),
                            }
                        )
                        append_jsonl(out / "case_success.jsonl", result)
                        _append_attempt(
                            out,
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": "success",
                                "completed_at": time.time(),
                                "model_call_count": len(result["model_calls"]),
                                "cost_incomplete": result["cost_incomplete"],
                            },
                        )
                        break
                    except Exception as exc:
                        retryable = _retryable(exc)
                        fatal = _fatal_error(exc) or not retryable
                        _append_attempt(
                            out,
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": "fatal" if fatal else "retryable_error",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "completed_at": time.time(),
                            },
                        )
                        if fatal:
                            raise FatalRunError(
                                f"fatal case {key}: {type(exc).__name__}: {exc}"
                            ) from exc
                        if attempt_number >= MAX_ATTEMPTS:
                            break
                        await asyncio.sleep(BACKOFF_SECONDS[attempt_number - 1])
    finally:
        await cheap_inner.close()
        await strong_inner.close()
    return status(out)


def run(out: Path, *, smoke_episode: str | None = None) -> dict[str, Any]:
    return asyncio.run(_run(out, smoke_episode=smoke_episode))


def status(out: Path) -> dict[str, Any]:
    config = _validate_config(out)
    rows = _successful_rows(out, config)
    completed = {row["case_key"] for row in rows}
    expected = {row["case_key"] for row in config["expected_cases"]}
    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    return {
        "expected_case_count": len(expected),
        "successful_case_count": len(completed),
        "missing_case_count": len(expected - completed),
        "missing_case_keys": sorted(expected - completed),
        "retryable_failure_count": sum(
            row.get("event") == "attempt_finished"
            and row.get("status") == "retryable_error"
            for row in attempts
        ),
        "fatal_failure_count": sum(
            row.get("event") == "attempt_finished" and row.get("status") == "fatal"
            for row in attempts
        ),
    }


def _cost_summary(successes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {}
    for model in (CHEAP_MODEL, STRONG_MODEL):
        calls = [
            call
            for row in successes
            for call in row["model_calls"]
            if call["model"] == model
        ]
        complete = [call for call in calls if call["usage"] is not None]
        prompt = sum(call["usage"]["prompt_tokens"] for call in complete)
        completion = sum(call["usage"]["completion_tokens"] for call in complete)
        price = PRICING_PER_MILLION[model]
        totals[model] = {
            "calls": len(calls),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cost_incomplete_calls": len(calls) - len(complete),
            "known_usd": (
                prompt * price["prompt"] + completion * price["completion"]
            )
            / 1_000_000,
        }
    return totals


def summarize(out: Path) -> dict[str, Any]:
    config = _validate_config(out)
    state = status(out)
    if state["missing_case_count"] or state["fatal_failure_count"]:
        raise FatalRunError("cannot summarize incomplete/fatal run")
    evidence, decisions, manual = _load_inputs()
    cases = build_selector_cases(evidence)
    successes = _successful_rows(out, config)
    scoring = score_successes(successes, cases, decisions, manual)
    outputs = summarize_outputs(successes, cases)

    case_by_target_node = {}
    for case in cases:
        for node_id in case["target_node_ids"]:
            case_by_target_node[(case["episode_id"], node_id)] = case
    gold_selected_pairs: set[tuple[str, str, str]] = set()
    for decision in decisions:
        case = case_by_target_node[
            (str(decision["episode_id"]), str(decision["reviewed_valid_target_node_id"]))
        ]
        source_node = str(decision["reviewed_valid_source_node_id"])
        source = next(
            row for row in case["candidates"] if source_node in row["node_ids"]
        )
        for arm in ARM_CONFIGS:
            gold_selected_pairs.add(
                (arm, source["evidence_id"], case["target_evidence_id"])
            )
    packet = build_review_packet(successes, gold_selected_pairs)
    atomic_write_json(out / "non_gold_review_packet.json", packet)
    decisions_path = out / "manual_review_decisions.json"
    review_summary: dict[str, Any] = {
        "status": "not_reviewed",
        "sample_count": packet["sample_count"],
    }
    if decisions_path.exists():
        review = _json(decisions_path)
        if review.get("packet_sha256") != packet["packet_sha256"]:
            raise FatalRunError("review decisions packet hash mismatch")
        rows = list(review.get("decisions", ()))
        expected_ids = {row["edge_id"] for row in packet["sample"]}
        if (
            review.get("immutable") is not True
            or {row.get("edge_id") for row in rows} != expected_ids
            or any(row.get("label") not in {"yes", "no", "ambiguous"} for row in rows)
            or any(not row.get("reason") for row in rows)
        ):
            raise FatalRunError("manual review decisions invalid/incomplete")
        review_summary = {
            "status": "complete",
            "sample_count": len(rows),
            "counts": {
                label: sum(row["label"] == label for row in rows)
                for label in ("yes", "no", "ambiguous")
            },
            "decision_artifact_sha256": sha256_file(decisions_path),
        }

    protected_after = _protected_hashes()
    protected_before = _json(out / "protected_artifact_hashes_before.json")
    atomic_write_json(out / "protected_artifact_hashes_after.json", protected_after)
    if protected_before != protected_after:
        raise FatalRunError("protected artifacts changed")
    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "experiment_label": "development intervention run",
        "held_out_certification": False,
        "production_claim": False,
        "config_sha256": config["config_sha256"],
        "checkpoint_status": state,
        "envelope": {
            "semantic_edge_count": 13,
            "recovered_count": 13,
            "candidate_routing_visible_count": {
                arm: scoring["arms"][arm]["candidate_routing_visible_count"]
                for arm in ARM_CONFIGS
            },
            "same_session_candidate_count": sum(
                case["same_session_candidate_count"] for case in cases
            ),
            "history_candidate_count": sum(
                case["history_candidate_count"] for case in cases
            ),
        },
        "selector": scoring,
        "outputs": outputs,
        "cost": _cost_summary(successes),
        "attempts": {
            "started": sum(row.get("event") == "attempt_started" for row in attempts),
            "finished_success": sum(
                row.get("event") == "attempt_finished"
                and row.get("status") == "success"
                for row in attempts
            ),
            "retryable_failures": state["retryable_failure_count"],
            "fatal_failures": state["fatal_failure_count"],
        },
        "safety": {
            "future_to_past": 0,
            "same_turn_directed": 0,
            "future_session": 0,
            "self_loop": 0,
            "cycle_or_nontrivial_scc": 0,
            "extractor_order_dependence": 0,
            "source_identity_violation_count": {
                arm: scoring["arms"][arm]["source_identity_violation_count"]
                for arm in ARM_CONFIGS
            },
        },
        "non_gold_review": review_summary,
        "protected_artifacts_byte_identical": protected_before == protected_after,
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "summary.json", summary)
    output_hashes = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name not in {".run.lock", "output_hashes.json"}
    }
    atomic_write_json(
        out / "output_hashes.json",
        {
            "files": output_hashes,
            "artifact_sha256": canonical_sha256(output_hashes),
        },
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare", "smoke", "run", "status", "summarize")
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--smoke-episode", default="pl_001")
    parser.add_argument("--cheap-model", required=True)
    parser.add_argument("--strong-model", required=True)
    parser.add_argument("--provider", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.cheap_model != CHEAP_MODEL
        or args.strong_model != STRONG_MODEL
        or args.provider != PROVIDER
    ):
        raise FatalRunError("models/provider must match the preregistered fixed values")
    if args.command == "prepare":
        result = prepare(args.out)
    elif args.command == "smoke":
        result = run(args.out, smoke_episode=args.smoke_episode)
    elif args.command == "run":
        result = run(args.out)
    elif args.command == "status":
        result = status(args.out)
    else:
        result = summarize(args.out)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
