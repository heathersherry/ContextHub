"""Chronological P1+P2 MEME runner.

New CLI; the old ``run_eval.py`` interface is left unchanged. Every model
flag is required for subcommands that call a model. No silent defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from contexthub.propagation.registry import PropagationRuleRegistry
from integrations.memebench.answer import answer_question
from integrations.memebench.chronological_ingest import (
    chronological_edge_scores,
    ingest_case_chronological,
)
from integrations.memebench.chronological_policy import (
    ALPHA_GRAPH,
    ALPHA_PROP,
    EPSILON_GRAPH,
    EPSILON_PROP,
    EVALUATION_FRACTION,
    RECOMPUTE_COST_PRIMARY,
    SELECTION_FRACTION,
    SPLIT_SEED,
    BuildPlan,
    assert_frozen_plan_menu,
    build_plan_from_json,
    json_safe,
    plan_menu_manifest,
    policy_from_selection_cases,
    policy_manifest_hash,
    registered_build_plans,
    registered_schedules,
    select_frozen_p1_policy,
    split_episodes_for_chronological,
)
from integrations.memebench.ingest import (
    apply_root_change_raw,
    ingest_case_raw_cascade_e2e,
    ingest_filler,
)
from integrations.memebench.judge import judge_case_async, matches
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.common import clopper_pearson_upper
from integrations.memebench.planned_propagation import (
    CONTINUATION_MODE,
    PlannedDerivedMemoryRule,
    make_planned_registry,
    p2_case_fields,
    plan_published_graph,
    published_edge_pairs,
)
from integrations.memebench.propagation_planner_eval import (
    calibrate_contracts,
    load_joined,
)
from integrations.memebench.common import (
    DEFAULT_DATA,
    _token_delta,
    _token_snap,
)
from integrations.memebench.systems import EvalSystem, build_system

# Moved to integrations/memebench/common.py so this finished experiment could be archived.
from integrations.memebench.common import (
    bind_root_plan,
    sha256_text,
    wipe_account,
)

GROUPS = ("G0", "G1", "G2", "G3")
DEFAULT_OUT = Path("integrations/memebench/runs/chronological_v1")
MODEL_FLAGS = (
    "chat_model",
    "extract_model",
    "p1_cheap_model",
    "p1_strong_model",
    "p2_cheap_model",
    "p2_strong_model",
    "embedding_model",
)


def group_spec(group: str) -> dict[str, str]:
    return {
        "G0": {"p1": "old", "p2": "old"},
        "G1": {"p1": "new", "p2": "old"},
        "G2": {"p1": "old", "p2": "new"},
        "G3": {"p1": "new", "p2": "new"},
    }[group]


def hop_dir(out: Path | str, hop: int) -> Path:
    return Path(out) / f"hop{hop}"


def chronological_account(
    case,
    *,
    group: str,
    hop: int,
    policy: str,
    schedule: str,
) -> str:
    payload = (
        f"{hop}|{group}|{policy}|{schedule}|{case.episode_id}|{case.target_entity}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"ch{hop}{group}-{digest}"[:60]


def case_key(record: Mapping[str, Any]) -> str:
    return "|".join(
        (
            str(record.get("episode_id") or ""),
            str(record.get("hop") or ""),
            str(record.get("group") or ""),
            str(record.get("target_entity") or ""),
            str(record.get("p1_policy") or ""),
            str(record.get("consolidation_mode") or ""),
        )
    )


def merge_tokens(previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> dict[str, Any]:
    """Sum per-bucket deltas so a retried error still counts its first spend."""

    out: dict[str, Any] = {}
    for source in (previous or {}, current or {}):
        for bucket, payload in source.items():
            if not isinstance(payload, Mapping):
                continue
            slot = out.setdefault(
                bucket,
                {
                    "model": payload.get("model"),
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
            )
            if payload.get("model"):
                slot["model"] = payload.get("model")
            for field in ("calls", "prompt_tokens", "completion_tokens"):
                slot[field] = int(slot.get(field) or 0) + int(payload.get(field) or 0)
    return out


def scored_records(cases: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        row
        for row in cases
        if not row.get("errors") and row.get("p1", {}).get("scored") is True
    ]


def filter_rows_to_episode_ids(
    rows: Sequence[Mapping[str, Any]],
    episode_ids: Sequence[str] | set[str],
) -> list[Mapping[str, Any]]:
    allowed = {str(item) for item in episode_ids}
    return [row for row in rows if str(row.get("episode_id")) in allowed]



def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()




SOURCE_FINGERPRINT_FILES = (
    "integrations/memebench/run_chronological_p1p2.py",
    "integrations/memebench/chronological_policy.py",
    "integrations/memebench/chronological_ingest.py",
    "integrations/memebench/planned_propagation.py",
    "integrations/memebench/judge.py",
    "integrations/memebench/answer.py",
    "integrations/memebench/ingest.py",
    # Was p1_policy_certification.py; its clopper_pearson_upper (the only symbol
    # this runner used) now lives verbatim in common.py, and that file is archived.
    "integrations/memebench/common.py",
)


def source_fingerprint(repo: Path) -> str:
    digest = hashlib.sha256()
    for relative in SOURCE_FINGERPRINT_FILES:
        path = repo / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.exists():
            digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def git_identity(repo: Path) -> dict[str, Any]:
    """Describe the code actually being executed. Parent git is never the version."""

    def _run(args: list[str], cwd: Path) -> str | None:
        try:
            return subprocess.check_output(
                args, cwd=cwd, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    fingerprint = source_fingerprint(repo)
    toplevel = _run(["git", "rev-parse", "--show-toplevel"], repo)
    commit = _run(["git", "rev-parse", "HEAD"], repo)
    tracked = _run(
        [
            "git",
            "ls-files",
            "--error-unmatch",
            SOURCE_FINGERPRINT_FILES[0],
        ],
        repo,
    )
    owns_repo = bool(
        toplevel
        and Path(toplevel).resolve() == repo.resolve()
        and tracked is not None
    )
    identity: dict[str, Any] = {
        "repo": str(repo),
        "source_fingerprint": fingerprint,
    }
    if owns_repo:
        dirty = _run(["git", "status", "--porcelain"], repo)
        identity.update(
            {
                "git_commit": commit,
                "git_dirty": bool(dirty),
            }
        )
        return identity
    parent_commit = _run(["git", "rev-parse", "HEAD"], repo.parent)
    identity.update(
        {
            "git_commit": None,
            "parent_git_commit": parent_commit,
            "parent_git_tracks_this_tree": False,
            "note": (
                "ContextHub is not a tracked git root. parent_git_commit is "
                "not a restore key; use source_fingerprint."
            ),
        }
    )
    return identity


def prompt_hashes() -> dict[str, str]:
    from contexthub.propagation.derived_memory_rule import _ORACLE_PROMPT
    from contexthub.services.conversation_extraction_service import _EXTRACT_PROMPT
    from contexthub.services.dependency_discovery_service import _DISCOVERY_PROMPT
    from integrations.memebench.judge import _JUDGE_PROMPT

    return {
        "extract": sha256_text(_EXTRACT_PROMPT),
        "discovery": sha256_text(_DISCOVERY_PROMPT),
        "oracle": sha256_text(_ORACLE_PROMPT),
        "judge": sha256_text(_JUDGE_PROMPT),
    }


def package_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    try:
        from importlib import metadata

        for name in ("asyncpg", "httpx", "pulp"):
            try:
                versions[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                continue
    except Exception:
        pass
    return versions


def frozen_artifact_content_hash(path: Path) -> str:
    """SHA-256 of file bytes. Path and mtime are never part of the digest."""

    if not path.is_file():
        raise FileNotFoundError(
            f"missing frozen artifact {path}; cannot bind a content hash"
        )
    return sha256_file(path)


def run_config_payload(
    args: argparse.Namespace,
    *,
    split_hash: str | None,
    contract_method: str | None = None,
    frozen_p1_policy_hash: str | None = None,
    p2_contract_hash: str | None = None,
) -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[2]
    models = {name: getattr(args, name, None) for name in MODEL_FLAGS}
    models["judge_model"] = getattr(args, "judge_model", None)
    models["provider"] = getattr(args, "provider", None)
    models["embedding_provider"] = getattr(args, "embedding_provider", None)
    data = getattr(args, "data", None)
    data_path = Path(data) if data else None
    return {
        "data": str(data_path) if data_path else None,
        "data_hash": (
            sha256_file(data_path) if data_path is not None and data_path.exists() else None
        ),
        "hop": getattr(args, "hop", None),
        "split_hash": split_hash,
        "contract_method": contract_method,
        "frozen_p1_policy_hash": frozen_p1_policy_hash,
        "p2_contract_hash": p2_contract_hash,
        "models": models,
        "prompt_hashes": prompt_hashes(),
        "plan_menu_hash": policy_manifest_hash(),
        "source_fingerprint": source_fingerprint(repo),
    }


def run_config_hash(payload: Mapping[str, Any]) -> str:
    return sha256_text(json.dumps(json_safe(payload), sort_keys=True, ensure_ascii=False))


def bind_run_config(dest: Path, payload: Mapping[str, Any]) -> str:
    """Refuse to resume a checkpoint written under a different experiment config."""

    digest = run_config_hash(payload)
    meta_path = dest / "run_config.json"
    ckpt = dest / "checkpoint.jsonl"
    if meta_path.exists():
        stored = json.loads(meta_path.read_text(encoding="utf-8"))
        if stored.get("run_config_hash") != digest:
            raise ValueError(
                f"{meta_path} does not match the current data/model/prompt/split/"
                "contract/frozen-artifact hash; use a new --out directory instead "
                "of mixing runs"
            )
    elif ckpt.exists() and ckpt.stat().st_size:
        raise ValueError(
            f"{ckpt} exists without run_config.json; refusing to mix an unbound checkpoint"
        )
    write_json(meta_path, {**json_safe(payload), "run_config_hash": digest})
    return digest


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def empty_p1_block() -> dict[str, Any]:
    return {
        "n_gold": 0,
        "n_pred": 0,
        "n_tp": 0,
        "graph_miss": None,
        "scored": False,
        "tier_mix": {},
        "fast_path_seconds": 0.0,
        "consolidation_seconds": 0.0,
        "max_pending_nodes": 0,
        "cheap_none": 0,
    }


def empty_p2_block(*, contract_method: str) -> dict[str, Any]:
    return {
        "contract_method": contract_method,
        "epsilon_prop": EPSILON_PROP,
        "solver": None,
        "planned_mode_mix": {},
        "executed_mode_mix": {},
        "max_path_risk": 0.0,
        "direct_stale_count": 0,
        "receding_horizon_complete": False,
        "continuation_mode": CONTINUATION_MODE,
        "certified": False,
        "diagnostic_only": contract_method == "point",
        "held_out_certification": False,
        "contract_distribution_mismatch": True,
        "certification_blocked_reason": ["not_scored"],
        "realized_tokens": {},
        "executed_audit": [],
        "planning_errors": [],
    }


def build_case_record(
    *,
    episode_id: str,
    hop: int,
    group: str,
    p1_policy: str,
    consolidation_mode: str,
    target_entity: str = "",
    p1: Mapping[str, Any] | None = None,
    p2: Mapping[str, Any] | None = None,
    outcome: Mapping[str, Any] | None = None,
    tokens: Mapping[str, Any] | None = None,
    timings: Mapping[str, Any] | None = None,
    errors: Sequence[str] | None = None,
) -> dict[str, Any]:
    error_list = list(errors or [])
    p1_in = dict(p1 or {})
    explicit_scored = p1_in.get("scored") if p1 is not None else False
    p1_block = {**empty_p1_block(), **p1_in}
    if error_list:
        scored = False
    elif explicit_scored is not None:
        scored = bool(explicit_scored)
    else:
        scored = p1 is not None
    p1_block["scored"] = scored
    if scored:
        p1_block["graph_miss"] = int(p1_block["n_tp"]) < int(p1_block["n_gold"])
    else:
        p1_block["graph_miss"] = None
    record = {
        "episode_id": episode_id,
        "hop": hop,
        "group": group,
        "target_entity": target_entity,
        "p1_policy": p1_policy,
        "consolidation_mode": consolidation_mode,
        "p1": p1_block,
        "p2": {
            **empty_p2_block(
                contract_method=(p2 or {}).get("contract_method", "cp-upper")
            ),
            **(p2 or {}),
        },
        "outcome": {
            "off_trivial_pass": None,
            "on_trivial_pass": None,
            "false_fresh": None,
            "false_stale_proxy": None,
            **(outcome or {}),
        },
        "tokens": dict(tokens or {}),
        "timings": dict(timings or {}),
        "errors": error_list,
        "scoring_method": None,
        "run_config_hash": None,
    }
    if error_list:
        record["outcome"] = {
            "off_trivial_pass": None,
            "on_trivial_pass": None,
            "false_fresh": None,
            "false_stale_proxy": None,
        }
        record["p2"]["certified"] = False
        record["p2"]["held_out_certification"] = False
    return json_safe(record)


def _require_models(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    missing = [name for name in MODEL_FLAGS if not getattr(args, name, None)]
    if missing:
        flags = ", ".join("--" + name.replace("_", "-") for name in missing)
        parser.error(f"this subcommand requires explicit models: {flags}")


def _add_model_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chat-model", required=True)
    parser.add_argument("--extract-model", required=True)
    parser.add_argument("--p1-cheap-model", required=True)
    parser.add_argument("--p1-strong-model", required=True)
    parser.add_argument("--p2-cheap-model", required=True)
    parser.add_argument("--p2-strong-model", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--embedding-provider", required=True)
    parser.add_argument("--provider", required=True)


def _add_shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", default=DEFAULT_DATA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--hop", type=int, choices=[1, 2], required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--split-seed", default=SPLIT_SEED)


def load_cases(data: str, hop: int, limit: int | None = None):
    episodes = load_episodes(data)
    cases = extract_cascade_cases(episodes, hop=hop)
    if limit is not None:
        # Keep every Cascade case of an admitted episode together.
        kept_ids: list[str] = []
        selected = []
        for case in cases:
            if case.episode_id not in kept_ids:
                if len(kept_ids) >= limit:
                    break
                kept_ids.append(case.episode_id)
            if case.episode_id in kept_ids:
                selected.append(case)
        cases = selected
    return cases


def cmd_prepare_split(args: argparse.Namespace) -> int:
    cases = load_cases(args.data, args.hop, args.limit)
    episode_ids = [case.episode_id for case in cases]
    unique = list(dict.fromkeys(episode_ids))
    split = split_episodes_for_chronological(
        unique, seed=args.split_seed, selection_fraction=SELECTION_FRACTION
    )
    data_path = Path(args.data)
    manifest = {
        "data_path": str(data_path),
        "data_hash": sha256_file(data_path) if data_path.exists() else None,
        "split_seed": args.split_seed,
        "selection_fraction": SELECTION_FRACTION,
        "evaluation_fraction": EVALUATION_FRACTION,
        "hop": args.hop,
        "episode_ids": unique,
        "n_cases": len(cases),
        "plan_menu_hash": policy_manifest_hash(),
        "plan_menu": plan_menu_manifest(),
        "prompt_hashes": prompt_hashes(),
        "python_package_versions": package_versions(),
        "source_fingerprint": source_fingerprint(Path(__file__).resolve().parents[2]),
        **git_identity(Path(__file__).resolve().parents[2]),
        "epsilon_graph": EPSILON_GRAPH,
        "epsilon_prop": EPSILON_PROP,
        "alpha_graph": ALPHA_GRAPH,
        "alpha_prop": ALPHA_PROP,
        "feasibility_only": True,
    }
    out = hop_dir(args.out, args.hop)
    write_json(out / "split.json", {"split": split.__dict__, "hop": args.hop})
    write_json(out / "manifest.json", manifest)
    return 0


def cmd_select_p1(args: argparse.Namespace) -> int:
    out = hop_dir(args.out, args.hop)
    split_doc = json.loads((out / "split.json").read_text(encoding="utf-8"))
    split = split_episodes_for_chronological(
        split_doc["split"]["selection_ids"] + split_doc["split"]["certification_ids"],
        seed=split_doc["split"]["seed"],
        selection_fraction=split_doc["split"]["selection_fraction"],
    )
    plans = registered_build_plans()
    assert_frozen_plan_menu(plans)
    policies = []
    for name, plan in plans.items():
        cases_path = out / "p1_selection" / name / "cases.json"
        payload = json.loads(cases_path.read_text(encoding="utf-8"))
        policies.append(
            policy_from_selection_cases(plan, last_wins_case_list(payload["cases"]))
        )
    decision = select_frozen_p1_policy(policies, split)
    write_json(out / "frozen_p1_policy.json", decision)
    return 0


def load_frozen_plan(
    out: Path, *, split_hash: str | None = None
) -> tuple[BuildPlan, bool]:
    payload = json.loads((out / "frozen_p1_policy.json").read_text(encoding="utf-8"))
    if payload.get("frozen") and payload.get("selected_policy"):
        stored = payload.get("split_hash")
        if split_hash is not None and stored != split_hash:
            raise ValueError(
                f"frozen_p1_policy.json split_hash {stored!r} does not match "
                f"current split {split_hash!r}; rerun select-p1"
            )
        return build_plan_from_json(payload["selected_policy"]["parameters"]), True
    return registered_build_plans()["T_current_tau"], False


def load_p2_contract(
    *,
    hop: int,
    method: str,
    selection_ids: Sequence[str],
    evaluation_ids: Sequence[str],
    split_hash: str,
    recompute_cost: float = RECOMPUTE_COST_PRIMARY,
    alpha: float = ALPHA_PROP,
) -> dict[str, Any]:
    """Calibrate on the selection/calibration split only. Never use evaluation IDs."""

    root = Path(__file__).resolve().parent / "runs"
    edges = root / f"neg_edge_set_hop{hop}.json"
    verdicts = root / f"judge_routing_hop{hop}.json"
    rows = load_joined(edges, verdicts)
    selection = {str(item) for item in selection_ids}
    evaluation = {str(item) for item in evaluation_ids}
    cal_rows = filter_rows_to_episode_ids(rows, selection)
    leaked = filter_rows_to_episode_ids(rows, evaluation)
    if not cal_rows:
        raise ValueError(
            f"historical P2 artifacts for hop={hop} contain no selection-split episodes"
        )
    overlap = {str(row["episode_id"]) for row in cal_rows} & evaluation
    if overlap:
        raise ValueError(
            f"P2 calibration still contains evaluation episodes: {sorted(overlap)[:8]}"
        )
    contracts, source = calibrate_contracts(
        cal_rows, method=method, alpha=alpha, recompute_cost=recompute_cost
    )
    return {
        "contracts": contracts,
        "source": source,
        "contract_distribution_mismatch": True,
        "evaluation_episodes_in_calibration": False,
        "held_out_episode_split": True,
        "n_calibration_rows": len(cal_rows),
        "n_excluded_evaluation_rows": len(leaked),
        "calibration_episode_ids": sorted(
            {str(row["episode_id"]) for row in cal_rows}
        ),
        "held_out_certification": False,
        "split_hash": split_hash,
        "note": (
            "Phase-1 reuse of historical edge/judge files, filtered to the "
            "selection/calibration split. Not a frozen-P1-graph contract, so "
            "cp-upper is not held-out certification."
        ),
        "edges": str(edges),
        "verdicts": str(verdicts),
        "method": method,
    }


def load_frozen_p2_contract(
    out: Path,
    method: str,
    *,
    split_hash: str | None = None,
    selection_ids: Sequence[str] | None = None,
    evaluation_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    path = Path(out) / f"p2_contract_{method}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"missing frozen P2 contract {path}; run run-p2-calibration first. "
            "run-e2e will not recalibrate."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("evaluation_episodes_in_calibration"):
        raise ValueError(f"{path} was calibrated on evaluation episodes")
    stored_method = payload.get("method")
    if stored_method is not None and str(stored_method) != method:
        raise ValueError(f"{path} method {stored_method!r} does not match {method!r}")
    if split_hash is not None and payload.get("split_hash") != split_hash:
        raise ValueError(
            f"{path} split_hash {payload.get('split_hash')!r} does not match "
            f"current split {split_hash!r}; rerun run-p2-calibration"
        )
    calibration_ids = {str(item) for item in payload.get("calibration_episode_ids") or []}
    if evaluation_ids is not None:
        leaked = calibration_ids & {str(item) for item in evaluation_ids}
        if leaked:
            raise ValueError(
                f"{path} calibration IDs include current evaluation episodes: "
                f"{sorted(leaked)[:8]}"
            )
    if selection_ids is not None:
        extra = calibration_ids - {str(item) for item in selection_ids}
        if extra:
            raise ValueError(
                f"{path} calibration IDs are not in the current selection split: "
                f"{sorted(extra)[:8]}"
            )
    return payload


def cmd_run_p2_calibration(args: argparse.Namespace) -> int:
    out = hop_dir(args.out, args.hop)
    split_doc = json.loads((out / "split.json").read_text(encoding="utf-8"))
    split = split_doc["split"]
    for method in ("point", "cp-upper"):
        doc = load_p2_contract(
            hop=args.hop,
            method=method,
            selection_ids=split["selection_ids"],
            evaluation_ids=split["certification_ids"],
            split_hash=split["split_hash"],
        )
        write_json(out / f"p2_contract_{method}.json", doc)
    return 0


def p1_fields_from_ingest(result, case) -> dict[str, Any]:
    scores = chronological_edge_scores(case, result)
    cheap_none = int(result.tier_mix.get("edge", {}).get("cheap_none", 0))
    return {
        "n_gold": int(scores["n_gold"]),
        "n_pred": int(scores["n_pred"]),
        "n_tp": int(scores["n_tp"]),
        "graph_miss": int(scores["n_tp"]) < int(scores["n_gold"]),
        "scored": True,
        "tier_mix": result.tier_mix,
        "fast_path_seconds": result.timings["fast_path_seconds"],
        "consolidation_seconds": result.timings["consolidation_seconds"],
        "max_pending_nodes": result.timings["max_pending_nodes"],
        "cheap_none": cheap_none,
    }


def case_identity(
    case,
    *,
    hop: int,
    group: str,
    p1_policy: str,
    consolidation_mode: str,
) -> str:
    return case_key(
        {
            "episode_id": case.episode_id,
            "hop": hop,
            "group": group,
            "target_entity": getattr(case, "target_entity", ""),
            "p1_policy": p1_policy,
            "consolidation_mode": consolidation_mode,
        }
    )


def default_p2_registry(system: EvalSystem) -> PropagationRuleRegistry:
    return PropagationRuleRegistry.default(
        chat_client=system.oracle_chat,
        repo=system.repo,
        cheap_chat=system.p2_cheap_chat,
        gate_event_sink=system.gate_events,
    )




def restore_default_p2(system: EvalSystem) -> None:
    system.gate_events.clear()
    system.rule_registry = default_p2_registry(system)


@asynccontextmanager
async def isolated_account(system: EvalSystem, account: str):
    restore_default_p2(system)
    await wipe_account(system, account)
    try:
        yield
    finally:
        restore_default_p2(system)
        await wipe_account(system, account)


async def _load_published_pairs(db) -> list[tuple[UUID, UUID]]:
    rows = await db.fetch(
        """
        SELECT d.dependency_id, d.dependent_id
        FROM dependencies d
        JOIN contexts src ON src.id = d.dependency_id
        JOIN contexts dst ON dst.id = d.dependent_id
        WHERE d.dep_type = 'derived_from'
        """
    )
    return published_edge_pairs(
        [
            {
                "dependency_id": row["dependency_id"],
                "dependent_id": row["dependent_id"],
            }
            for row in rows
        ]
    )


async def pending_event_context_ids(system: EvalSystem, account: str) -> list[str]:
    async with system.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT context_id::text AS context_id
            FROM change_events
            WHERE account_id = $1
              AND delivery_status IN ('pending', 'retry')
              AND next_retry_at <= NOW()
            GROUP BY context_id
            ORDER BY MIN(timestamp) ASC
            """,
            account,
        )
    return [str(row["context_id"]) for row in rows]




