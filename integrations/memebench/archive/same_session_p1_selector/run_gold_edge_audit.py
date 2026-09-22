"""Run the selection-only P1 gold-edge stage-conditioned audit.

Gold labels are loaded only after chronological ingest has completed. They are
never passed to extraction, disambiguation, candidate routing, or discovery.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from integrations.memebench.chronological_ingest import ingest_case_chronological
from integrations.memebench.chronological_policy import (
    build_plan_to_json,
    registered_build_plans,
    registered_schedules,
)
from integrations.memebench.gold_edge_audit import (
    AUDIT_SCHEMA_VERSION,
    ENTITY_MATCHER_VERSION,
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    attempt_finished_event,
    attempt_started_event,
    canonical_sha256,
    classify_gold_edge,
    completeness_report,
    formal_path_hashes,
    manifest_sha256,
    map_gold_entities_to_nodes,
    read_jsonl_tolerant,
    records_from_last_complete_successes,
    recovery_events_for_stale,
    sha256_file,
    summarize_audit,
    validate_checkpoint_config,
    validate_manifest_hash,
    validate_selection_evaluation_disjoint,
    verify_formal_artifact_hashes,
)
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.run_chronological_p1p2 import (
    package_versions,
    prompt_hashes,
    wipe_account,
)
from integrations.memebench.run_eval import _token_delta, _token_snap
from integrations.memebench.systems import build_system


DEFAULT_DATA = Path(
    "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json"
)
DEFAULT_FORMAL = Path(__file__).parent / "runs" / "chronological_20260820" / "formal"
DEFAULT_OUT = Path(__file__).parent / "runs" / "p1_gold_edge_audit_20260822"
POLICIES = ("R_full_cheap", "R_full_verify")
SCHEDULE = "async-each-session"
BACKOFF_SECONDS = (10, 30, 60, 120, 240)
MAX_ATTEMPTS = 5
MAX_WORKER_RESTARTS = 10
CASE_TIMEOUT_SECONDS = 1800.0
FIXED_MODELS = {
    "extract_model": "gpt-4.1-mini",
    "p1_cheap_model": "gpt-4o-mini",
    "p1_strong_model": "gpt-4.1-mini",
    "embedding_provider": "aliyun",
    "embedding_model": "text-embedding-v4",
    "provider": "openlux",
}
SOURCE_FILES = (
    "integrations/memebench/loader.py",
    "integrations/memebench/gold_edge_audit.py",
    "integrations/memebench/run_gold_edge_audit.py",
    "integrations/memebench/chronological_ingest.py",
    "integrations/memebench/chronological_policy.py",
    "integrations/memebench/ingest.py",
    "integrations/memebench/systems.py",
    "integrations/memebench/cost.py",
    "integrations/memebench/embedding_retry.py",
    "src/contexthub/db/repository.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/llm/openai_client.py",
    "src/contexthub/services/cascade_router.py",
    "src/contexthub/services/dependency_discovery_service.py",
    "src/contexthub/services/conversation_extraction_service.py",
)


class FatalAuditError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FatalAuditError(f"{path} is not a JSON object")
    return payload


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def source_fingerprint() -> str:
    root = _repo_root()
    digest = hashlib.sha256()
    for relative in SOURCE_FILES:
        path = root / relative
        if not path.is_file():
            raise FatalAuditError(f"source fingerprint file missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _stable_ids_hash(values: Sequence[str]) -> str:
    return canonical_sha256(sorted(map(str, values)))


def case_key(hop: int, episode_id: str, target_entity: str, policy: str) -> str:
    return f"{hop}|{episode_id}|{target_entity}|{policy}"


def audit_account(
    hop: int, policy: str, episode_id: str, target_entity: str
) -> str:
    payload = f"{hop}|{policy}|{episode_id}|{target_entity}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    safe_policy = policy.lower().replace("_", "-")[:12]
    return f"p1audit-v1-{hop}-{safe_policy}-{digest}"[:60]


def _formal_before_path(out: Path) -> Path:
    return out / "formal_artifact_hashes_before.json"


def assert_formal_guard(out: Path, formal: Path) -> dict[str, str]:
    before_path = _formal_before_path(out)
    if not before_path.is_file():
        raise FatalAuditError(f"missing formal before-hash manifest: {before_path}")
    before = _json(before_path)
    current = formal_path_hashes(formal)
    try:
        verify_formal_artifact_hashes(before, current)
    except ValueError as exc:
        raise FatalAuditError(str(exc)) from exc
    return before


def _selection_rows(formal: Path, hop: int) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for policy in registered_build_plans():
        rows = _json(
            formal / f"hop{hop}" / "p1_selection" / policy / "cases.json"
        ).get("cases")
        if not isinstance(rows, list):
            raise FatalAuditError(f"invalid cases for hop{hop}/{policy}")
        result[policy] = {str(row["episode_id"]): row for row in rows}
    return result


def select_audit_episodes(
    formal: Path, *, smoke: bool = False
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, dict[str, Any]]]:
    selected: dict[int, list[dict[str, Any]]] = {}
    split_docs: dict[int, dict[str, Any]] = {}
    for hop in (1, 2):
        split_path = formal / f"hop{hop}" / "split.json"
        split_doc = _json(split_path)
        split = split_doc["split"]
        validate_selection_evaluation_disjoint(
            split["selection_ids"], split["certification_ids"]
        )
        split_docs[hop] = {
            "path": str(split_path),
            "sha256": sha256_file(split_path),
            "split_hash": split["split_hash"],
            "selection_ids": list(split["selection_ids"]),
            "evaluation_ids": list(split["certification_ids"]),
            "selection_ids_hash": _stable_ids_hash(split["selection_ids"]),
            "evaluation_ids_hash": _stable_ids_hash(split["certification_ids"]),
        }
        by_policy = _selection_rows(formal, hop)
        cheap = by_policy["R_full_cheap"]
        if hop == 1:
            rescued = sorted(
                episode_id
                for episode_id, row in by_policy["T_current_tau"].items()
                if row["p1"]["graph_miss"]
                and not cheap[episode_id]["p1"]["graph_miss"]
            )
            all_miss = sorted(
                episode_id
                for episode_id in cheap
                if all(
                    by_policy[policy][episode_id]["p1"]["graph_miss"]
                    for policy in by_policy
                )
            )
            one = [
                episode_id
                for episode_id in all_miss
                if int(cheap[episode_id]["p1"]["n_gold"])
                - int(cheap[episode_id]["p1"]["n_tp"])
                == 1
            ][:4]
            multi = [
                episode_id
                for episode_id in all_miss
                if int(cheap[episode_id]["p1"]["n_gold"])
                - int(cheap[episode_id]["p1"]["n_tp"])
                >= 2
            ][:3]
            ids_and_strata = (
                [(item, "T_current_tau_miss_R_full_cheap_pass") for item in rescued]
                + [(item, "all_policy_miss_R_full_cheap_1_edge") for item in one]
                + [(item, "all_policy_miss_R_full_cheap_2plus_edges") for item in multi]
            )
            if len(ids_and_strata) != 10:
                raise FatalAuditError(
                    f"hop1 deterministic strata produced {len(ids_and_strata)} episodes"
                )
        else:
            misses = {
                episode_id: int(row["p1"]["n_gold"]) - int(row["p1"]["n_tp"])
                for episode_id, row in cheap.items()
            }
            one = sorted(item for item, count in misses.items() if count == 1)[:5]
            two = sorted(item for item, count in misses.items() if count == 2)[:4]
            three = sorted(item for item, count in misses.items() if count >= 3)[:1]
            if not three:
                remaining = [
                    item
                    for item in sorted(key for key, count in misses.items() if count == 2)
                    if item not in two
                ]
                three = remaining[:1]
            ids_and_strata = (
                [(item, "R_full_cheap_1_edge") for item in one]
                + [(item, "R_full_cheap_2_edges") for item in two]
                + [(item, "R_full_cheap_3plus_edges") for item in three]
            )
            if len(ids_and_strata) != 10:
                raise FatalAuditError(
                    f"hop2 deterministic strata produced {len(ids_and_strata)} episodes"
                )
        selected[hop] = [
            {
                "episode_id": episode_id,
                "stratum": stratum,
                "R_full_cheap_missed_edges": int(
                    cheap[episode_id]["p1"]["n_gold"]
                )
                - int(cheap[episode_id]["p1"]["n_tp"]),
            }
            for episode_id, stratum in ids_and_strata
        ]
    if smoke:
        selected = {1: [selected[1][0]]}
    return selected, split_docs


def prepare_manifest(args: argparse.Namespace) -> int:
    out = Path(args.out)
    formal = Path(args.formal)
    data = Path(args.data)
    before = assert_formal_guard(out, formal)
    selected, splits = select_audit_episodes(formal, smoke=args.smoke)
    cases_by_hop = {
        hop: {
            case.episode_id: case
            for case in extract_cascade_cases(load_episodes(data), hop=hop)
        }
        for hop in selected
    }
    episode_rows: list[dict[str, Any]] = []
    expected_keys: list[str] = []
    for hop, rows in selected.items():
        selection_ids = set(splits[hop]["selection_ids"])
        evaluation_ids = set(splits[hop]["evaluation_ids"])
        for row in rows:
            episode_id = row["episode_id"]
            if episode_id not in selection_ids or episode_id in evaluation_ids:
                raise FatalAuditError(f"audit episode is not selection-only: {episode_id}")
            case = cases_by_hop[hop].get(episode_id)
            if case is None:
                raise FatalAuditError(f"dataset case missing: hop{hop}/{episode_id}")
            edge_pairs = sorted({(edge.source, edge.target) for edge in case.edges})
            enriched = {
                **row,
                "hop": hop,
                "target_entity": case.target_entity,
                "expected_gold_edge_count": len(edge_pairs),
                "gold_edge_ids_hash": canonical_sha256(
                    [f"{episode_id}|{hop}|{source}|{target}" for source, target in edge_pairs]
                ),
            }
            episode_rows.append(enriched)
            expected_keys.extend(
                case_key(hop, episode_id, case.target_entity, policy)
                for policy in POLICIES
            )
    plans = registered_build_plans()
    policy_defs = {name: build_plan_to_json(plans[name]) for name in POLICIES}
    manifest: dict[str, Any] = {
        "manifest_version": "p1-gold-edge-audit-manifest-v1",
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "smoke": bool(args.smoke),
        "selection_only_diagnostic": True,
        "data_path": str(data),
        "data_sha256": sha256_file(data),
        "formal_root": str(formal),
        "formal_before_manifest_path": str(_formal_before_path(out)),
        "formal_before_manifest_sha256": sha256_file(_formal_before_path(out)),
        "formal_file_count": len(before),
        "splits": {str(hop): splits[hop] for hop in splits},
        "selected_episodes": episode_rows,
        "selected_episode_ids": {
            str(hop): [row["episode_id"] for row in rows]
            for hop, rows in selected.items()
        },
        "expected_case_keys": expected_keys,
        "expected_case_count": len(expected_keys),
        "policies": policy_defs,
        "policy_hashes": {
            name: canonical_sha256(policy_defs[name]) for name in POLICIES
        },
        "sample_selection_algorithm": (
            "hop1: all T_current_tau-miss/R_full_cheap-pass, then sorted first "
            "4 all-policy-miss with one cheap miss and first 3 with >=2; "
            "hop2: sorted first 5/4/1 with cheap miss counts 1/2/3+"
        ),
        "sample_selection_note": (
            "Smoke uses the first deterministically selected hop1 episode."
            if args.smoke
            else "Actual formal artifacts supplied every requested stratum."
        ),
        "entity_matcher_version": ENTITY_MATCHER_VERSION,
    }
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    dest = out / ("smoke/audit_manifest.json" if args.smoke else "audit_manifest.json")
    if dest.exists():
        existing = _json(dest)
        if existing != manifest:
            raise FatalAuditError(
                f"immutable manifest differs: {dest}; use a new --out"
            )
    else:
        atomic_write_json(dest, manifest)
    print(f"manifest={dest}", flush=True)
    print(f"manifest_sha256={manifest['manifest_sha256']}", flush=True)
    print(f"expected_cases={manifest['expected_case_count']}", flush=True)
    return 0


def _manifest_path(out: Path, smoke: bool) -> Path:
    return out / ("smoke/audit_manifest.json" if smoke else "audit_manifest.json")


def load_manifest(out: Path, *, smoke: bool) -> dict[str, Any]:
    path = _manifest_path(out, smoke)
    manifest = _json(path)
    validate_manifest_hash(manifest)
    if bool(manifest.get("smoke")) != smoke:
        raise FatalAuditError(f"manifest smoke mode mismatch: {path}")
    if manifest.get("audit_schema_version") != AUDIT_SCHEMA_VERSION:
        raise FatalAuditError("unsupported audit schema")
    if len(manifest["expected_case_keys"]) != int(manifest["expected_case_count"]):
        raise FatalAuditError("manifest expected case count mismatch")
    return manifest


def _validate_fixed_args(args: argparse.Namespace) -> None:
    for name, expected in FIXED_MODELS.items():
        actual = getattr(args, name)
        if actual != expected:
            raise FatalAuditError(
                f"{name} must remain frozen at {expected!r}, got {actual!r}"
            )


def run_config(args: argparse.Namespace, manifest: Mapping[str, Any]) -> dict[str, Any]:
    _validate_fixed_args(args)
    plans = registered_build_plans()
    prompts = prompt_hashes()
    payload = {
        "data_path": str(Path(args.data)),
        "data_sha256": sha256_file(args.data),
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "audit_manifest_sha256": manifest["manifest_sha256"],
        "selection_ids_hashes": {
            hop: split["selection_ids_hash"]
            for hop, split in manifest["splits"].items()
        },
        "evaluation_ids_hashes": {
            hop: split["evaluation_ids_hash"]
            for hop, split in manifest["splits"].items()
        },
        "split_hashes": {
            hop: split["split_hash"] for hop, split in manifest["splits"].items()
        },
        "plans": {
            name: build_plan_to_json(plans[name]) for name in POLICIES
        },
        "plan_hashes": {
            name: canonical_sha256(build_plan_to_json(plans[name]))
            for name in POLICIES
        },
        "models": {
            **FIXED_MODELS,
            "chat_model_unused": args.chat_model,
            "embedding_dimension": 1536,
        },
        "prompt_hashes": {
            "extraction": prompts["extract"],
            "discovery": prompts["discovery"],
        },
        "provider_config_sha256": sha256_file(
            _repo_root() / "model_providers.local.json"
        ),
        "source_fingerprint": source_fingerprint(),
        "entity_matcher_version": ENTITY_MATCHER_VERSION,
        "formal_before_manifest_sha256": manifest[
            "formal_before_manifest_sha256"
        ],
        "formal_root": str(Path(args.formal)),
        "schedule": SCHEDULE,
        "case_timeout_seconds": float(args.case_timeout),
        "max_attempts": int(args.max_attempts),
        "package_versions": package_versions(),
    }
    payload["run_config_hash"] = canonical_sha256(payload)
    return payload


def bind_run_config(run_dir: Path, payload: Mapping[str, Any]) -> str:
    path = run_dir / "run_config.json"
    expected = str(payload["run_config_hash"])
    checkpoint_paths = (
        run_dir / "attempts.jsonl",
        run_dir / "case_success.jsonl",
        run_dir / "gold_edge_audit.jsonl",
    )
    if path.exists():
        existing = _json(path)
        if existing != dict(payload):
            raise FatalAuditError(
                f"run_config hash mismatch at {path}; use a new --out"
            )
    elif any(item.exists() and item.stat().st_size for item in checkpoint_paths):
        raise FatalAuditError("unbound audit checkpoint; use a new --out")
    else:
        atomic_write_json(path, dict(payload))
    return expected


def _run_dir(out: Path, smoke: bool) -> Path:
    return out / "smoke" if smoke else out


def _expected_counts(manifest: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in manifest["selected_episodes"]:
        for policy in POLICIES:
            key = case_key(
                int(row["hop"]),
                str(row["episode_id"]),
                str(row["target_entity"]),
                policy,
            )
            counts[key] = int(row["expected_gold_edge_count"])
    return counts


def _checkpoint(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        read_jsonl_tolerant(run_dir / "attempts.jsonl"),
        read_jsonl_tolerant(run_dir / "gold_edge_audit.jsonl"),
        read_jsonl_tolerant(run_dir / "case_success.jsonl"),
    )


def compute_completeness(
    run_dir: Path, manifest: Mapping[str, Any], config_hash: str
) -> dict[str, Any]:
    journal, records, successes = _checkpoint(run_dir)
    validate_checkpoint_config(journal, run_config_hash=config_hash)
    report = completeness_report(
        manifest["expected_case_keys"],
        journal,
        records,
        run_config_hash=config_hash,
        expected_gold_edges_by_case=_expected_counts(manifest),
        case_success_records=successes,
    )
    incomplete_events = [
        event
        for event in journal
        if bool(event.get("cost_incomplete"))
        and event.get("run_config_hash") == config_hash
        and event.get("event") in {"attempt_finished", "attempt_recovered"}
    ]
    report.update(
        {
            "retryable_attempts": sum(
                event.get("event") == "attempt_finished"
                and event.get("status") == "retryable_error"
                and event.get("run_config_hash") == config_hash
                for event in journal
            ),
            "fatal_attempts": sum(
                event.get("event") == "attempt_finished"
                and event.get("status") == "fatal_error"
                and event.get("run_config_hash") == config_hash
                for event in journal
            ),
            "stale_recoveries": sum(
                event.get("event") == "attempt_recovered"
                and event.get("run_config_hash") == config_hash
                for event in journal
            ),
            "process_restarts": sum(
                event.get("event") == "worker_restarted"
                and event.get("run_config_hash") == config_hash
                for event in journal
            ),
            "cost_incomplete_attempts": len(
                {
                    str(event.get("attempt_id"))
                    for event in incomplete_events
                    if event.get("attempt_id")
                }
            ),
            "cost_incomplete_events": len(incomplete_events),
        }
    )
    atomic_write_json(run_dir / "completeness.json", report)
    return report


def _heartbeat_markdown(report: Mapping[str, Any], *, currently_running: str = "") -> str:
    return "\n".join(
        (
            "# P1 Gold-Edge Audit State",
            "",
            f"- expected_cases: {report['expected_cases']}",
            f"- success_cases: {report['success_cases']}",
            f"- retryable_cases: {len(report['retryable_case_keys'])}",
            f"- fatal_cases: {len(report['fatal_case_keys'])}",
            f"- stale_cases: {report['stale_cases']}",
            f"- currently_running: {currently_running or 'none'}",
            f"- remaining_cases: {len(report['missing_case_keys'])}",
            f"- retryable_attempts: {report.get('retryable_attempts', 0)}",
            f"- stale_recoveries: {report.get('stale_recoveries', 0)}",
            f"- process_restarts: {report.get('process_restarts', 0)}",
            f"- cost_incomplete_attempts: {report.get('cost_incomplete_attempts', 0)}",
            "",
        )
    )


def update_state(run_dir: Path, report: Mapping[str, Any], *, running: str = "") -> None:
    atomic_write_text(
        run_dir / "AUDIT_STATE.md",
        _heartbeat_markdown(report, currently_running=running),
    )
    print(
        " ".join(
            (
                f"expected_cases={report['expected_cases']}",
                f"success_cases={report['success_cases']}",
                f"retryable_cases={len(report['retryable_case_keys'])}",
                f"fatal_cases={len(report['fatal_case_keys'])}",
                f"stale_cases={report['stale_cases']}",
                f"currently_running={running or 'none'}",
                f"remaining_cases={len(report['missing_case_keys'])}",
            )
        ),
        flush=True,
    )


def _case_index(data: Path, manifest: Mapping[str, Any]) -> dict[tuple[int, str], Any]:
    wanted = {
        (int(row["hop"]), str(row["episode_id"]))
        for row in manifest["selected_episodes"]
    }
    episodes = load_episodes(data)
    result: dict[tuple[int, str], Any] = {}
    for hop in sorted({hop for hop, _ in wanted}):
        for case in extract_cascade_cases(episodes, hop=hop):
            key = (hop, case.episode_id)
            if key in wanted:
                if key in result:
                    raise FatalAuditError(f"multiple Cascade cases for {key}")
                result[key] = case
    missing = wanted - result.keys()
    if missing:
        raise FatalAuditError(f"dataset missing selected cases: {sorted(missing)}")
    return result


async def _build_system(args: argparse.Namespace):
    return await build_system(
        chat_model=args.chat_model,
        oracle_model=args.p1_strong_model,
        extract_model=args.extract_model,
        provider_label=args.provider,
        embedding_provider_label=args.embedding_provider,
        embedding_model=args.embedding_model,
        cascade=True,
        cascade_cheap_model=args.p1_cheap_model,
        cascade_strong_model=args.p1_strong_model,
        p2_cascade=False,
    )


async def _account_empty(system, account: str) -> bool:
    async with system.pool.acquire() as conn:
        contexts = await conn.fetchval(
            "SELECT COUNT(*) FROM contexts WHERE account_id = $1", account
        )
        events = await conn.fetchval(
            "SELECT COUNT(*) FROM change_events WHERE account_id = $1", account
        )
        edges = await conn.fetchval(
            """
            SELECT COUNT(*) FROM dependencies
            WHERE dependent_id IN (SELECT id FROM contexts WHERE account_id = $1)
               OR dependency_id IN (SELECT id FROM contexts WHERE account_id = $1)
            """,
            account,
        )
    return int(contexts or 0) == 0 and int(events or 0) == 0 and int(edges or 0) == 0


async def _actual_persisted_sources(db) -> dict[str, list[str]]:
    rows = await db.fetch(
        """
        SELECT dependent_id::text AS dependent_id, dependency_id::text AS dependency_id
        FROM dependencies
        WHERE dep_type = 'derived_from'
        ORDER BY dependent_id::text, dependency_id::text
        """
    )
    result: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        result[str(row["dependent_id"])].append(str(row["dependency_id"]))
    return result


def _trace_with_actual_persistence(
    traces: Sequence[Mapping[str, Any]], actual: Mapping[str, Sequence[str]]
) -> list[dict[str, Any]]:
    result = []
    for trace in traces:
        row = dict(trace)
        row["persisted_source_ids"] = list(actual.get(str(row["node_id"]), ()))
        snapshot = set(map(str, row["candidate_snapshot_ids"]))
        hmax = set(map(str, row["hmax_candidate_ids"]))
        routed = set(map(str, row["routed_candidate_ids"]))
        selected = set(map(str, row["final_selected_source_ids"]))
        persisted = set(map(str, row["persisted_source_ids"]))
        if not routed <= hmax <= snapshot:
            raise FatalAuditError(
                f"trace envelope invariant failed for node {row['node_id']}"
            )
        if not selected <= routed or not persisted <= selected:
            raise FatalAuditError(
                f"trace selection/persistence invariant failed for node {row['node_id']}"
            )
        result.append(row)
    return result


async def _execute_case(system, case, plan, account: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    await wipe_account(system, account)
    if not await _account_empty(system, account):
        raise FatalAuditError(f"audit account not empty before case: {account}")
    token_before = _token_snap(system)
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
            plan=plan,
            schedule=registered_schedules()[SCHEDULE],
            audit_trace=True,
        )
        actual = await _actual_persisted_sources(db)
    if result.audit_trace is None:
        raise FatalAuditError("audit trace was not collected")
    nodes = result.audit_trace["nodes"]
    traces = _trace_with_actual_persistence(
        result.audit_trace["consolidations"], actual
    )
    if len(nodes) != len(traces):
        raise FatalAuditError("partial trace: node/consolidation count mismatch")
    mappings = map_gold_entities_to_nodes(case.entities, nodes)
    records: list[dict[str, Any]] = []
    for source, target in sorted({(edge.source, edge.target) for edge in case.edges}):
        source_entity = case.entities.get(source)
        target_entity = case.entities.get(target)
        if source_entity is None or target_entity is None:
            raise FatalAuditError(f"gold endpoint missing from entities: {source}->{target}")
        records.append(
            classify_gold_edge(
                case_key="",
                episode_id=case.episode_id,
                hop=case.hop,
                target_entity=case.target_entity,
                policy=plan.name,
                gold_source_entity=source,
                gold_target_entity=target,
                source_before_value=source_entity.before,
                target_before_value=target_entity.before,
                nodes=nodes,
                route_traces=traces,
                mappings=mappings,
            )
        )
    return records, _token_delta(token_before, _token_snap(system))


def _error_status(exc: BaseException) -> tuple[str, bool]:
    message = f"{type(exc).__name__}: {exc}"
    lowered = message.casefold()
    fatal_markers = (
        "401",
        "unauthorized",
        "403",
        "forbidden",
        "model access",
        "schema",
        "gold leakage",
        "hash mismatch",
        "run_config",
        "run config",
        "formal artifact contamination",
        "unsupported audit",
        "invariant failed",
        "partial trace",
        "selection/evaluation overlap",
    )
    if isinstance(exc, (FatalAuditError, ValueError)) or any(
        marker in lowered for marker in fatal_markers
    ):
        return "fatal_error", False
    retryable_types = (
        asyncio.TimeoutError,
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.ConnectError,
        ConnectionError,
        OSError,
    )
    retryable_markers = (
        "connection reset",
        "ssl",
        "408",
        "429",
        "500",
        "502",
        "503",
        "504",
        "postgres",
        "asyncpg",
        "temporar",
        "timeout",
    )
    if isinstance(exc, retryable_types) or any(
        marker in lowered for marker in retryable_markers
    ):
        return "retryable_error", True
    return "fatal_error", False


def _attempt_count(journal: Sequence[Mapping[str, Any]], key: str, config: str) -> int:
    return sum(
        item.get("event") == "attempt_started"
        and item.get("case_key") == key
        and item.get("run_config_hash") == config
        for item in journal
    )


async def run_one_attempt(
    args: argparse.Namespace,
    run_dir: Path,
    case,
    policy: str,
    config_hash: str,
) -> str:
    key = case_key(case.hop, case.episode_id, case.target_entity, policy)
    journal_path = run_dir / "attempts.jsonl"
    journal = read_jsonl_tolerant(journal_path)
    number = _attempt_count(journal, key, config_hash) + 1
    attempt_id = str(uuid.uuid4())
    append_jsonl(
        journal_path,
        attempt_started_event(
            case_key=key,
            attempt_id=attempt_id,
            attempt_number=number,
            run_config_hash=config_hash,
        ),
    )
    system = None
    token_before = None
    account = audit_account(
        case.hop, policy, case.episode_id, case.target_entity
    )
    tokens: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        system = await _build_system(args)
        token_before = _token_snap(system)
        records, tokens = await asyncio.wait_for(
            _execute_case(
                system, case, registered_build_plans()[policy], account
            ),
            timeout=float(args.case_timeout),
        )
        tokens = tokens or _token_delta(token_before, _token_snap(system))
        await wipe_account(system, account)
        cleanup_complete = await _account_empty(system, account)
        if not cleanup_complete:
            raise FatalAuditError(f"audit account cleanup incomplete: {account}")
        for record in records:
            record.update(
                {
                    "case_key": key,
                    "attempt_id": attempt_id,
                    "attempt_number": number,
                    "run_config_hash": config_hash,
                }
            )
            append_jsonl(run_dir / "gold_edge_audit.jsonl", record)
        append_jsonl(
            run_dir / "case_success.jsonl",
            {
                "event": "case_success",
                "case_key": key,
                "attempt_id": attempt_id,
                "attempt_number": number,
                "timestamp": time.time(),
                "run_config_hash": config_hash,
                "account": account,
                "account_cleanup_complete": True,
                "gold_edge_record_count": len(records),
                "tokens": tokens,
                "wall_seconds": time.perf_counter() - started,
            },
        )
        finished = attempt_finished_event(
            case_key=key,
            attempt_id=attempt_id,
            attempt_number=number,
            run_config_hash=config_hash,
            status="success",
        )
        finished["tokens"] = tokens
        finished["wall_seconds"] = time.perf_counter() - started
        append_jsonl(journal_path, finished)
        return "success"
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        status, retryable = _error_status(exc)
        cost_incomplete = isinstance(exc, asyncio.TimeoutError)
        cleanup_error: BaseException | None = None
        if system is not None:
            try:
                if token_before is not None:
                    tokens = tokens or _token_delta(
                        token_before, _token_snap(system)
                    )
                await wipe_account(system, account)
                if not await _account_empty(system, account):
                    raise FatalAuditError("post-error account cleanup incomplete")
            except BaseException as cleanup_exc:
                cleanup_error = cleanup_exc
                status, retryable = _error_status(cleanup_exc)
        message = f"{type(exc).__name__}: {exc}"
        if cleanup_error is not None:
            message += f"; cleanup={type(cleanup_error).__name__}: {cleanup_error}"
        finished = attempt_finished_event(
            case_key=key,
            attempt_id=attempt_id,
            attempt_number=number,
            run_config_hash=config_hash,
            status=status,
            error_type=type(exc).__name__,
            error_message=message,
            cost_incomplete=cost_incomplete,
        )
        finished["tokens"] = tokens
        finished["wall_seconds"] = time.perf_counter() - started
        append_jsonl(journal_path, finished)
        return "retryable_error" if retryable and status != "fatal_error" else status
    finally:
        if system is not None:
            try:
                await system.close()
            except Exception:
                # The terminal attempt event is already durable. A broken client
                # close must not turn it into an apparent stale attempt.
                pass


def _recover_stale(run_dir: Path, config_hash: str) -> int:
    journal_path = run_dir / "attempts.jsonl"
    journal = read_jsonl_tolerant(journal_path)
    recoveries = recovery_events_for_stale(
        journal, run_config_hash=config_hash
    )
    for event in recoveries:
        append_jsonl(journal_path, event)
        # Close the orphan logically so it is not reported stale forever.
        finished = attempt_finished_event(
            case_key=str(event["case_key"]),
            attempt_id=str(event["attempt_id"]),
            attempt_number=int(event["attempt_number"]),
            run_config_hash=config_hash,
            status="retryable_error",
            error_type="stale_attempt",
            error_message=str(event["error_message"]),
            cost_incomplete=True,
        )
        append_jsonl(journal_path, finished)
    return len(recoveries)


async def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    run_dir = _run_dir(out, args.smoke)
    manifest = load_manifest(out, smoke=args.smoke)
    assert_formal_guard(out, Path(args.formal))
    config = run_config(args, manifest)
    config_hash = bind_run_config(run_dir, config)
    _recover_stale(run_dir, config_hash)
    report = compute_completeness(run_dir, manifest, config_hash)
    update_state(run_dir, report)
    if report["fatal_case_keys"]:
        return 2
    cases = _case_index(Path(args.data), manifest)
    for row in manifest["selected_episodes"]:
        case = cases[(int(row["hop"]), str(row["episode_id"]))]
        for policy in POLICIES:
            key = case_key(case.hop, case.episode_id, case.target_entity, policy)
            report = compute_completeness(run_dir, manifest, config_hash)
            if key not in report["missing_case_keys"]:
                continue
            update_state(run_dir, report, running=key)
            while _attempt_count(
                read_jsonl_tolerant(run_dir / "attempts.jsonl"),
                key,
                config_hash,
            ) < int(args.max_attempts):
                status = await run_one_attempt(
                    args, run_dir, case, policy, config_hash
                )
                report = compute_completeness(run_dir, manifest, config_hash)
                update_state(run_dir, report)
                if status == "success":
                    break
                if status == "fatal_error":
                    return 2
                attempts = _attempt_count(
                    read_jsonl_tolerant(run_dir / "attempts.jsonl"),
                    key,
                    config_hash,
                )
                if attempts < int(args.max_attempts):
                    delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
                    print(f"retrying {key} in {delay}s", flush=True)
                    await asyncio.sleep(delay)
            report = compute_completeness(run_dir, manifest, config_hash)
            if key in report["missing_case_keys"]:
                # Five exhausted retryable attempts are a terminal blocker.
                append_jsonl(
                    run_dir / "attempts.jsonl",
                    {
                        "event": "attempt_finished",
                        "case_key": key,
                        "attempt_id": f"exhausted-{uuid.uuid4()}",
                        "attempt_number": int(args.max_attempts),
                        "timestamp": time.time(),
                        "run_config_hash": config_hash,
                        "status": "fatal_error",
                        "error_type": "attempts_exhausted",
                        "error_message": "five runner-level attempts exhausted",
                        "cost_incomplete": False,
                    },
                )
                report = compute_completeness(run_dir, manifest, config_hash)
                update_state(run_dir, report)
                return 2
    report = compute_completeness(run_dir, manifest, config_hash)
    update_state(run_dir, report)
    return 0 if report["complete"] else 1


def _load_bound(args: argparse.Namespace) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    out = Path(args.out)
    run_dir = _run_dir(out, args.smoke)
    manifest = load_manifest(out, smoke=args.smoke)
    config = run_config(args, manifest)
    bind_run_config(run_dir, config)
    return run_dir, manifest, config


def cmd_check(args: argparse.Namespace) -> int:
    assert_formal_guard(Path(args.out), Path(args.formal))
    run_dir, manifest, config = _load_bound(args)
    report = compute_completeness(
        run_dir, manifest, str(config["run_config_hash"])
    )
    update_state(run_dir, report)
    return 0 if report["complete"] and not report["fatal_case_keys"] else 1


def _paired_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for record in records:
        grouped[str(record["gold_edge_id"])][str(record["policy"])] = str(
            record["first_failure_stage"]
        )
    complete = [pair for pair in grouped.values() if set(pair) == set(POLICIES)]
    return {
        "paired_gold_edges": len(complete),
        "same_stage": sum(
            pair[POLICIES[0]] == pair[POLICIES[1]] for pair in complete
        ),
        "different_stage": sum(
            pair[POLICIES[0]] != pair[POLICIES[1]] for pair in complete
        ),
        "transitions": dict(
            Counter(
                f"{pair[POLICIES[0]]}->{pair[POLICIES[1]]}"
                for pair in complete
            )
        ),
    }


def cmd_summarize(args: argparse.Namespace) -> int:
    assert_formal_guard(Path(args.out), Path(args.formal))
    run_dir, manifest, config = _load_bound(args)
    config_hash = str(config["run_config_hash"])
    report = compute_completeness(run_dir, manifest, config_hash)
    if not report["complete"] or report["fatal_case_keys"]:
        raise FatalAuditError("cannot summarize an incomplete/fatal audit")
    journal, all_records, successes = _checkpoint(run_dir)
    records = records_from_last_complete_successes(
        journal,
        all_records,
        run_config_hash=config_hash,
        expected_gold_edges_by_case=_expected_counts(manifest),
        case_success_records=successes,
    )
    summary = {
        **report,
        **summarize_audit(records),
        "paired_policy_comparison": _paired_summary(records),
        "failed_attempts": report["retryable_attempts"]
        + report["fatal_attempts"],
        "selection_only_diagnostic": True,
        "not_p1_certification": True,
        "not_held_out_evaluation": True,
        "not_deployment_sla": True,
        "gold_scoring_side_only": True,
        "no_production_provenance_claim": True,
    }
    atomic_write_json(run_dir / "summary.json", summary)
    return 0


def cmd_verify_formal(args: argparse.Namespace) -> int:
    out = Path(args.out)
    before_path = _formal_before_path(out)
    before = _json(before_path)
    after = formal_path_hashes(Path(args.formal))
    after_path = out / "formal_artifact_hashes_after.json"
    atomic_write_json(after_path, after)
    verify_formal_artifact_hashes(before, after)
    if before_path.read_bytes() != after_path.read_bytes():
        raise FatalAuditError("formal before/after manifests are not byte-identical")
    print("formal_artifact_hashes_match=true", flush=True)
    return 0


def cmd_run_until_complete(args: argparse.Namespace) -> int:
    run_dir, manifest, config = _load_bound(args)
    config_hash = str(config["run_config_hash"])
    argv = list(sys.argv[1:])
    command_index = argv.index("run-until-complete")
    argv[command_index] = "run"
    for restart in range(MAX_WORKER_RESTARTS + 1):
        report = compute_completeness(run_dir, manifest, config_hash)
        update_state(run_dir, report)
        if report["complete"] and not report["fatal_case_keys"]:
            return 0
        if report["fatal_case_keys"]:
            return 2
        completed = subprocess.run(
            [sys.executable, "-m", "integrations.memebench.run_gold_edge_audit", *argv],
            cwd=_repo_root(),
            check=False,
        )
        report = compute_completeness(run_dir, manifest, config_hash)
        if report["complete"] and not report["fatal_case_keys"]:
            update_state(run_dir, report)
            return 0
        if report["fatal_case_keys"] or completed.returncode == 2:
            update_state(run_dir, report)
            return 2
        if restart >= MAX_WORKER_RESTARTS:
            break
        append_jsonl(
            run_dir / "attempts.jsonl",
            {
                "event": "worker_restarted",
                "timestamp": time.time(),
                "restart_number": restart + 1,
                "worker_returncode": completed.returncode,
                "run_config_hash": config_hash,
            },
        )
    report = compute_completeness(run_dir, manifest, config_hash)
    update_state(run_dir, report)
    return 1


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--formal", type=Path, default=DEFAULT_FORMAL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--smoke", action="store_true")


def _add_models(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chat-model", required=True)
    parser.add_argument("--extract-model", required=True)
    parser.add_argument("--p1-cheap-model", required=True)
    parser.add_argument("--p1-strong-model", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--embedding-provider", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--case-timeout", type=float, default=CASE_TIMEOUT_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-manifest")
    _add_paths(prepare)
    prepare.set_defaults(handler=prepare_manifest, async_handler=False)
    for name, handler, is_async in (
        ("run", cmd_run, True),
        ("check", cmd_check, False),
        ("summarize", cmd_summarize, False),
        ("run-until-complete", cmd_run_until_complete, False),
    ):
        command = sub.add_parser(name)
        _add_paths(command)
        _add_models(command)
        command.set_defaults(handler=handler, async_handler=is_async)
    verify = sub.add_parser("verify-formal")
    _add_paths(verify)
    verify.set_defaults(handler=cmd_verify_formal, async_handler=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "async_handler", False):
            return asyncio.run(args.handler(args))
        return args.handler(args)
    except FatalAuditError as exc:
        run_dir = _run_dir(Path(args.out), bool(getattr(args, "smoke", False)))
        atomic_write_text(
            run_dir / "AUDIT_STATE.md",
            f"# P1 Gold-Edge Audit State\n\n- fatal_blocker: {type(exc).__name__}: {exc}\n",
        )
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
