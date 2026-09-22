"""Run the zero-call full100 endpoint and alignment counterfactual audit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from integrations.memebench.full100_endpoint_counterfactual import (
    DEFAULT_MIN_OVERLAP,
    CounterfactualError,
    audit_alignment_invariants,
    generate_provenance_candidates,
    recompute_metrics,
    score_alignment_recovery,
    validate_ledger,
    verify_alignment_permutation,
)
from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
SHARED = RUNS / "p1_same_session_selector_full100_shared_20260824"
CHEAP = RUNS / "p1_same_session_selector_full100_cheap_20260824"
COMPARISON = RUNS / "p1_same_session_selector_full100_comparison_20260824"
LEDGER = (
    ROOT
    / "integrations"
    / "memebench"
    / "adjudications"
    / "p1_full100_endpoint_adjudication_v1.json"
)
DEFAULT_OUT = RUNS / "p1_full100_offline_adjudication_counterfactual_20260824_v3"
SOURCE_FILES = (
    "integrations/memebench/full100_endpoint_counterfactual.py",
    "integrations/memebench/run_full100_endpoint_counterfactual.py",
)
INPUT_ROOTS = {"shared": SHARED, "cheap": CHEAP, "comparison": COMPARISON}
RAW = {
    1: {"gold_edges": 333, "hits": 314, "episode_misses": 16, "episodes": 100},
    2: {"gold_edges": 261, "hits": 249, "episode_misses": 9, "episodes": 64},
}


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CounterfactualError(f"{path} must contain an object")
    return value


def _tree_hashes() -> dict[str, str]:
    return {
        f"{label}/{path.relative_to(root)}": sha256_file(path)
        for label, root in INPUT_ROOTS.items()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    payload = "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for row in rows
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    temporary.replace(path)


def _stage_manifest(
    stage: str,
    *,
    config_sha256: str,
    inputs: Mapping[str, str],
    outputs: Mapping[str, str],
    code_hashes: Mapping[str, str],
) -> dict[str, Any]:
    value = {
        "schema_version": "p1-full100-offline-stage-manifest-v1",
        "stage": stage,
        "offline_only_no_model_or_api_calls": True,
        "config_sha256": config_sha256,
        "input_content_hashes": dict(sorted(inputs.items())),
        "output_content_hashes": dict(sorted(outputs.items())),
        "code_fingerprint": dict(sorted(code_hashes.items())),
    }
    value["manifest_sha256"] = canonical_sha256(value)
    return value


def _load_shared_shards() -> list[dict[str, Any]]:
    index = _json(SHARED / "shared_manifest_index.json")
    shards = []
    for row in index["episodes"]:
        path = SHARED / str(row["manifest_path"])
        if sha256_file(path) != row["manifest_file_sha256"]:
            raise CounterfactualError(f"shared shard hash mismatch: {row['episode_id']}")
        shards.append(_json(path))
    if len(shards) != 100 or sum(len(row["nodes"]) for row in shards) != 2394:
        raise CounterfactualError("shared full100 node completeness mismatch")
    return shards


def _run_alignment_without_gold(
    shards: Sequence[Mapping[str, Any]],
    *,
    min_overlap: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Gold-isolated runtime stage: only frozen shared nodes are in scope."""

    results = [
        generate_provenance_candidates(
            str(shard["episode_id"]),
            list(shard["nodes"]),
            min_overlap=min_overlap,
        )
        for shard in shards
    ]
    invariant = audit_alignment_invariants(results)
    permutations = all(
        verify_alignment_permutation(
            str(shard["episode_id"]),
            list(shard["nodes"]),
            min_overlap=min_overlap,
        )
        for shard in shards
    )
    baseline = sum(
        len(case["candidates"])
        for shard in shards
        for case in shard["selector_cases"]
    )
    candidate_count = int(invariant["candidate_count"])
    summary = {
        "schema_version": "p1-provenance-multicandidate-full100-summary-v1",
        "honest_provenance_boundary": {
            "true_extraction_time_provenance_available": False,
            "implemented_mode": "offline verifiable multi-hypothesis approximation",
            "not_claimed_as_provenance_first_runtime_implementation": True,
            "minimum_future_schema": [
                "source_document/session stable ID",
                "origin turn stable ID and monotonic event time",
                "source character/token span",
                "quoted source bytes and quote hash",
                "structured claim identity: type/value/polarity/validity",
                "extractor version and input content hash",
                "explicit multi-span support plus abstain/quarantine reason",
            ],
        },
        "alignment": {
            **invariant,
            "coverage_rate": (
                (invariant["aligned_unique_count"] + invariant["multi_candidate_count"])
                / invariant["alignment_node_count"]
            ),
            "unique_alignment_rate": (
                invariant["aligned_unique_count"] / invariant["alignment_node_count"]
            ),
            "ambiguous_retained_rate": (
                invariant["multi_candidate_count"] / invariant["alignment_node_count"]
            ),
            "quarantine_rate": (
                invariant["quarantine_count"] / invariant["alignment_node_count"]
            ),
        },
        "candidate_inflation": {
            "frozen_shared_candidate_incidences": baseline,
            "counterfactual_pair_provenanced_candidate_incidences": candidate_count,
            "absolute_delta": candidate_count - baseline,
            "multiplier": candidate_count / baseline if baseline else None,
        },
        "safety": {
            "all_100_episode_permutation_invariant": permutations,
            "extractor_array_order_used": False,
            "uuid_sort_used_for_causality": False,
            "future_turn_information_used_as_source": False,
            "candidates_not_persisted_as_dependency_edges": True,
        },
        "runtime_forbidden_inputs": [
            "gold sidecar",
            "endpoint adjudication ledger",
            "episode-specific rules",
            "MEME entity names",
            "model/API services",
        ],
    }
    return results, summary


