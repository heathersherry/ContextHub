"""Run the frozen nine-case P1 same-session manual-validation extraction."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from integrations.memebench.chronological_ingest import ingest_case_chronological
from integrations.memebench.chronological_policy import registered_build_plans, registered_schedules
from integrations.memebench.gold_edge_audit import (
    append_jsonl,
    atomic_write_json,
    canonical_sha256,
    formal_path_hashes,
    map_gold_entities_to_nodes,
    read_jsonl_tolerant,
    sha256_file,
    verify_formal_artifact_hashes,
)
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.run_chronological_p1p2 import (
    package_versions,
    prompt_hashes,
    wipe_account,
)
from integrations.memebench.run_gold_edge_audit import (
    BACKOFF_SECONDS,
    CASE_TIMEOUT_SECONDS,
    FatalAuditError,
    _account_empty,
    _actual_persisted_sources,
    _build_system,
    _error_status,
    _trace_with_actual_persistence,
)
from integrations.memebench.same_session_manual_validation import (
    prepare_validation_manifest,
    summarize_manual,
    validate_manual_record,
)
from integrations.memebench.systems import DEFAULT_PROVIDERS_PATH


DEFAULT_DATA = Path("/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json")
DEFAULT_OLD_AUDIT = Path(__file__).parent / "runs" / "p1_gold_edge_audit_20260822"
DEFAULT_FORMAL = Path(__file__).parent / "runs" / "chronological_20260820" / "formal"
DEFAULT_OUT = Path(__file__).parent / "runs" / "p1_same_session_manual_validation_20260823"
POLICY = "R_full_cheap"
SCHEDULE = "async-each-session"
MAX_ATTEMPTS = 5
FIXED = {
    "extract_model": "gpt-4.1-mini",
    "p1_cheap_model": "gpt-4o-mini",
    "p1_strong_model": "gpt-4.1-mini",
    "embedding_provider": "aliyun",
    "embedding_model": "text-embedding-v4",
    "provider": "openlux",
}
RUN_SOURCE_FILES = (
    "integrations/memebench/loader.py",
    "integrations/memebench/systems.py",
    "integrations/memebench/chronological_policy.py",
    "integrations/memebench/chronological_ingest.py",
    "integrations/memebench/ingest.py",
    "integrations/memebench/cost.py",
    "integrations/memebench/embedding_retry.py",
    "integrations/memebench/gold_edge_audit.py",
    "integrations/memebench/reclassify_gold_edge_audit.py",
    "integrations/memebench/run_gold_edge_audit.py",
    "integrations/memebench/run_same_session_manual_validation.py",
    "integrations/memebench/same_session_manual_validation.py",
    "integrations/memebench/render_same_session_review.py",
    "integrations/memebench/prepare_same_session_manual_decisions.py",
    "integrations/memebench/write_same_session_manual_labels.py",
    "integrations/memebench/finalize_same_session_analysis.py",
    "src/contexthub/db/repository.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/llm/openai_client.py",
    "src/contexthub/services/conversation_extraction_service.py",
    "src/contexthub/services/dependency_discovery_service.py",
    "src/contexthub/services/cascade_router.py",
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FatalAuditError(f"{path} is not a JSON object")
    return value


def _before_path(out: Path) -> Path:
    return out / "formal_artifact_hashes_before.json"


def _assert_formal(
    out: Path, formal: Path, manifest: Mapping[str, Any] | None = None
) -> None:
    before = _json(_before_path(out))
    if manifest is not None and canonical_sha256(before) != manifest.get(
        "formal_before_manifest_sha256"
    ):
        raise FatalAuditError(
            "formal before manifest no longer matches validation manifest"
        )
    verify_formal_artifact_hashes(before, formal_path_hashes(formal))


def cmd_prepare(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    before_path = _before_path(out)
    current = formal_path_hashes(args.formal)
    if before_path.exists() and _json(before_path) != current:
        raise FatalAuditError("formal before manifest differs; use a new --out")
    if not before_path.exists():
        atomic_write_json(before_path, current)
    manifest = prepare_validation_manifest(
        data=args.data, old_audit=args.old_audit, formal=args.formal
    )
    manifest["formal_before_manifest_sha256"] = canonical_sha256(current)
    # The guard hash is part of the immutable payload.
    from integrations.memebench.gold_edge_audit import manifest_sha256

    manifest["manifest_sha256"] = manifest_sha256(manifest)
    path = out / "validation_manifest.json"
    if path.exists() and _json(path) != manifest:
        raise FatalAuditError("immutable validation manifest differs")
    if not path.exists():
        atomic_write_json(path, manifest)
    print(f"manifest={path}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    return 0


def _load_manifest(args: argparse.Namespace) -> dict[str, Any]:
    from integrations.memebench.gold_edge_audit import validate_manifest_hash

    manifest = _json(Path(args.out) / "validation_manifest.json")
    validate_manifest_hash(manifest)
    if manifest["policy"] != POLICY or manifest["episode_count"] != 9:
        raise FatalAuditError("validation manifest policy/count mismatch")
    _assert_formal(Path(args.out), Path(args.formal), manifest)
    return manifest


def _run_config(args: argparse.Namespace, manifest: Mapping[str, Any]) -> dict[str, Any]:
    for key, expected in FIXED.items():
        if getattr(args, key) != expected:
            raise FatalAuditError(f"{key} must be {expected!r}")
    payload = {
        "manifest_sha256": manifest["manifest_sha256"],
        "policy": POLICY,
        "schedule": SCHEDULE,
        "models": {**FIXED, "chat_model_unused": args.chat_model},
        "case_timeout": float(args.case_timeout),
        "max_attempts": int(args.max_attempts),
        "source_hashes": {
            name: sha256_file(Path(__file__).resolve().parents[2] / name)
            for name in RUN_SOURCE_FILES
        },
        "prompt_hashes": prompt_hashes(),
        "provider_config_sha256": sha256_file(DEFAULT_PROVIDERS_PATH),
        "package_versions": package_versions(),
    }
    payload["run_config_hash"] = canonical_sha256(payload)
    return payload


def _bind_config(out: Path, config: Mapping[str, Any]) -> None:
    path = out / "run_config.json"
    if path.exists() and _json(path) != dict(config):
        raise FatalAuditError("run config hash mismatch; use a new --out")
    if not path.exists():
        if any((out / name).exists() for name in ("attempts.jsonl", "case_evidence.jsonl")):
            raise FatalAuditError("unbound checkpoint")
        atomic_write_json(path, config)


def _case_key(episode_id: str) -> str:
    return f"same-session|{episode_id}|{POLICY}"


def validation_account(episode_id: str) -> str:
    return f"p1sameval-v1-{canonical_sha256(episode_id)[:16]}"


def _attempts(out: Path) -> list[dict[str, Any]]:
    return read_jsonl_tolerant(out / "attempts.jsonl")


def _evidence_checkpoint_identity_valid(row: Mapping[str, Any]) -> bool:
    required = {
        "case_key",
        "episode_id",
        "hop",
        "policy",
        "target_entity",
        "gold_edge_ids",
        "gold_edge_ids_sha256",
    }
    if not required <= set(row):
        return False
    ids = list(map(str, row["gold_edge_ids"]))
    return (
        row["case_key"] == _case_key(str(row["episode_id"]))
        and row["policy"] == POLICY
        and isinstance(row["hop"], int)
        and bool(row["target_entity"])
        and len(ids) == len(set(ids))
        and ids == sorted(ids)
        and row["gold_edge_ids_sha256"] == canonical_sha256(ids)
    )


def _successes(
    out: Path, config_hash: str, *, require_checkpoint_identity: bool = False
) -> set[str]:
    evidence = {
        (row.get("case_key"), row.get("attempt_id"))
        for row in read_jsonl_tolerant(out / "case_evidence.jsonl")
        if row.get("run_config_hash") == config_hash
        and (
            not require_checkpoint_identity
            or _evidence_checkpoint_identity_valid(row)
        )
    }
    success_rows = {
        (row.get("case_key"), row.get("attempt_id"))
        for row in read_jsonl_tolerant(out / "case_success.jsonl")
        if row.get("run_config_hash") == config_hash
        and row.get("account_cleanup_complete") is True
    }
    return {
        str(row["case_key"])
        for row in _attempts(out)
        if row.get("event") == "attempt_finished"
        and row.get("status") == "success"
        and row.get("run_config_hash") == config_hash
        and (row.get("case_key"), row.get("attempt_id")) in evidence & success_rows
    }


def _pid_alive(pid: int, hostname: str) -> bool:
    if hostname != socket.gethostname() or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recover_stale(out: Path, config_hash: str, *, stale_after: float = 3600.0) -> int:
    journal = _attempts(out)
    finished = {row.get("attempt_id") for row in journal if row.get("event") == "attempt_finished"}
    stale = [
        row
        for row in journal
        if row.get("event") == "attempt_started"
        and row.get("run_config_hash") == config_hash
        and row.get("attempt_id") not in finished
        and not (
            _pid_alive(int(row.get("owner_pid") or 0), str(row.get("owner_host") or ""))
            and time.time() - float(row.get("heartbeat_timestamp", row.get("timestamp", 0)))
            < stale_after
        )
    ]
    for row in stale:
        terminal = {
            **row,
            "event": "attempt_finished",
            "timestamp": time.time(),
            "status": "retryable_error",
            "error_type": "stale_attempt",
            "error_message": "attempt_started has no matching attempt_finished",
            "cost_incomplete": True,
        }
        append_jsonl(out / "attempts.jsonl", terminal)
    return len(stale)


@contextmanager
def exclusive_run_lock(out: Path):
    """Prevent a second runner from reclaiming a live process's attempts."""

    path = out / ".same_session_runner.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FatalAuditError("another same-session runner is active") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps({"pid": os.getpid(), "hostname": socket.gethostname()}) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _summary_state(out: Path, manifest: Mapping[str, Any], config_hash: str) -> dict[str, Any]:
    journal = _attempts(out)
    expected = {_case_key(row["episode_id"]) for row in manifest["episodes"]}
    success = _successes(out, config_hash, require_checkpoint_identity=True)
    return {
        "expected_cases": len(expected),
        "success_cases": len(expected & success),
        "missing_case_keys": sorted(expected - success),
        "retryable_attempts": sum(
            row.get("event") == "attempt_finished"
            and row.get("status") == "retryable_error"
            and row.get("run_config_hash") == config_hash
            for row in journal
        ),
        "fatal_attempts": sum(
            row.get("event") == "attempt_finished"
            and row.get("status") == "fatal_error"
            and row.get("run_config_hash") == config_hash
            for row in journal
        ),
        "stale_recoveries": sum(
            row.get("error_type") == "stale_attempt"
            and row.get("run_config_hash") == config_hash
            for row in journal
        ),
        "process_restarts": sum(row.get("event") == "worker_restarted" for row in journal),
        "cost_incomplete": sum(bool(row.get("cost_incomplete")) for row in journal),
    }


