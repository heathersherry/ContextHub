"""CLI and programmable entrypoint for EntCollabBench 2x2 evaluation."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from integrations.entcollabbench.metrics import (
    InstanceResult,
    aggregate_main_table,
    h2_deltas,
    to_jsonable_result,
)
from integrations.entcollabbench.systems import build_system
from integrations.entcollabbench.world_loader import LoadedWorld, WorldLoader


DEFAULT_METRICS = (
    "task_success",
    "workflow_closure_rate",
    "false_block",
    "blocked_unsafe_action_rate",
    "total_tokens",
)


@dataclass(frozen=True)
class EvalConfig:
    models: dict[str, str]
    systems: tuple[str, ...]
    subsets: tuple[str, ...]
    instances: int
    seeds: int
    out: Path
    dry_run: bool = False
    account_id: str = "entcollab-eval"


async def run_eval(
    config: EvalConfig,
    *,
    repo=None,
    acl=None,
    audit=None,
    runner=None,
    instance_source=None,
    loaded: LoadedWorld | None = None,
) -> dict[str, Any]:
    """Run model × system × subset × seed orchestration.

    ``runner`` is the integration point for the real EntCollabBench Python
    runner. Tests and ``--dry-run`` pass fixture instances instead.
    """

    if not config.dry_run and runner is None and instance_source is None:
        raise RuntimeError(
            "Real EntCollabBench execution requires a runner or instance_source. "
            "Use --dry-run for the local mock smoke path."
        )

    results: list[InstanceResult] = []
    for subset in config.subsets:
        instances = _instances_for_subset(config, subset, instance_source=instance_source)
        for seed in range(config.seeds):
            for model_alias, model_id in config.models.items():
                for instance in instances:
                    for system_name in config.systems:
                        loaded_world = await _loaded_world_for_instance(
                            instance,
                            repo=repo,
                            account_id=config.account_id,
                            loaded=loaded,
                            needs_load=system_name.startswith("S2"),
                        )
                        system = build_system(
                            system_name,
                            repo=repo,
                            account_id=config.account_id,
                            loaded=loaded_world,
                            acl=acl,
                            audit=audit,
                            runner=runner,
                        )
                        result = await system.run_instance(
                            instance,
                            model_id,
                            seed=seed,
                            subset=subset,
                        )
                        result.model = model_alias
                        results.append(result)

    s0_oracles = {
        (result.instance_id, result.model, result.seed): result
        for result in results
        if result.system == "S0"
    }
    table = aggregate_main_table(results, metrics=DEFAULT_METRICS, s0_oracles=s0_oracles)
    deltas = h2_deltas(table, metric="task_success")

    _write_jsonl(config.out, results)
    summary_path = config.out.with_suffix(config.out.suffix + ".summary.csv")
    _write_summary_csv(summary_path, table)
    h2_path = config.out.with_suffix(config.out.suffix + ".h2.json")
    h2_path.write_text(json.dumps(deltas, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {
        "results": results,
        "main_table": table,
        "h2_deltas": deltas,
        "results_path": str(config.out),
        "summary_path": str(summary_path),
        "h2_path": str(h2_path),
    }


def parse_args(argv: list[str] | None = None) -> EvalConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models",
        default="weak=gpt-4o-mini,strong=gpt-5",
        help="Comma-separated aliases, e.g. weak=gpt-4o-mini,strong=gpt-5",
    )
    parser.add_argument("--systems", default="S0,S1,S2,S2a,S2b")
    parser.add_argument("--subset", choices=["workflow", "approval", "both"], default="both")
    parser.add_argument("--instances", type=int, default=1)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--out", default="results.jsonl")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--account-id", default="entcollab-eval")
    args = parser.parse_args(argv)

    subsets = ("workflow", "approval") if args.subset == "both" else (args.subset,)
    return EvalConfig(
        models=_parse_models(args.models),
        systems=tuple(item.strip() for item in args.systems.split(",") if item.strip()),
        subsets=subsets,
        instances=args.instances,
        seeds=args.seeds,
        out=Path(args.out),
        dry_run=args.dry_run,
        account_id=args.account_id,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    summary = asyncio.run(run_eval(config))
    print(f"wrote results: {summary['results_path']}")
    print(f"wrote summary: {summary['summary_path']}")
    print(f"wrote h2 deltas: {summary['h2_path']}")
    return 0


def _instances_for_subset(config: EvalConfig, subset: str, *, instance_source) -> list[dict[str, Any]]:
    if instance_source is None:
        return _dry_run_instances(subset, config.instances)

    value = instance_source(subset=subset, limit=config.instances)
    if asyncio.iscoroutine(value):
        raise TypeError("async instance_source is not supported; materialize instances before run_eval")
    return list(value)[: config.instances]


async def _loaded_world_for_instance(
    instance: dict[str, Any],
    *,
    repo,
    account_id: str,
    loaded: LoadedWorld | None,
    needs_load: bool,
) -> LoadedWorld:
    if loaded is not None:
        return loaded
    if needs_load and repo is not None and instance.get("world") is not None:
        return await WorldLoader(repo, account_id).load(instance["world"])
    return LoadedWorld()


def _dry_run_instances(subset: str, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"dry-{subset}-{idx}",
            "subset": subset,
            "task_success": True,
            "subtask_success": 1.0,
            "agent_pass": 1.0,
            "workflow_closure": True,
            "trace": [
                {"boundary": "handoff"},
                {"boundary": "tool_call"},
                {"boundary": "closure"},
            ],
            "events": [
                {
                    "boundary": "handoff",
                    "payload": {
                        "sender": "hr_service_specialist",
                        "recipient": "it_service_desk_l1",
                        "task_intent": "dry run handoff",
                        "expected_action": "continue",
                    },
                    "oracle_violation": False,
                },
                {
                    "boundary": "tool_call",
                    "payload": {
                        "tool_name": "update_ticket",
                        "allowed_tools": ["update_ticket"],
                        "tool_schema": {
                            "type": "object",
                            "required": ["status"],
                            "properties": {"status": {"enum": ["open", "closed"]}},
                        },
                        "tool_args": {"status": "closed"},
                    },
                    "oracle_violation": False,
                },
            ],
            "costs": {
                "total_tokens": 100 + idx,
                "tool_calls": 1,
                "delegations": 1,
                "repair_rounds": 0,
            },
            "latency_overheads_ms": [0.0],
        }
        for idx in range(limit)
    ]


def _parse_models(raw: str) -> dict[str, str]:
    models: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise ValueError("--models entries must be alias=model_id")
        alias, model_id = item.split("=", 1)
        models[alias.strip()] = model_id.strip()
    if not models:
        raise ValueError("--models must contain at least one alias=model_id")
    return models


def _write_jsonl(path: Path, results: list[InstanceResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(to_jsonable_result(result), sort_keys=True) + "\n")


def _write_summary_csv(path: Path, table: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["system", "model", "metric", "mean", "variance", "n"],
        )
        writer.writeheader()
        for system, model_cells in sorted(table.items()):
            for model, metric_cells in sorted(model_cells.items()):
                for metric, stats in sorted(metric_cells.items()):
                    writer.writerow(
                        {
                            "system": system,
                            "model": model,
                            "metric": metric,
                            "mean": stats["mean"],
                            "variance": stats["variance"],
                            "n": stats["n"],
                        }
                    )


if __name__ == "__main__":
    raise SystemExit(main())
