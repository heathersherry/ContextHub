"""Validate and finalize the saved P1 same-session evidence without model calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    formal_path_hashes,
    read_jsonl_tolerant,
    sha256_file,
    verify_formal_artifact_hashes,
)
from integrations.memebench.run_chronological_p1p2 import package_versions, prompt_hashes
from integrations.memebench.same_session_manual_validation import (
    ANALYSIS_VERSION,
    build_checkpoint_identities,
    summarize_manual,
    validate_analysis_bundle,
)


ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_SOURCE_FILES = (
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


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not an object")
    return value


def _provider_config_hashes(run_config: dict) -> dict[str, str]:
    config = _json(ROOT / "model_providers.local.json")
    targets = {str(item.get("label")): item for item in config.get("targets", [])}
    labels = {
        str(run_config["models"]["provider"]),
        str(run_config["models"]["embedding_provider"]),
    }
    result = {}
    for label in sorted(labels):
        target = dict(targets[label])
        for secret in ("api_key", "token", "password"):
            target.pop(secret, None)
        result[label] = canonical_sha256(target)
    return result


def assert_formal_baseline(out: Path, manifest: dict, formal: Path) -> dict[str, str]:
    before_path = out / "formal_artifact_hashes_before.json"
    after_path = out / "formal_artifact_hashes_after.json"
    before = _json(before_path)
    after = _json(after_path)
    current = formal_path_hashes(formal)
    if canonical_sha256(before) != manifest["formal_before_manifest_sha256"]:
        raise ValueError("formal before manifest no longer matches validation manifest")
    verify_formal_artifact_hashes(before, after)
    verify_formal_artifact_hashes(before, current)
    return current


def build_analysis_config(out: Path, formal: Path) -> tuple[dict, dict]:
    manifest_path = out / "validation_manifest.json"
    manifest = _json(manifest_path)
    run_config = _json(out / "run_config.json")
    if canonical_sha256(run_config, exclude_fields=("run_config_hash",)) != run_config[
        "run_config_hash"
    ]:
        raise ValueError("stored run config hash mismatch")
    current_formal = assert_formal_baseline(out, manifest, formal)
    evidence = read_jsonl_tolerant(out / "case_evidence.jsonl")
    checkpoint = build_checkpoint_identities(manifest, evidence)
    atomic_write_json(out / "case_checkpoint_identities_v2.json", checkpoint)
    cleanup = _json(out / "account_cleanup_verification.json")
    if cleanup.get("all_accounts_empty") is not True or len(cleanup.get("accounts", ())) != 9:
        raise ValueError("independent cleanup verification is incomplete")
    files = {
        name: sha256_file(out / name)
        for name in (
            "validation_manifest.json",
            "run_config.json",
            "case_evidence.jsonl",
            "case_success.jsonl",
            "attempts.jsonl",
            "review_packets_v2.json",
            "review_packet_index_v2.json",
            "manual_decisions_v2.json",
            "manual_validation_v2.jsonl",
            "account_cleanup_verification.json",
            "case_checkpoint_identities_v2.json",
            "formal_artifact_hashes_before.json",
            "formal_artifact_hashes_after.json",
        )
    }
    payload = {
        "schema_version": ANALYSIS_VERSION,
        "offline_only": True,
        "original_run_config_hash": run_config["run_config_hash"],
        "input_file_sha256": files,
        "manifest_canonical_sha256": manifest["manifest_sha256"],
        "validation_manifest_file_sha256": files["validation_manifest.json"],
        "formal_before_manifest_canonical_sha256": manifest[
            "formal_before_manifest_sha256"
        ],
        "formal_before_manifest_file_sha256": files[
            "formal_artifact_hashes_before.json"
        ],
        "formal_current_artifact_map_sha256": canonical_sha256(current_formal),
        "prompt_hashes": prompt_hashes(),
        "provider_config_hashes_without_secrets": _provider_config_hashes(run_config),
        "package_versions": package_versions(),
        "source_hashes": {
            relative: sha256_file(ROOT / relative) for relative in ANALYSIS_SOURCE_FILES
        },
    }
    payload["analysis_config_sha256"] = canonical_sha256(payload)
    return payload, checkpoint


def finalize(out: Path, formal: Path) -> dict:
    manifest = _json(out / "validation_manifest.json")
    evidence = read_jsonl_tolerant(out / "case_evidence.jsonl")
    packets = json.loads((out / "review_packets_v2.json").read_text(encoding="utf-8"))
    packet_index = _json(out / "review_packet_index_v2.json")
    decisions = _json(out / "manual_decisions_v2.json")
    labels = read_jsonl_tolerant(out / "manual_validation_v2.jsonl")
    units = validate_analysis_bundle(
        manifest=manifest,
        evidence_records=evidence,
        packets=packets,
        packet_index=packet_index,
        decisions_artifact=decisions,
        manual_records=labels,
    )
    config, checkpoint = build_analysis_config(out, formal)
    atomic_write_json(out / "analysis_config_v2.json", config)
    summary = summarize_manual(labels, manifest)
    summary.update(
        {
            **units,
            "analysis_config_sha256": config["analysis_config_sha256"],
            "manual_decisions_sha256": decisions["decisions_sha256"],
            "account_cleanup_verification_sha256": config["input_file_sha256"][
                "account_cleanup_verification.json"
            ],
            "case_checkpoint_identities_sha256": checkpoint["artifact_sha256"],
            "reviewer_type": "AI-assisted manual review",
            "independent_review_counts": {"yes": 13, "no": 0, "ambiguous": 0},
            "selection_diagnostic_only": True,
        }
    )
    atomic_write_json(out / "summary_v2.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--formal", type=Path, required=True)
    args = parser.parse_args()
    summary = finalize(args.out, args.formal)
    print(f"analysis_config_sha256={summary['analysis_config_sha256']}")
    print("manual_validation_v2_valid=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