def _case_index(args: argparse.Namespace, manifest: Mapping[str, Any]) -> dict[str, Any]:
    wanted = {
        (int(row["representative_hop"]), row["episode_id"], row["representative_target_entity"])
        for row in manifest["episodes"]
    }
    result = {}
    episodes = load_episodes(args.data)
    for hop in (1, 2):
        for case in extract_cascade_cases(episodes, hop=hop):
            key = (case.hop, case.episode_id, case.target_entity)
            if key in wanted:
                result[case.episode_id] = case
    if len(result) != 9:
        raise FatalAuditError(f"expected 9 representative cases, got {len(result)}")
    return result


async def _execute(args, case, semantic_edges, account):
    system = await _build_system(args)
    evidence = None
    try:
        await wipe_account(system, account)
        if not await _account_empty(system, account):
            raise FatalAuditError("validation account not empty")
        async with system.repo.session(account) as db:
            result = await ingest_case_chronological(
                db,
                case,
                account,
                system.embedding.embed_batch,
                extractor=system.extractor,
                disamb_cheap=system.cascade_cheap_svc,
                disamb_strong=system.cascade_strong_svc,
                edge_cheap=system.cascade_cheap_svc,
                edge_strong=system.cascade_strong_svc,
                plan=registered_build_plans()[POLICY],
                schedule=registered_schedules()[SCHEDULE],
                audit_trace=True,
            )
            actual = await _actual_persisted_sources(db)
        nodes = result.audit_trace["nodes"]
        traces = _trace_with_actual_persistence(result.audit_trace["consolidations"], actual)
        mappings = map_gold_entities_to_nodes(case.entities, nodes)
        edge_matches = []
        by_id = {row["node_id"]: row for row in nodes}
        for edge in semantic_edges:
            source = mappings[edge["gold_source_entity"]]
            target = mappings[edge["gold_target_entity"]]
            edge_matches.append(
                {
                    "semantic_edge_id": edge["semantic_edge_id"],
                    "source_before_value": edge["source_before_value"],
                    "target_before_value": edge["target_before_value"],
                    "source_match_method": source.match_method,
                    "target_match_method": target.match_method,
                    "source_nodes": [by_id[node_id] for node_id in source.node_ids],
                    "target_nodes": [by_id[node_id] for node_id in target.node_ids],
                }
            )
        gold_edge_ids = sorted(
            f"{case.episode_id}|{case.hop}|{edge.source}|{edge.target}"
            for edge in case.edges
        )
        evidence = {
            "nodes": nodes,
            "candidate_traces": traces,
            "gold_node_matches": edge_matches,
            "hop": case.hop,
            "policy": POLICY,
            "target_entity": case.target_entity,
            "gold_edge_ids": gold_edge_ids,
            "gold_edge_ids_sha256": canonical_sha256(gold_edge_ids),
        }
        return evidence
    finally:
        try:
            await wipe_account(system, account)
            cleanup_complete = await _account_empty(system, account)
            if evidence is not None:
                evidence["account_cleanup_complete"] = cleanup_complete
            if not cleanup_complete:
                raise FatalAuditError("validation account cleanup verification failed")
        finally:
            await system.close()