async def unfinished_propagation_events(
    system: EvalSystem, account: str
) -> list[dict[str, Any]]:
    async with system.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_id::text AS event_id,
                   context_id::text AS context_id,
                   delivery_status,
                   last_error,
                   attempt_count
            FROM change_events
            WHERE account_id = $1
              AND delivery_status NOT IN ('processed', 'succeeded')
            ORDER BY timestamp ASC
            """,
            account,
        )
    return [dict(row) for row in rows]


async def drain_case_events(system: EvalSystem, account: str) -> list[dict[str, Any]]:
    """Drain this account until no ready events remain, then audit leftovers.

    Newly created ``marked_stale`` hops are picked up on later rounds, so a
    chain ``A→B→C`` cannot be truncated by the first-pass context order.
    The benchmark only drives the production durable consumer and reads its
    structured leftover state; it does not implement queue semantics itself.
    """

    engine = system.build_engine(cascade_on_stale=True)
    engine._running = True
    for _ in range(32):
        context_ids = await pending_event_context_ids(system, account)
        if not context_ids:
            break
        for context_id in context_ids:
            await engine.drain_once(context_id=context_id)
    return await unfinished_propagation_events(system, account)


async def run_one_group_case(
    system: EvalSystem,
    case,
    *,
    group: str,
    hop: int,
    plan: BuildPlan,
    schedule_name: str,
    contract_method: str,
    contract: Mapping[str, Mapping[str, Any]],
    new_p1_frozen: bool,
    contract_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = group_spec(group)
    schedule = registered_schedules()[schedule_name]
    policy_name = "T_current_tau"
    if spec["p1"] == "new" and new_p1_frozen:
        policy_name = plan.name
    consolidation_mode = (
        schedule_name if spec["p1"] == "new" else "sync-inline"
    )
    account = chronological_account(
        case,
        group=group,
        hop=hop,
        policy=policy_name,
        schedule=consolidation_mode,
    )
    tok_before = _token_snap(system)
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    errors: list[str] = []
    warnings: list[str] = []
    p1_block = empty_p1_block()
    p2_block = empty_p2_block(contract_method=contract_method)
    outcome = {
        "off_trivial_pass": None,
        "on_trivial_pass": None,
        "false_fresh": None,
        "false_stale_proxy": None,
    }
    if spec["p1"] == "new" and not new_p1_frozen:
        warnings.append("new-P1 smoke fallback; not a new-P1 claim")
    meta = dict(contract_meta or {})
    mismatch = bool(meta.get("contract_distribution_mismatch", True))
    eval_in_cal = bool(meta.get("evaluation_episodes_in_calibration", False))

    def _lap(stage: str, started: float) -> float:
        timings[stage] = round(time.perf_counter() - started, 3)
        return time.perf_counter()

    try:
        async with isolated_account(system, account):
            if case.after_question is None:
                raise ValueError("missing after_question; cannot score E2E answers")
            t = t0
            async with system.repo.session(account) as db:
                if spec["p1"] == "old":
                    baseline = registered_build_plans()["T_current_tau"]
                    graph, tiers = await ingest_case_raw_cascade_e2e(
                        db,
                        case,
                        account,
                        system.embedding.embed_batch,
                        extractor=system.extractor,
                        disamb_cheap=system.cascade_cheap_svc,
                        disamb_strong=system.cascade_strong_svc,
                        edge_cheap=system.cascade_cheap_svc,
                        edge_strong=system.cascade_strong_svc,
                        tau_disamb=baseline.tau_disamb,
                        tau_cand=baseline.tau_cand,
                        tau_edge=baseline.tau_edge or 0.4,
                        k=baseline.k,
                    )
                    from integrations.memebench.ingest import edge_pr_raw

                    scores = edge_pr_raw(case, graph)
                    p1_block = {
                        "n_gold": int(scores["n_gold"]),
                        "n_pred": int(scores["n_pred"]),
                        "n_tp": int(scores["n_tp"]),
                        "graph_miss": int(scores["n_tp"]) < int(scores["n_gold"]),
                        "scored": True,
                        "tier_mix": {key: dict(value) for key, value in tiers.items()},
                        "fast_path_seconds": 0.0,
                        "consolidation_seconds": 0.0,
                        "max_pending_nodes": 0,
                        "cheap_none": int(
                            dict(tiers.get("edge") or {}).get("cheap_none", 0)
                        ),
                    }
                    policy_name = "T_current_tau"
                else:
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
                        schedule=schedule,
                    )
                    graph = result.graph
                    p1_block = p1_fields_from_ingest(result, case)
                await ingest_filler(db, case, account, system.embedding.embed_batch)
            t = _lap("ingest", t)

            before_answer = ""
            before_res = None
            before_gold = (
                case.before_question.expected_answer if case.before_question else None
            )
            if case.before_question:
                async with system.repo.session(account) as db:
                    before_res = await answer_question(
                        system, db, account, case.before_question.question
                    )
                    before_answer = before_res.answer
            t = _lap("before_q", t)

            async with system.repo.session(account) as db:
                await apply_root_change_raw(
                    db,
                    case,
                    account,
                    graph,
                    system.embedding.embed_batch,
                    system.extractor,
                    system.discovery_chat,
                )
                pairs = await _load_published_pairs(db)
            t = _lap("root_change", t)

            async with system.repo.session(account) as db:
                off_res = await answer_question(
                    system, db, account, case.after_question.question
                )
            t = _lap("off_answer", t)

            rule = None
            if spec["p2"] == "new":
                if not pairs:
                    warnings.append("no published edges to plan")
                    p2_block = empty_p2_block(contract_method=contract_method)
                    p2_block["contract_distribution_mismatch"] = mismatch
                    p2_block["evaluation_episodes_in_calibration"] = eval_in_cal
                    p2_block["certification_blocked_reason"] = [
                        "no_published_edges",
                        *(
                            ["contract_distribution_mismatch"]
                            if mismatch
                            else []
                        ),
                    ]
                else:
                    plan_doc = plan_published_graph(
                        pairs,
                        contract=contract,
                        epsilon=EPSILON_PROP,
                        contract_method=contract_method,
                        contract_distribution_mismatch=mismatch,
                        evaluation_episodes_in_calibration=eval_in_cal,
                    )
                    rule = PlannedDerivedMemoryRule(
                        modes=plan_doc["assignments"],
                        repo=system.repo,
                        strong_chat=system.oracle_chat,
                        cheap_chat=system.p2_cheap_chat,
                        event_sink=system.gate_events,
                    )
                    system.rule_registry = make_planned_registry(rule)
                    await bind_root_plan(system, account, plan_doc, contract)
                    leftover = await drain_case_events(system, account)
                    if leftover:
                        p2_block = p2_case_fields(plan_doc, rule=rule)
                        p2_block["unfinished_events"] = leftover
                        p2_block["certified"] = False
                        p2_block["held_out_certification"] = False
                        raise RuntimeError(
                            f"propagation unfinished: {len(leftover)} events "
                            "still pending/retry/processing"
                        )
                    p2_block = p2_case_fields(plan_doc, rule=rule)
            else:
                restore_default_p2(system)
                leftover = await drain_case_events(system, account)
                p2_block = empty_p2_block(contract_method=contract_method)
                p2_block["solver"] = "old-oracle"
                p2_block["executed_mode_mix"] = {
                    "cascade": len(system.gate_events)
                }
                p2_block["certified"] = False
                p2_block["held_out_certification"] = False
                p2_block["contract_distribution_mismatch"] = mismatch
                p2_block["evaluation_episodes_in_calibration"] = eval_in_cal
                blocked = ["old_p2_oracle"]
                if mismatch:
                    blocked.append("contract_distribution_mismatch")
                if eval_in_cal:
                    blocked.append("evaluation_episodes_in_calibration")
                p2_block["certification_blocked_reason"] = blocked
                if leftover:
                    p2_block["unfinished_events"] = leftover
                    raise RuntimeError(
                        f"propagation unfinished: {len(leftover)} events "
                        "still pending/retry/processing"
                    )
            t = _lap("drain", t)

            async with system.repo.session(account) as db:
                on_res = await answer_question(
                    system, db, account, case.after_question.question
                )
            t = _lap("on_answer", t)

            bq = case.before_question.question if case.before_question else ""
            aq = case.after_question.question
            off_v = await judge_case_async(
                before_answer,
                before_gold,
                off_res.answer,
                case.after_question.expected_answer,
                before_question=bq,
                after_question=aq,
                chat=system.judge_chat,
            )
            on_v = await judge_case_async(
                before_answer,
                before_gold,
                on_res.answer,
                case.after_question.expected_answer,
                before_question=bq,
                after_question=aq,
                chat=system.judge_chat,
            )
            leak = []
            if before_gold:
                for uri, txt in zip(on_res.retrieved_uris, on_res.retrieved_l2):
                    if matches(txt, before_gold):
                        leak.append({"uri": uri, "text": txt[:300]})
            outcome = {
                "off_trivial_pass": off_v.trivial_pass,
                "on_trivial_pass": on_v.trivial_pass,
                "false_fresh": bool(on_v.before_ok and not on_v.after_ok),
                "false_stale_proxy": None,
                "before_ok": on_v.before_ok,
                "off_after_ok": off_v.after_ok,
                "on_after_ok": on_v.after_ok,
                "on_leak_notes": leak,
            }
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    record = build_case_record(
        episode_id=case.episode_id,
        hop=hop,
        group=group,
        p1_policy=policy_name,
        consolidation_mode=consolidation_mode,
        target_entity=getattr(case, "target_entity", ""),
        p1=p1_block,
        p2=p2_block,
        outcome=outcome,
        tokens=_token_delta(tok_before, _token_snap(system)),
        timings={**timings, "total": round(time.perf_counter() - t0, 3)},
        errors=errors,
    )
    if warnings:
        record["claim_warnings"] = warnings
    record["scoring_method"] = (
        "llm-judge" if getattr(system, "judge_chat", None) is not None else "containment"
    )
    return record


def _split_doc(args: argparse.Namespace) -> dict[str, Any]:
    path = hop_dir(args.out, args.hop) / "split.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def _iter_selection_cases(args: argparse.Namespace):
    cases = load_cases(args.data, args.hop, args.limit)
    selection = set(_split_doc(args)["split"]["selection_ids"])
    return [case for case in cases if case.episode_id in selection]


async def _iter_eval_cases(args: argparse.Namespace):
    cases = load_cases(args.data, args.hop, args.limit)
    evaluation = set(_split_doc(args)["split"]["certification_ids"])
    return [case for case in cases if case.episode_id in evaluation]


async def _build_eval_system(args: argparse.Namespace) -> EvalSystem:
    return await build_system(
        chat_model=args.chat_model,
        oracle_model=args.p2_strong_model,
        extract_model=args.extract_model,
        judge_model=getattr(args, "judge_model", None),
        provider_label=args.provider,
        embedding_provider_label=args.embedding_provider,
        embedding_model=args.embedding_model,
        cascade=True,
        cascade_cheap_model=args.p1_cheap_model,
        cascade_strong_model=args.p1_strong_model,
        p2_cascade=True,
        p2_cheap_model=args.p2_cheap_model,
    )


def _checkpoint_write(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(json_safe(record), ensure_ascii=False) + "\n")


def _checkpoint_index(path: Path) -> dict[str, dict[str, Any]]:
    last: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return last
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        last[case_key(row)] = row
    return last


def _checkpoint_done_keys(
    path: Path, *, config_hash: str | None = None
) -> set[str]:
    done: set[str] = set()
    for key, row in _checkpoint_index(path).items():
        if config_hash is not None and row.get("run_config_hash") != config_hash:
            continue
        if not row.get("errors") and row.get("p1", {}).get("scored") is True:
            done.add(key)
    return done


def last_wins_case_list(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    last: dict[str, dict[str, Any]] = {}
    for row in rows:
        last[case_key(row)] = dict(row)
    return list(last.values())


def last_wins_records(
    path: Path, *, config_hash: str | None = None
) -> list[dict[str, Any]]:
    rows = list(_checkpoint_index(path).values())
    if config_hash is None:
        return rows
    return [row for row in rows if row.get("run_config_hash") == config_hash]


def maybe_merge_retry_tokens(
    previous: Mapping[str, Any] | None,
    record: dict[str, Any],
) -> dict[str, Any]:
    if previous and previous.get("errors"):
        record["tokens"] = merge_tokens(previous.get("tokens"), record.get("tokens"))
    return record


def edge_miss_from_scored_cases(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Diagnostic: missed gold edges / gold edges on scored cases only.

    This does not replace the episode any-miss Gate. Failed/unscored cases are
    excluded, matching ``成功评完的边``.
    """

    gold = 0
    recalled = 0
    for row in scored_records(rows):
        p1 = row.get("p1") or {}
        n_gold = int(p1.get("n_gold") or 0)
        n_tp = int(p1.get("n_tp") or 0)
        gold += n_gold
        recalled += min(n_tp, n_gold)
    missed = gold - recalled
    return {
        "n_gold_edges": gold,
        "n_recalled_edges": recalled,
        "n_missed_edges": missed,
        "edge_miss_rate": (missed / gold) if gold else None,
        "edge_recall": (recalled / gold) if gold else None,
    }


