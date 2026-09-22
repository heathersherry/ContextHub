"""Derive paired/output analysis from immutable selector checkpoints."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
)
from integrations.memebench.same_session_selector_intervention import ARM_CONFIGS


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
DEFAULT_OUT = RUNS / "p1_same_session_selector_intervention_20260824_v2"
FROZEN = RUNS / "p1_same_session_manual_validation_20260823"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = round((len(ordered) - 1) * quantile)
    return ordered[index]


def finalize(out: Path = DEFAULT_OUT) -> dict[str, Any]:
    config = _json(out / "config.json")
    summary = _json(out / "summary.json")
    successes = read_jsonl_tolerant(out / "case_success.jsonl")
    if (
        summary["checkpoint_status"]["successful_case_count"]
        != config["expected_case_count"]
        or len({row["case_key"] for row in successes}) != config["expected_case_count"]
    ):
        raise ValueError("checkpoint is incomplete")
    decisions = _json(FROZEN / "manual_decisions_v2.json")["decisions"]
    manual = read_jsonl_tolerant(FROZEN / "manual_validation_v2.jsonl")
    manual_by_edge = {row["semantic_edge_id"]: row for row in manual}
    gold_node_pairs = {
        (
            str(row["episode_id"]),
            str(row["reviewed_valid_source_node_id"]),
            str(row["reviewed_valid_target_node_id"]),
        )
        for row in decisions
    }

    output_sets: dict[str, set[tuple[str, str, str]]] = {}
    output_rows: dict[str, dict[tuple[str, str, str], Mapping[str, Any]]] = {}
    arm_counts: dict[str, Any] = {}
    for arm in ARM_CONFIGS:
        keyed: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        gold = 0
        source_identity = 0
        for success in successes:
            if success["arm"] != arm:
                continue
            target_nodes = set(map(str, success["target_node_ids"]))
            for source in success["selected_sources"]:
                key = (
                    str(success["episode_id"]),
                    str(source["source_evidence_id"]),
                    str(success["target_evidence_id"]),
                )
                keyed[key] = {"success": success, "source": source}
                source_nodes = set(map(str, source["source_node_ids"]))
                matched_gold = any(
                    episode == success["episode_id"]
                    and source_node in source_nodes
                    and target_node in target_nodes
                    for episode, source_node, target_node in gold_node_pairs
                )
                gold += matched_gold
                for decision in decisions:
                    if (
                        decision["episode_id"] == success["episode_id"]
                        and decision["reviewed_valid_target_node_id"] in target_nodes
                    ):
                        substring = set(
                            map(
                                str,
                                manual_by_edge[decision["semantic_edge_id"]].get(
                                    "substring_only_source_match_node_ids", ()
                                ),
                            )
                        )
                        source_identity += bool(source_nodes & substring)
        output_sets[arm] = set(keyed)
        output_rows[arm] = keyed
        arm_successes = [row for row in successes if row["arm"] == arm]
        target_latencies = [
            sum(call["latency_seconds"] for call in row["model_calls"])
            for row in arm_successes
        ]
        episode_latencies = [
            sum(
                call["latency_seconds"]
                for row in arm_successes
                if row["episode_id"] == episode_id
                for call in row["model_calls"]
            )
            for episode_id in config["expected_episode_ids"]
        ]
        model_usage = {}
        for model, pricing in config["pricing_per_million_usd"].items():
            calls = [
                call
                for row in arm_successes
                for call in row["model_calls"]
                if call["model"] == model
            ]
            complete = [call for call in calls if call["usage"] is not None]
            prompt_tokens = sum(call["usage"]["prompt_tokens"] for call in complete)
            completion_tokens = sum(
                call["usage"]["completion_tokens"] for call in complete
            )
            model_usage[model] = {
                "calls": len(calls),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_incomplete_calls": len(calls) - len(complete),
                "known_usd": (
                    prompt_tokens * pricing["prompt"]
                    + completion_tokens * pricing["completion"]
                )
                / 1_000_000,
            }
        arm_counts[arm] = {
            "output_edge_count": len(keyed),
            "gold_output_count": gold,
            "non_gold_unmatched_output_count": len(keyed) - gold,
            "source_identity_violation_count": source_identity,
            "model_usage": model_usage,
            "per_target": {
                "candidate_count_p50": _percentile(
                    [row["candidate_count"] for row in arm_successes], 0.50
                ),
                "candidate_count_p95": _percentile(
                    [row["candidate_count"] for row in arm_successes], 0.95
                ),
                "output_edge_count_p50": _percentile(
                    [row["selected_source_count"] for row in arm_successes], 0.50
                ),
                "output_edge_count_p95": _percentile(
                    [row["selected_source_count"] for row in arm_successes], 0.95
                ),
                "latency_seconds_p50": _percentile(target_latencies, 0.50),
                "latency_seconds_p95": _percentile(target_latencies, 0.95),
            },
            "per_episode": {
                "latency_seconds_p50": _percentile(episode_latencies, 0.50),
                "latency_seconds_p95": _percentile(episode_latencies, 0.95),
            },
        }

    cheap = output_sets["turn_full_cheap"]
    verify = output_sets["turn_full_verify"]
    cheap_only = sorted(cheap - verify)
    verify_only = sorted(verify - cheap)
    paired = {
        "common_output_count": len(cheap & verify),
        "cheap_only_output_count": len(cheap_only),
        "verify_only_output_count": len(verify_only),
        "cheap_only": [
            {
                "episode_id": key[0],
                "source_evidence_id": key[1],
                "target_evidence_id": key[2],
                "source_text": output_rows["turn_full_cheap"][key]["source"]["source_text"],
                "target_text": output_rows["turn_full_cheap"][key]["success"]["target_text"],
            }
            for key in cheap_only
        ],
        "verify_only": [
            {
                "episode_id": key[0],
                "source_evidence_id": key[1],
                "target_evidence_id": key[2],
                "source_text": output_rows["turn_full_verify"][key]["source"]["source_text"],
                "target_text": output_rows["turn_full_verify"][key]["success"]["target_text"],
            }
            for key in verify_only
        ],
        "gold_rescued_by_verify": [],
        "gold_lost_by_verify": [],
    }

    frozen_evidence = read_jsonl_tolerant(FROZEN / "case_evidence.jsonl")
    old_history_edges = sum(
        len(trace["final_selected_source_ids"])
        for record in frozen_evidence
        for trace in record["candidate_traces"]
    )
    graph_inflation = {
        "baseline": "frozen 20260823 R_full_cheap history-only output incidences",
        "baseline_output_edge_incidences": old_history_edges,
        "comparison_is_descriptive_not_same-call-paired": True,
        "turn_full_cheap_output_edges": len(cheap),
        "turn_full_cheap_ratio": len(cheap) / old_history_edges,
        "turn_full_cheap_absolute_delta": len(cheap) - old_history_edges,
        "turn_full_verify_output_edges": len(verify),
        "turn_full_verify_ratio": len(verify) / old_history_edges,
        "turn_full_verify_absolute_delta": len(verify) - old_history_edges,
    }

    per_episode: dict[str, dict[str, Any]] = defaultdict(dict)
    for arm in ARM_CONFIGS:
        for episode_id in config["expected_episode_ids"]:
            rows = [
                row
                for row in successes
                if row["arm"] == arm and row["episode_id"] == episode_id
            ]
            per_episode[episode_id][arm] = {
                "target_count": len(rows),
                "input_history_candidates": sum(
                    row["history_candidate_count"] for row in rows
                ),
                "input_same_session_candidates": sum(
                    row["same_session_candidate_count"] for row in rows
                ),
                "output_edges": sum(row["selected_source_count"] for row in rows),
                "model_calls": sum(len(row["model_calls"]) for row in rows),
            }

    artifact: dict[str, Any] = {
        "schema_version": "p1-same-session-selector-final-analysis-v1",
        "experiment_label": "development intervention run",
        "held_out_certification": False,
        "production_claim": False,
        "input_hashes": {
            "config.json": sha256_file(out / "config.json"),
            "case_success.jsonl": sha256_file(out / "case_success.jsonl"),
            "summary.json": sha256_file(out / "summary.json"),
            "manual_review_decisions.json": sha256_file(
                out / "manual_review_decisions.json"
            ),
        },
        "arms": arm_counts,
        "paired_comparison": paired,
        "graph_inflation_vs_frozen_history_only": graph_inflation,
        "per_episode": dict(per_episode),
    }
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    atomic_write_json(out / "final_analysis.json", artifact)
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
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    print(json.dumps(finalize(args.out), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