async def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    with exclusive_run_lock(out):
        return await _cmd_run_locked(args, out)


async def _cmd_run_locked(args: argparse.Namespace, out: Path) -> int:
    manifest = _load_manifest(args)
    config = _run_config(args, manifest)
    _bind_config(out, config)
    config_hash = config["run_config_hash"]
    _recover_stale(out, config_hash)
    cases = _case_index(args, manifest)
    edges_by_episode = {
        episode["episode_id"]: [
            edge
            for edge in manifest["semantic_edges"]
            if edge["episode_id"] == episode["episode_id"]
        ]
        for episode in manifest["episodes"]
    }
    for episode in manifest["episodes"]:
        episode_id = episode["episode_id"]
        key = _case_key(episode_id)
        if key in _successes(out, config_hash, require_checkpoint_identity=True):
            continue
        attempts = sum(
            row.get("event") == "attempt_started" and row.get("case_key") == key
            for row in _attempts(out)
        )
        while attempts < args.max_attempts:
            attempts += 1
            attempt_id = str(uuid.uuid4())
            started = {
                "event": "attempt_started",
                "case_key": key,
                "attempt_id": attempt_id,
                "attempt_number": attempts,
                "timestamp": time.time(),
                "heartbeat_timestamp": time.time(),
                "owner_pid": os.getpid(),
                "owner_host": socket.gethostname(),
                "run_config_hash": config_hash,
            }
            append_jsonl(out / "attempts.jsonl", started)
            try:
                evidence = await asyncio.wait_for(
                    _execute(
                        args,
                        cases[episode_id],
                        edges_by_episode[episode_id],
                        validation_account(episode_id),
                    ),
                    timeout=args.case_timeout,
                )
                cleanup_complete = bool(evidence.pop("account_cleanup_complete"))
                append_jsonl(
                    out / "case_evidence.jsonl",
                    {
                        **evidence,
                        "case_key": key,
                        "attempt_id": attempt_id,
                        "episode_id": episode_id,
                        "run_config_hash": config_hash,
                    },
                )
                append_jsonl(
                    out / "case_success.jsonl",
                    {
                        "case_key": key,
                        "attempt_id": attempt_id,
                        "run_config_hash": config_hash,
                        "account": validation_account(episode_id),
                        "account_cleanup_complete": cleanup_complete,
                    },
                )
                append_jsonl(
                    out / "attempts.jsonl",
                    {
                        **started,
                        "event": "attempt_finished",
                        "timestamp": time.time(),
                        "status": "success",
                        "cost_incomplete": False,
                    },
                )
                break
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                status, retryable = _error_status(exc)
                append_jsonl(
                    out / "attempts.jsonl",
                    {
                        **started,
                        "event": "attempt_finished",
                        "timestamp": time.time(),
                        "status": status,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "cost_incomplete": isinstance(exc, asyncio.TimeoutError),
                    },
                )
                if not retryable or status == "fatal_error":
                    return 2
                if attempts < args.max_attempts:
                    await asyncio.sleep(BACKOFF_SECONDS[attempts - 1])
        if key not in _successes(out, config_hash, require_checkpoint_identity=True):
            return 2
    atomic_write_json(out / "completeness.json", _summary_state(out, manifest, config_hash))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    manifest = _load_manifest(args)
    config = _run_config(args, manifest)
    _bind_config(Path(args.out), config)
    state = _summary_state(Path(args.out), manifest, config["run_config_hash"])
    atomic_write_json(Path(args.out) / "completeness.json", state)
    return 0 if not state["missing_case_keys"] and not state["fatal_attempts"] else 1