def graph_miss_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_episode_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Clopper–Pearson on unique episodes. Any case miss ⇒ episode miss.

    Episodes with an unscored/error case are excluded from U_graph, not counted
    as hits. ``p1_go`` is False unless every expected episode scored and none failed.
    ``edge_miss_rate`` is a descriptive diagnostic and never selects the policy.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("episode_id") or ""), []).append(row)
    scored_episodes: list[bool] = []
    n_failed_episodes = 0
    for group in grouped.values():
        if any(
            not (not row.get("errors") and row.get("p1", {}).get("scored") is True)
            for row in group
        ):
            n_failed_episodes += 1
            continue
        scored_episodes.append(any(bool(row["p1"]["graph_miss"]) for row in group))
    n = len(scored_episodes)
    misses = sum(scored_episodes)
    upper = clopper_pearson_upper(misses, n, ALPHA_GRAPH) if n else 1.0
    expected = {str(item) for item in expected_episode_ids or []}
    n_missing = len(expected - set(grouped)) if expected else 0
    complete = n_failed_episodes == 0 and n_missing == 0 and n > 0
    if expected:
        complete = complete and expected <= set(grouped)
    return {
        "n": n,
        "n_episodes": n,
        "n_cases": len(scored_records(rows)),
        "n_failed": n_failed_episodes,
        "n_failed_cases": len(rows) - len(scored_records(rows)),
        "n_missing_episodes": n_missing,
        "event": "episode",
        "graph_miss_rate": misses / n if n else None,
        "U_graph": upper,
        "p1_go": bool(complete and upper <= EPSILON_GRAPH),
        "evaluation_complete": complete,
        **edge_miss_from_scored_cases(rows),
    }


