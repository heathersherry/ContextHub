"""Run the offline MEME turn-aware same-session candidate mechanism experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
)
from integrations.memebench.turn_candidate_mechanism import (
    DEFAULT_MIN_TOKEN_OVERLAP,
    PERMUTATION_SEEDS,
    SCHEMA_VERSION,
    audit_candidate_invariants,
    build_non_gold_review_packet,
    candidate_inflation,
    generate_turn_candidates,
    score_with_manual_gold,
    summarize_manual_review,
    verify_permutation_invariance,
)


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
FROZEN = RUNS / "p1_same_session_manual_validation_20260823"
FORMAL = RUNS / "chronological_20260820" / "formal"
OLD_AUDIT = RUNS / "p1_gold_edge_audit_20260822"
DEFAULT_OUT = RUNS / "p1_same_session_turn_candidate_mechanism_20260824"
SOURCE_FILES = (
    "integrations/memebench/turn_candidate_mechanism.py",
    "integrations/memebench/run_turn_candidate_mechanism.py",
    "integrations/memebench/gold_edge_audit.py",
)
FROZEN_INPUT_FILES = (
    "validation_manifest.json",
    "manual_decisions_v2.json",
    "case_evidence.jsonl",
    "review_packets_v2.json",
    "manual_validation_v2.jsonl",
)
OLD_AUDIT_FILES = (
    "audit_manifest.json",
    "gold_edge_audit.jsonl",
    "gold_edge_audit_reclassified.jsonl",
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _protected_hashes(
    *,
    formal: Path,
    old_audit: Path,
    frozen: Path,
) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(item for item in formal.rglob("*") if item.is_file()):
        rows[f"formal/{path.relative_to(formal)}"] = sha256_file(path)
    for name in OLD_AUDIT_FILES:
        path = old_audit / name
        rows[f"gold-edge-audit-20260822/{name}"] = sha256_file(path)
    for name in FROZEN_INPUT_FILES:
        path = frozen / name
        rows[f"manual-validation-20260823/{name}"] = sha256_file(path)
    return rows


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    payload = "".join(
        json.dumps(dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
        for row in rows
    ).encode("utf-8")
    if path.exists() and path.read_bytes() == payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _input_hashes(frozen: Path) -> dict[str, str]:
    return {name: sha256_file(frozen / name) for name in FROZEN_INPUT_FILES}


def _source_hashes() -> dict[str, str]:
    return {name: sha256_file(ROOT / name) for name in SOURCE_FILES}


def _output_hashes(out: Path) -> dict[str, Any]:
    files = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name != "output_hashes.json"
    }
    artifact: dict[str, Any] = {
        "schema_version": "p1-turn-candidate-output-hashes-v1",
        "files": files,
    }
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    return artifact


def run(
    *,
    out: Path = DEFAULT_OUT,
    frozen: Path = FROZEN,
    formal: Path = FORMAL,
    old_audit: Path = OLD_AUDIT,
    min_token_overlap: float = DEFAULT_MIN_TOKEN_OVERLAP,
    sample_per_episode: int = 2,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    protected_before = _protected_hashes(
        formal=formal, old_audit=old_audit, frozen=frozen
    )
    atomic_write_json(out / "protected_artifact_hashes_before.json", protected_before)

    manifest = _json(frozen / "validation_manifest.json")
    decisions_artifact = _json(frozen / "manual_decisions_v2.json")
    evidence_records = read_jsonl_tolerant(frozen / "case_evidence.jsonl")
    manual_records = read_jsonl_tolerant(frozen / "manual_validation_v2.jsonl")
    if (
        manifest.get("semantic_edge_count") != 13
        or manifest.get("episode_count") != 9
        or len(evidence_records) != 9
        or len(decisions_artifact.get("decisions", ())) != 13
    ):
        raise ValueError("frozen development sample is not the expected 13-edge/9-episode set")

    config: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "experiment_scope": "development-mechanism-only",
        "benchmark_only": True,
        "paid_model_calls": 0,
        "held_out_certification": False,
        "generator_inputs": [
            "episode_id",
            "node text",
            "raw session identity/index",
            "raw turns",
            "turn/span alignment",
        ],
        "generator_forbidden_inputs": [
            "extractor array position",
            "node UUID ordering",
            "gold entity/value",
            "reviewed source/target IDs",
            "manual labels",
            "future session nodes",
        ],
        "ambiguous_policy": "quarantine-no-active-candidate",
        "same_turn_policy": "unresolved-no-active-candidate",
        "min_token_overlap": min_token_overlap,
        "permutation_seeds": list(PERMUTATION_SEEDS),
        "review_sampling": {
            "sample_per_episode": sample_per_episode,
            "uses_candidate_score": False,
        },
        "input_file_sha256": _input_hashes(frozen),
        "source_hashes": _source_hashes(),
    }
    config["config_sha256"] = canonical_sha256(config)
    atomic_write_json(out / "config.json", config)
    atomic_write_json(
        out / "input_artifact_hashes.json",
        {
            "schema_version": "p1-turn-candidate-input-hashes-v1",
            "input_file_sha256": config["input_file_sha256"],
            "source_hashes": config["source_hashes"],
            "config_sha256": config["config_sha256"],
        },
    )

    results = []
    permutations = []
    for evidence in sorted(evidence_records, key=lambda row: str(row["episode_id"])):
        episode_id = str(evidence["episode_id"])
        nodes = list(evidence["nodes"])
        results.append(
            generate_turn_candidates(
                episode_id,
                nodes,
                min_token_overlap=min_token_overlap,
            )
        )
        permutations.append(
            verify_permutation_invariance(
                episode_id,
                nodes,
                min_token_overlap=min_token_overlap,
            )
        )

    invariants = audit_candidate_invariants(results)
    scoring = score_with_manual_gold(
        results,
        decisions_artifact["decisions"],
        manual_records,
    )
    inflation = candidate_inflation(results, evidence_records)
    review_packet = build_non_gold_review_packet(
        results,
        scoring["recovered_candidate_edge_ids"],
        sample_per_episode=sample_per_episode,
    )
    manual_review_path = out / "manual_review_decisions_v1.json"
    manual_review = summarize_manual_review(
        review_packet,
        _json(manual_review_path) if manual_review_path.exists() else None,
    )

    alignments = [
        {"episode_id": result["episode_id"], **row}
        for result in results
        for row in result["alignments"]
    ]
    candidates = [
        row for result in results for row in result["candidates"]
    ]
    unresolved = [
        row for result in results for row in result["same_turn_unresolved"]
    ]
    _write_jsonl(out / "alignments.jsonl", alignments)
    _write_jsonl(out / "candidates.jsonl", candidates)
    _write_jsonl(out / "same_turn_unresolved.jsonl", unresolved)
    atomic_write_json(
        out / "permutation_checks.json",
        {
            "all_episodes_invariant": all(row["all_identical"] for row in permutations),
            "episode_count": len(permutations),
            "checks": permutations,
        },
    )
    atomic_write_json(out / "candidate_inflation.json", inflation)
    atomic_write_json(out / "gold_scoring.json", scoring)
    atomic_write_json(out / "non_gold_review_packet_v1.json", review_packet)

    protected_after = _protected_hashes(
        formal=formal, old_audit=old_audit, frozen=frozen
    )
    atomic_write_json(out / "protected_artifact_hashes_after.json", protected_after)
    if protected_before != protected_after:
        raise ValueError("protected formal/audit/manual-validation artifacts changed")

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "config_sha256": config["config_sha256"],
        "development_mechanism_only": True,
        "not_held_out_certification": True,
        "paid_model_calls": 0,
        "gold_recovery": {
            "semantic_edges": scoring["semantic_edge_count"],
            "recovered": scoring["semantic_edge_recovered_count"],
            "episodes": scoring["episode_count"],
            "episodes_all_edges_recovered": scoring[
                "episode_all_edges_recovered_count"
            ],
        },
        "candidate_counts": {
            "same_session_cross_turn": invariants["candidate_count"],
            "non_gold_unmatched": review_packet["unmatched_candidate_count"],
            "same_turn_unresolved": invariants["same_turn_unresolved_count"],
        },
        "inflation": {
            key: inflation[key]
            for key in (
                "history_only_same_session_candidate_count",
                "history_only_candidate_count",
                "added_same_session_cross_turn_candidate_count",
                "combined_candidate_count",
                "candidate_multiplier",
                "absolute_delta",
                "added_share_of_combined",
            )
        },
        "alignment_strata": scoring["alignment_strata"],
        "invariants": {
            **invariants,
            "extractor_order_dependence": not all(
                row["all_identical"] for row in permutations
            ),
            "look_ahead_candidate_count": invariants["future_to_past_count"],
            "future_session_source_candidate_count": 0,
        },
        "source_identity": {
            "substring_only_matches_counted_as_gold_recovery": 0,
            "edges_with_substring_only_outgoing_candidates": sum(
                row["substring_only_outgoing_candidate_count"] > 0
                for row in scoring["edge_results"]
            ),
            "edges_with_substring_only_candidate_to_reviewed_target": sum(
                row["substring_only_to_gold_target_count"] > 0
                for row in scoring["edge_results"]
            ),
            "candidate_classification": "reference/identity-ambiguous",
            "classification_scope": "scoring-side-audit-only",
        },
        "manual_review": manual_review,
        "protected_artifacts_byte_identical": protected_before == protected_after,
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "summary.json", summary)
    atomic_write_json(out / "output_hashes.json", _output_hashes(out))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--frozen", type=Path, default=FROZEN)
    parser.add_argument("--formal", type=Path, default=FORMAL)
    parser.add_argument("--old-audit", type=Path, default=OLD_AUDIT)
    parser.add_argument(
        "--min-token-overlap",
        type=float,
        default=DEFAULT_MIN_TOKEN_OVERLAP,
    )
    parser.add_argument("--sample-per-episode", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(
        out=args.out,
        frozen=args.frozen,
        formal=args.formal,
        old_audit=args.old_audit,
        min_token_overlap=args.min_token_overlap,
        sample_per_episode=args.sample_per_episode,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