def cmd_summarize(args: argparse.Namespace) -> int:
    from integrations.memebench.finalize_same_session_analysis import finalize

    finalize(Path(args.out), Path(args.formal))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from integrations.memebench.finalize_same_session_analysis import finalize

    finalize(Path(args.out), Path(args.formal))
    print("manual_validation_v2_valid=true")
    return 0


def cmd_verify_formal(args: argparse.Namespace) -> int:
    before = _json(_before_path(Path(args.out)))
    after = formal_path_hashes(args.formal)
    atomic_write_json(Path(args.out) / "formal_artifact_hashes_after.json", after)
    verify_formal_artifact_hashes(before, after)
    if _before_path(Path(args.out)).read_bytes() != (
        Path(args.out) / "formal_artifact_hashes_after.json"
    ).read_bytes():
        raise FatalAuditError("formal hash manifests are not byte-identical")
    print("formal_artifact_hashes_match=true")
    return 0


def cmd_run_until_complete(args: argparse.Namespace) -> int:
    argv = list(sys.argv[1:])
    argv[argv.index("run-until-complete")] = "run"
    for restart in range(11):
        completed = subprocess.run(
            [sys.executable, "-m", "integrations.memebench.run_same_session_manual_validation", *argv],
            cwd=Path(__file__).resolve().parents[2],
            check=False,
        )
        if completed.returncode in (0, 2):
            return completed.returncode
        append_jsonl(
            Path(args.out) / "attempts.jsonl",
            {"event": "worker_restarted", "restart_number": restart + 1, "timestamp": time.time()},
        )
    return 1


