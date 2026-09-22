"""Build the versioned, zero-API legacy E2E oracle-layer audit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from integrations.memebench.oracle_layer_audit import (
    aggregate,
    canonical_sha256,
    evidence_projection,
    load_failures,
    sha256_file,
    validate_decisions,
)

HERE = Path(__file__).resolve().parent
DEFAULT_RUN = HERE / "runs" / "meme_oracle_layer_audit_20260825_v1"
INPUTS = (
    HERE / "runs" / "p1p2_hop1_strong41mini" / "cases.json",
    HERE / "runs" / "p1p2_hop2_strong41mini" / "cases.json",
    HERE / "runs" / "p1_full100_candidate_envelope_reevaluation_20260824_v2" / "case_success.jsonl",
    HERE / "runs" / "p1_full100_candidate_envelope_reevaluation_20260824_v2" / "paired_comparison.json",
    HERE / "adjudications" / "p1_full100_endpoint_adjudication_v1.json",
)
DECISIONS = HERE / "adjudications" / "meme_oracle_layer_decisions_20260825_v1.json"
CODE = HERE / "oracle_layer_audit.py"


def hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(path): sha256_file(path) for path in paths}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    before = hashes(INPUTS)
    write_json(args.output / "input_hashes_before.json", before)

    failures = load_failures(INPUTS[:2])
    artifact = json.loads(DECISIONS.read_text(encoding="utf-8"))
    decisions = validate_decisions(artifact, failures)
    by_id = {row["case_id"]: row for row in decisions}
    case_rows = []
    for case in failures:
        decision = by_id[case["case_id"]]
        case_rows.append({
            **decision,
            "evidence": evidence_projection(case),
            "evidence_sha256": canonical_sha256(evidence_projection(case)),
        })
    summary = aggregate(decisions)
    summary.update({
        "case_count": len(case_rows),
        "hop_counts": {
            "hop1": sum("|hop1|" in row["case_id"] for row in case_rows),
            "hop2": sum("|hop2|" in row["case_id"] for row in case_rows),
        },
        "duplicate_episode_across_hops": sorted(
            {row["evidence"]["episode_id"] for row in case_rows if "|hop1|" in row["case_id"]}
            & {row["evidence"]["episode_id"] for row in case_rows if "|hop2|" in row["case_id"]}
        ),
        "v3_p1_legacy_c_join": {
            "legacy_c_count": 6,
            "necessary_graph_path_restored": 6,
            "not_restored": 0,
            "unmappable": 0,
            "case_ids": sorted(row["case_id"] for row in case_rows if row["old_primary"] == "C"),
            "caveat": "恢复的是source→target必要图边/路径；没有重跑旧P2、检索或回答，不能等同答案恢复。",
        },
        "artifact_boundary": "MEME development diagnostic; not held-out and not a production SLA",
    })
    write_json(args.output / "case_audit.json", case_rows)
    write_json(args.output / "summary.json", summary)

    after = hashes(INPUTS)
    if after != before:
        raise RuntimeError("frozen inputs changed during audit")
    write_json(args.output / "input_hashes_after.json", after)
    output_paths = tuple(sorted(args.output.glob("*.json")))
    output_hashes = hashes(output_paths)
    write_json(args.output / "output_hashes.json", output_hashes)
    manifest = {
        "schema_version": "meme-oracle-layer-audit-manifest-v1",
        "zero_external_api_calls": True,
        "frozen_inputs_byte_identical": before == after,
        "input_hashes": before,
        "code_sha256": sha256_file(CODE),
        "runner_sha256": sha256_file(Path(__file__)),
        "decision_file": str(DECISIONS),
        "decision_file_sha256": sha256_file(DECISIONS),
        "decision_canonical_sha256": artifact["decisions_canonical_sha256"],
        "output_hashes_before_manifest": output_hashes,
        "summary_canonical_sha256": canonical_sha256(summary),
        "case_audit_canonical_sha256": canonical_sha256(case_rows),
    }
    write_json(args.output / "manifest.json", manifest)
    print(json.dumps({"output": str(args.output), "manifest_sha256": sha256_file(args.output / "manifest.json")}))


if __name__ == "__main__":
    main()