def planned_all_direct_stale(rows: Sequence[Mapping[str, Any]]) -> bool | None:
    planned = [
        row
        for row in rows
        if row.get("p2", {}).get("planned_mode_mix")
    ]
    if not planned:
        return None
    return all(
        (row["p2"].get("planned_mode_mix") or {}).get("direct-stale", 0)
        == row["p2"].get("direct_stale_count", 0)
        and row["p2"].get("direct_stale_count", 0)
        == sum((row["p2"].get("planned_mode_mix") or {}).values())
        for row in planned
    )


async def _run_isolated_ingest(
    system: EvalSystem,
    case,
    *,
    account: str,
    plan: BuildPlan,
    schedule_name: str,
) -> dict[str, Any]:
    tok_before = _token_snap(system)
    errors: list[str] = []
    p1_block = empty_p1_block()
    timings: dict[str, Any] = {}
    try:
        async with isolated_account(system, account):
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
                    schedule=registered_schedules()[schedule_name],
                )
            p1_block = p1_fields_from_ingest(result, case)
            timings = result.timings
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "p1": p1_block,
        "timings": timings,
        "tokens": _token_delta(tok_before, _token_snap(system)),
        "errors": errors,
    }


async def cmd_run_p1_selection(args: argparse.Namespace) -> int:
    plans = registered_build_plans()
    names = [args.policy] if getattr(args, "policy", None) else list(plans)
    assert_frozen_plan_menu(plans)
    cases = await _iter_selection_cases(args)
    system = await _build_eval_system(args)
    out = hop_dir(args.out, args.hop)
    split_hash = _split_doc(args)["split"]["split_hash"]
    config = run_config_payload(args, split_hash=split_hash)
    try:
        for name in names:
            plan = plans[name]
            dest = out / "p1_selection" / name
            config_hash = bind_run_config(dest, config)
            ckpt = dest / "checkpoint.jsonl"
            done = _checkpoint_done_keys(ckpt, config_hash=config_hash)
            index = _checkpoint_index(ckpt)
            for case in cases:
                key = case_identity(
                    case,
                    hop=args.hop,
                    group="p1-selection",
                    p1_policy=plan.name,
                    consolidation_mode="async-each-session",
                )
                if key in done:
                    continue
                account = chronological_account(
                    case,
                    group="p1-selection",
                    hop=args.hop,
                    policy=plan.name,
                    schedule="async-each-session",
                )
                payload = await _run_isolated_ingest(
                    system,
                    case,
                    account=account,
                    plan=plan,
                    schedule_name="async-each-session",
                )
                record = build_case_record(
                    episode_id=case.episode_id,
                    hop=args.hop,
                    group="p1-selection",
                    p1_policy=plan.name,
                    consolidation_mode="async-each-session",
                    target_entity=case.target_entity,
                    p1=payload["p1"],
                    tokens=payload["tokens"],
                    timings=payload["timings"],
                    errors=payload["errors"],
                )
                record["scoring_method"] = "graph-only"
                record["run_config_hash"] = config_hash
                record = maybe_merge_retry_tokens(index.get(key), record)
                _checkpoint_write(ckpt, record)
                index[key] = record
            rows = last_wins_records(ckpt, config_hash=config_hash)
            write_json(dest / "cases.json", {"policy": name, "cases": rows})
            write_json(
                dest / "summary.json",
                {
                    "policy": name,
                    **graph_miss_summary(
                        rows,
                        expected_episode_ids=_split_doc(args)["split"]["selection_ids"],
                    ),
                },
            )
    finally:
        await system.close()
    return 0