def _paths(parser):
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--old-audit", type=Path, default=DEFAULT_OLD_AUDIT)
    parser.add_argument("--formal", type=Path, default=DEFAULT_FORMAL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)


def _models(parser):
    parser.add_argument("--chat-model", required=True)
    parser.add_argument("--extract-model", required=True)
    parser.add_argument("--p1-cheap-model", required=True)
    parser.add_argument("--p1-strong-model", required=True)
    parser.add_argument("--embedding-provider", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--case-timeout", type=float, default=CASE_TIMEOUT_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    prepare = subs.add_parser("prepare-manifest")
    _paths(prepare)
    prepare.set_defaults(handler=cmd_prepare, is_async=False)
    for name, handler, is_async in (
        ("run", cmd_run, True),
        ("run-until-complete", cmd_run_until_complete, False),
        ("check", cmd_check, False),
    ):
        item = subs.add_parser(name)
        _paths(item)
        _models(item)
        item.set_defaults(handler=handler, is_async=is_async)
    for name, handler in (
        ("summarize", cmd_summarize),
        ("validate", cmd_validate),
    ):
        item = subs.add_parser(name)
        _paths(item)
        item.set_defaults(handler=handler, is_async=False)
    verify = subs.add_parser("verify-formal")
    _paths(verify)
    verify.set_defaults(handler=cmd_verify_formal, is_async=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.handler(args)) if args.is_async else args.handler(args)
    except (FatalAuditError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