def run(out: Path = DEFAULT_OUT, *, min_overlap: float = DEFAULT_MIN_OVERLAP) -> dict[str, Any]:
    if out in INPUT_ROOTS.values():
        raise CounterfactualError("output must not overlap frozen inputs")
    before = _tree_hashes()
    code_hashes = {name: sha256_file(ROOT / name) for name in SOURCE_FILES}
    ledger_file_hash = sha256_file(LEDGER)
    config = {
        "schema_version": "p1-full100-offline-adjudication-counterfactual-v1",
        "development_diagnostic_only": True,
        "held_out_certification": False,
        "production_go": False,
        "offline_only_no_model_or_api_calls": True,
        "min_overlap": min_overlap,
        "raw_approximate_frozen_counts": RAW,
        "epsilon_graph": [0.05, 0.10, 0.15],
        "ledger_path": str(LEDGER),
        "ledger_file_sha256": ledger_file_hash,
        "source_hashes": code_hashes,
        "input_roots": {key: str(value) for key, value in INPUT_ROOTS.items()},
    }
    config["config_sha256"] = canonical_sha256(config)
    out.mkdir(parents=True, exist_ok=False)
    atomic_write_json(out / "config.json", config)
    atomic_write_json(out / "input_hashes_before.json", before)

    # Stage 1 deliberately runs before either gold or adjudication is opened.
    shards = _load_shared_shards()
    alignment_results, alignment_summary = _run_alignment_without_gold(
        shards, min_overlap=min_overlap
    )
    _write_jsonl(out / "alignment_results.jsonl", alignment_results)
    atomic_write_json(out / "alignment_summary_pre_gold.json", alignment_summary)
    alignment_outputs = {
        "alignment_results.jsonl": sha256_file(out / "alignment_results.jsonl"),
        "alignment_summary_pre_gold.json": sha256_file(
            out / "alignment_summary_pre_gold.json"
        ),
    }
    atomic_write_json(
        out / "manifest_alignment_stage.json",
        _stage_manifest(
            "alignment_runtime_gold_isolated",
            config_sha256=config["config_sha256"],
            inputs={
                key: value
                for key, value in before.items()
                if key.startswith("shared/")
                and "gold_scoring_side" not in key
            },
            outputs=alignment_outputs,
            code_hashes=code_hashes,
        ),
    )

    # Stage 2 may now read the immutable adjudication ledger for scoring only.
    ledger = _json(LEDGER)
    decisions = validate_ledger(ledger)
    metrics = recompute_metrics(decisions, raw=RAW)
    recovery = score_alignment_recovery(alignment_results, decisions)
    atomic_write_json(out / "counterfactual_metrics.json", metrics)
    atomic_write_json(out / "alignment_recovery_post_gold.json", recovery)
    atomic_write_json(
        out / "adjudication_binding.json",
        {
            "ledger_path": str(LEDGER),
            "ledger_file_sha256": ledger_file_hash,
            "ledger_canonical_sha256": ledger["decisions_sha256"],
            "decision_count": len(decisions),
            "class_counts": {
                label: sum(row["decision_class"] == label for row in decisions)
                for label in sorted({row["decision_class"] for row in decisions})
            },
            "config_sha256": config["config_sha256"],
        },
    )
    scoring_outputs = {
        name: sha256_file(out / name)
        for name in (
            "counterfactual_metrics.json",
            "alignment_recovery_post_gold.json",
            "adjudication_binding.json",
        )
    }
    atomic_write_json(
        out / "manifest_scoring_stage.json",
        _stage_manifest(
            "adjudication_and_offline_rescoring",
            config_sha256=config["config_sha256"],
            inputs={
                "ledger_file": ledger_file_hash,
                "alignment_results": alignment_outputs["alignment_results.jsonl"],
                **{
                    key: value
                    for key, value in before.items()
                    if key.startswith(("cheap/", "comparison/", "shared/gold_scoring"))
                },
            },
            outputs=scoring_outputs,
            code_hashes=code_hashes,
        ),
    )

    after = _tree_hashes()
    atomic_write_json(out / "input_hashes_after.json", after)
    if before != after:
        raise CounterfactualError("a frozen input changed during analysis")
    summary = {
        "schema_version": "p1-full100-offline-adjudication-summary-v1",
        "config_sha256": config["config_sha256"],
        "input_hashes_before_after_identical": True,
        "offline_only_no_model_or_api_calls": True,
        "development_diagnostic_only": True,
        "primary_graph_miss": metrics["episode_cluster_unit"][
            "confirmed_pipeline_miss_all_100"
        ],
        "epsilon_sensitivity": metrics[
            "epsilon_sensitivity_primary_confirmed_pipeline_all_100"
        ],
        "alignment": alignment_summary,
        "alignment_recovery": recovery,
        "selector_rerun_interpretation": (
            "candidate recovery does not imply selection; only affected targets "
            "would need a future minimal paid selector rerun"
        ),
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "summary.json", summary)
    output_hashes = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name != "output_hashes.json"
    }
    atomic_write_json(
        out / "output_hashes.json",
        {"files": output_hashes, "artifact_sha256": canonical_sha256(output_hashes)},
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--min-overlap", type=float, default=DEFAULT_MIN_OVERLAP)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.out, min_overlap=args.min_overlap), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