async def cmd_run_p1_timing(args: argparse.Namespace) -> int:
    out = hop_dir(args.out, args.hop)
    split_hash = _split_doc(args)["split"]["split_hash"]
    plan, frozen = load_frozen_plan(out, split_hash=split_hash)
    if not frozen:
        print("frozen policy missing; timing will not claim new P1", flush=True)
    cases = await _iter_eval_cases(args)
    system = await _build_eval_system(args)
    policy_name = plan.name if frozen else "T_current_tau"
    config = run_config_payload(
        args,
        split_hash=split_hash,
        frozen_p1_policy_hash=frozen_artifact_content_hash(out / "frozen_p1_policy.json"),
    )
    try:
        for schedule_name in registered_schedules():
            dest = out / "p1_timing" / schedule_name
            config_hash = bind_run_config(dest, config)
            ckpt = dest / "checkpoint.jsonl"
            done = _checkpoint_done_keys(ckpt, config_hash=config_hash)
            index = _checkpoint_index(ckpt)
            for case in cases:
                key = case_identity(
                    case,
                    hop=args.hop,
                    group="p1-timing",
                    p1_policy=policy_name,
                    consolidation_mode=schedule_name,
                )
                if key in done:
                    continue
                account = chronological_account(
                    case,
                    group="p1-timing",
                    hop=args.hop,
                    policy=policy_name,
                    schedule=schedule_name,
                )
                payload = await _run_isolated_ingest(
                    system,
                    case,
                    account=account,
                    plan=plan,
                    schedule_name=schedule_name,
                )
                record = build_case_record(
                    episode_id=case.episode_id,
                    hop=args.hop,
                    group="p1-timing",
                    p1_policy=policy_name,
                    consolidation_mode=schedule_name,
                    target_entity=case.target_entity,
                    p1=payload["p1"],
                    tokens=payload["tokens"],
                    timings=payload["timings"],
                    errors=payload["errors"],
                )
                record["scoring_method"] = "graph-only"
                record["run_config_hash"] = config_hash
                record = maybe_merge_retry_tokens(index.get(key), record)
                _checkpoint_write(ckpt, record)
                index[key] = record
            rows = last_wins_records(ckpt, config_hash=config_hash)
            write_json(dest / "cases.json", {"schedule": schedule_name, "cases": rows})
    finally:
        await system.close()
    return 0


async def cmd_run_e2e(args: argparse.Namespace) -> int:
    out = hop_dir(args.out, args.hop)
    split_doc = _split_doc(args)
    plan, frozen = load_frozen_plan(out, split_hash=split_doc["split"]["split_hash"])
    contract_doc = load_frozen_p2_contract(
        out,
        args.contract,
        split_hash=split_doc["split"]["split_hash"],
        selection_ids=split_doc["split"]["selection_ids"],
        evaluation_ids=split_doc["split"]["certification_ids"],
    )
    contract = contract_doc["contracts"]["__global__"]
    cases = await _iter_eval_cases(args)
    system = await _build_eval_system(args)
    if system.judge_chat is None:
        raise ValueError("run-e2e requires --judge-model for MEME §4.1 scoring")
    config = run_config_payload(
        args,
        split_hash=split_doc["split"]["split_hash"],
        contract_method=args.contract,
        frozen_p1_policy_hash=frozen_artifact_content_hash(out / "frozen_p1_policy.json"),
        p2_contract_hash=frozen_artifact_content_hash(
            out / f"p2_contract_{args.contract}.json"
        ),
    )
    eval_ids = list(split_doc["split"]["certification_ids"])
    groups = [args.group] if getattr(args, "group", None) else list(GROUPS)
    try:
        for group in groups:
            dest = out / "e2e" / args.contract / group
            config_hash = bind_run_config(dest, config)
            ckpt = dest / "checkpoint.jsonl"
            done = _checkpoint_done_keys(ckpt, config_hash=config_hash)
            index = _checkpoint_index(ckpt)
            for case in cases:
                key = case_identity(
                    case,
                    hop=args.hop,
                    group=group,
                    p1_policy=(
                        plan.name
                        if group_spec(group)["p1"] == "new" and frozen
                        else "T_current_tau"
                    ),
                    consolidation_mode=(
                        "async-each-session"
                        if group_spec(group)["p1"] == "new"
                        else "sync-inline"
                    ),
                )
                if key in done:
                    continue
                record = await run_one_group_case(
                    system,
                    case,
                    group=group,
                    hop=args.hop,
                    plan=plan,
                    schedule_name="async-each-session",
                    contract_method=args.contract,
                    contract=contract,
                    new_p1_frozen=frozen,
                    contract_meta=contract_doc,
                )
                record["run_config_hash"] = config_hash
                record = maybe_merge_retry_tokens(index.get(key), record)
                _checkpoint_write(ckpt, record)
                index[key] = record
            rows = last_wins_records(ckpt, config_hash=config_hash)
            write_json(dest / "cases.json", {"group": group, "cases": rows})
            write_json(
                dest / "summary.json",
                graph_miss_summary(rows, expected_episode_ids=eval_ids),
            )
    finally:
        await system.close()
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    out = hop_dir(args.out, args.hop)
    summary: dict[str, Any] = {
        "feasibility_only": True,
        "epsilon_graph": EPSILON_GRAPH,
        "epsilon_prop": EPSILON_PROP,
        "not_a_deployment_sla": True,
        "held_out_certification": False,
        "contract_distribution_mismatch": True,
        "groups": {},
    }
    frozen_path = out / "frozen_p1_policy.json"
    if frozen_path.exists():
        summary["frozen_p1"] = json.loads(frozen_path.read_text(encoding="utf-8"))
    eval_ids = []
    split_path = out / "split.json"
    if split_path.exists():
        split = json.loads(split_path.read_text(encoding="utf-8"))
        eval_ids = list(split["split"]["certification_ids"])
        summary["split_hash"] = split["split"]["split_hash"]
    for method in ("point", "cp-upper"):
        for group in GROUPS:
            path = out / "e2e" / method / group / "cases.json"
            if not path.exists():
                continue
            cases = last_wins_case_list(
                json.loads(path.read_text(encoding="utf-8"))["cases"]
            )
            scored = scored_records(cases)
            stats = graph_miss_summary(cases, expected_episode_ids=eval_ids)
            scoring = {row.get("scoring_method") for row in scored if row.get("scoring_method")}
            false_fresh = sum(bool(row["outcome"].get("false_fresh")) for row in scored)
            on_pass = sum(bool(row["outcome"].get("on_trivial_pass")) for row in scored)
            stats.update(
                {
                    "scoring_method": (
                        scoring.pop() if len(scoring) == 1 else sorted(scoring)
                    ),
                    "false_fresh_rate": false_fresh / len(scored) if scored else None,
                    "on_trivial_pass_rate": on_pass / len(scored) if scored else None,
                    "any_certified": any(
                        bool(row["p2"].get("certified")) for row in scored
                    ),
                    "direct_stale_all": planned_all_direct_stale(scored),
                    "receding_horizon_complete": False,
                }
            )
            summary["groups"].setdefault(group, {})[method] = stats
    summary["evaluation_episode_ids"] = eval_ids
    write_json(out / "summary.json", summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare-split")
    _add_shared(prepare)
    prepare.set_defaults(handler=cmd_prepare_split, needs_models=False)

    select = sub.add_parser("select-p1")
    select.add_argument("--out", type=Path, default=DEFAULT_OUT)
    select.add_argument("--hop", type=int, choices=[1, 2], required=True)
    select.set_defaults(handler=cmd_select_p1, needs_models=False)

    p2cal = sub.add_parser("run-p2-calibration")
    _add_shared(p2cal)
    p2cal.set_defaults(handler=cmd_run_p2_calibration, needs_models=False)

    summarize = sub.add_parser("summarize")
    summarize.add_argument("--out", type=Path, default=DEFAULT_OUT)
    summarize.add_argument("--hop", type=int, choices=[1, 2], required=True)
    summarize.set_defaults(handler=cmd_summarize, needs_models=False)

    p1sel = sub.add_parser("run-p1-selection")
    _add_shared(p1sel)
    _add_model_flags(p1sel)
    p1sel.add_argument("--policy", choices=list(registered_build_plans()), default=None)
    p1sel.set_defaults(handler=cmd_run_p1_selection, needs_models=True, async_handler=True)

    p1time = sub.add_parser("run-p1-timing")
    _add_shared(p1time)
    _add_model_flags(p1time)
    p1time.set_defaults(handler=cmd_run_p1_timing, needs_models=True, async_handler=True)

    e2e = sub.add_parser("run-e2e")
    _add_shared(e2e)
    _add_model_flags(e2e)
    e2e.add_argument("--contract", choices=["point", "cp-upper"], required=True)
    e2e.add_argument("--group", choices=list(GROUPS), default=None)
    e2e.add_argument(
        "--judge-model",
        required=True,
        help="MEME §4.1 LLM judge; required for run-e2e, never silently omitted",
    )
    e2e.set_defaults(handler=cmd_run_e2e, needs_models=True, async_handler=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "needs_models", False):
        _require_models(parser, args)
    handler = args.handler
    if getattr(args, "async_handler", False):
        return asyncio.run(handler(args))
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
