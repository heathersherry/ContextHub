"""Build and validate an offline-only scoring sidecar for the frozen P1 input.

No model, provider, router, or selector is imported or called.  The sidecar
joins hash-bound MEME loader records to frozen evidence text only for scoring.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    sha256_file,
)
from integrations.memebench.ingest import _norm
from integrations.memebench.loader import CascadeCase, extract_cascade_cases, load_episodes


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = (
    ROOT
    / "integrations"
    / "memebench"
    / "runs"
    / "p1_same_session_selector_full100_shared_20260824"
)
SIDECAR_NAME = "gold_scoring_side_v2.json"
MANIFEST_NAME = "gold_scoring_side_v2_manifest.json"
SCHEMA_VERSION = "p1-same-session-gold-scoring-side-v2"
RULE_VERSION = "edge-pr-raw-normalized-before-substring-v1"
EXPECTED_SHARED_CONTENT_HASH = (
    "b36add2260393f14f0b24ba82162db5f83dda99b66c84c9226c107f87738cce5"
)
EXPECTED_SHARED_INDEX_HASH = (
    "47241616abc53ead10517b26ba84a00b314c9e3c7cb7ca60ccb1dc965870dfad"
)
SOURCE_FILES = (
    "integrations/memebench/build_gold_scoring_side_v2.py",
    "integrations/memebench/loader.py",
    "integrations/memebench/ingest.py",
)


class ScoringSidecarError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScoringSidecarError(f"{path} must contain an object")
    return value


def gold_edge_identity_v1(source: str, target: str, hop: int) -> str:
    """Exactly reproduce the irreversible identity used by v1."""

    return canonical_sha256({"source": source, "target": target, "hop": hop})


def _existing_hashes(run: Path) -> dict[str, str]:
    excluded = {SIDECAR_NAME, MANIFEST_NAME}
    return {
        str(path.relative_to(run)): sha256_file(path)
        for path in sorted(item for item in run.rglob("*") if item.is_file())
        if str(path.relative_to(run)) not in excluded
    }


def _case_maps(data: Path) -> tuple[dict[str, CascadeCase], dict[str, CascadeCase]]:
    episodes = load_episodes(data)
    hop1 = {case.episode_id: case for case in extract_cascade_cases(episodes, hop=1)}
    hop2 = {case.episode_id: case for case in extract_cascade_cases(episodes, hop=2)}
    if len(hop1) != 100 or len(hop2) != 64:
        raise ScoringSidecarError(
            f"loader completeness mismatch: hop1={len(hop1)}, hop2={len(hop2)}"
        )
    return hop1, hop2


def _edge_rows(case: CascadeCase) -> list[dict[str, Any]]:
    rows = []
    for ordinal, edge in enumerate(case.edges):
        if edge.source not in case.entities or edge.target not in case.entities:
            raise ScoringSidecarError(
                f"{case.episode_id}: edge references unknown entity "
                f"{edge.source}->{edge.target}"
            )
        source = case.entities[edge.source]
        target = case.entities[edge.target]
        identity = gold_edge_identity_v1(edge.source, edge.target, edge.hop)
        rows.append(
            {
                "loader_edge_ordinal": ordinal,
                "gold_edge_record_id": "gold-edge-record-"
                + canonical_sha256(
                    {
                        "episode_id": case.episode_id,
                        "loader_edge_ordinal": ordinal,
                        "source": edge.source,
                        "target": edge.target,
                        "hop": edge.hop,
                        "pattern": edge.pattern,
                        "is_2hop_middle": edge.is_2hop_middle,
                    }
                ),
                "gold_edge_identity_v1": identity,
                "source_entity": edge.source,
                "target_entity": edge.target,
                "source_before_value": source.before,
                "target_before_value": target.before,
                "source_before_value_normalized": _norm(source.before),
                "target_before_value_normalized": _norm(target.before),
                "edge_hop": edge.hop,
                "edge_pattern": edge.pattern,
                "is_2hop_middle": edge.is_2hop_middle,
            }
        )
    return rows


def _entity_catalog(case: CascadeCase) -> list[dict[str, Any]]:
    return [
        {
            "entity": name,
            "before_value": entity.before,
            "before_value_normalized": _norm(entity.before),
            "after_value": entity.after,
            "entity_hop": entity.hop,
            "cascade_source": entity.cascade_source,
        }
        for name, entity in sorted(case.entities.items())
    ]


def _mapping_rows(
    episode_manifest: Mapping[str, Any], case: CascadeCase
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    node_rows: list[dict[str, Any]] = []
    by_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    entity_values = [
        (name, entity.before, _norm(entity.before))
        for name, entity in sorted(case.entities.items())
    ]
    for alignment in episode_manifest["alignments"]:
        normalized_text = _norm(alignment.get("text"))
        matches = [
            {
                "entity": name,
                "before_value": value,
                "before_value_normalized": normalized,
                "match_rule": RULE_VERSION,
            }
            for name, value, normalized in entity_values
            if normalized and normalized in normalized_text
        ]
        row = {
            "node_id": str(alignment["node_id"]),
            "evidence_id": alignment.get("evidence_id"),
            "text_sha256": canonical_sha256(str(alignment.get("text") or "")),
            "alignment_status": alignment["status"],
            "alignment_method": alignment["method"],
            "matched_entities": matches,
            "matched_entity_count": len(matches),
            "unmapped": not matches,
            "entity_mapping_ambiguous": len(matches) > 1,
            "alignment_ambiguous": alignment["status"] != "aligned",
            "source_identity_ambiguity_reasons": [
                reason
                for condition, reason in (
                    (
                        len(matches) > 1,
                        "multiple gold before-values are substrings of the proposition",
                    ),
                    (
                        alignment["status"] != "aligned",
                        "proposition has no unique raw-turn alignment",
                    ),
                    (
                        bool(matches),
                        "substring match may be a reason-clause/reference rather than an asserted source",
                    ),
                )
                if condition
            ],
            "approximate_diagnostic_only": True,
        }
        node_rows.append(row)
        if row["evidence_id"]:
            by_evidence[str(row["evidence_id"])].append(row)

    evidence_rows = []
    for evidence_id, aliases in sorted(by_evidence.items()):
        entity_map = {
            match["entity"]: match
            for row in aliases
            for match in row["matched_entities"]
        }
        evidence_rows.append(
            {
                "evidence_id": evidence_id,
                "node_ids": sorted({row["node_id"] for row in aliases}),
                "matched_entities": [
                    entity_map[name] for name in sorted(entity_map)
                ],
                "matched_entity_count": len(entity_map),
                "unmapped": not entity_map,
                "entity_mapping_ambiguous": len(entity_map) > 1,
                "has_alignment_ambiguous_alias": any(
                    row["alignment_ambiguous"] for row in aliases
                ),
                "source_identity_ambiguity": bool(entity_map),
                "source_identity_ambiguity_note": (
                    "A normalized before-value substring hit does not distinguish "
                    "an asserted source from a target/reason-clause reference."
                    if entity_map
                    else None
                ),
                "approximate_diagnostic_only": True,
            }
        )
    stats = {
        "node_mapping_count": len(node_rows),
        "node_unmapped_count": sum(row["unmapped"] for row in node_rows),
        "node_entity_ambiguous_count": sum(
            row["entity_mapping_ambiguous"] for row in node_rows
        ),
        "node_alignment_ambiguous_count": sum(
            row["alignment_ambiguous"] for row in node_rows
        ),
        "evidence_mapping_count": len(evidence_rows),
        "evidence_unmapped_count": sum(row["unmapped"] for row in evidence_rows),
        "evidence_entity_ambiguous_count": sum(
            row["entity_mapping_ambiguous"] for row in evidence_rows
        ),
        "evidence_source_identity_ambiguous_count": sum(
            row["source_identity_ambiguity"] for row in evidence_rows
        ),
    }
    return node_rows, evidence_rows, stats


def build_sidecar(run: Path, data: Path) -> dict[str, Any]:
    config = _json(run / "config.json")
    index = _json(run / "shared_manifest_index.json")
    v1 = _json(run / "gold_scoring_side.json")
    if sha256_file(data) != config["data_sha256"]:
        raise ScoringSidecarError("raw MEME data hash differs from frozen config")
    if index.get("manifest_content_sha256") != EXPECTED_SHARED_CONTENT_HASH:
        raise ScoringSidecarError("unexpected shared manifest content hash")
    if index.get("index_sha256") != EXPECTED_SHARED_INDEX_HASH:
        raise ScoringSidecarError("unexpected shared manifest index hash")
    if canonical_sha256(index, exclude_fields=("index_sha256",)) != index["index_sha256"]:
        raise ScoringSidecarError("shared manifest index canonical hash mismatch")

    hop1, hop2 = _case_maps(data)
    v1_by_key = {
        (row["episode_id"], int(record["hop"])): record
        for row in v1["episodes"]
        for record in row["scoring_records"]
    }
    episodes = []
    aggregate = Counter()
    for entry in index["episodes"]:
        episode_id = str(entry["episode_id"])
        shard = run / entry["manifest_path"]
        if sha256_file(shard) != entry["manifest_file_sha256"]:
            raise ScoringSidecarError(f"{episode_id}: frozen episode shard hash mismatch")
        frozen = _json(shard)
        node_mappings, evidence_mappings, mapping_stats = _mapping_rows(
            frozen, hop1[episode_id]
        )
        scoring_records = []
        for hop, cases in ((1, hop1), (2, hop2)):
            case = cases.get(episode_id)
            if case is None:
                continue
            edges = _edge_rows(case)
            identities = sorted({row["gold_edge_identity_v1"] for row in edges})
            v1_record = v1_by_key.get((episode_id, hop))
            if v1_record is None:
                raise ScoringSidecarError(f"{episode_id}/hop{hop}: v1 record missing")
            if identities != sorted(v1_record["gold_edge_identities"]):
                raise ScoringSidecarError(
                    f"{episode_id}/hop{hop}: v1 edge identity round-trip failed"
                )
            scoring_records.append(
                {
                    "gold_scoring_id": v1_record["gold_scoring_id"],
                    "hop": hop,
                    "target_entity": case.target_entity,
                    "loader_edge_count": len(case.edges),
                    "unique_gold_edge_identity_count": len(identities),
                    "gold_edge_identity_v1_set": identities,
                    "gold_edges": edges,
                }
            )
            aggregate[f"hop{hop}_loader_edge_count"] += len(case.edges)
            aggregate[f"hop{hop}_unique_gold_edge_count"] += len(identities)
        aggregate.update(mapping_stats)
        episodes.append(
            {
                "episode_id": episode_id,
                "episode_manifest_file_sha256": entry["manifest_file_sha256"],
                "entity_catalog": _entity_catalog(hop1[episode_id]),
                "node_to_entity_mappings": node_mappings,
                "evidence_to_entity_mappings": evidence_mappings,
                "mapping_stats": mapping_stats,
                "scoring_records": scoring_records,
            }
        )
    sidecar: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mapping_rule_version": RULE_VERSION,
        "scoring_only": True,
        "runtime_input": False,
        "selector_adapter_input": False,
        "approximation_warning": (
            "Normalized before-value substring mapping matches edge_pr_raw and is "
            "an approximate diagnostic. Repeated values, substrings, and target/"
            "reason-clause references can create multi-matches or source-identity "
            "ambiguity; approximate precision must not be reported as exact precision."
        ),
        "shared_manifest_content_sha256": EXPECTED_SHARED_CONTENT_HASH,
        "shared_manifest_index_sha256": EXPECTED_SHARED_INDEX_HASH,
        "episode_count": len(episodes),
        "hop1_episode_count": len(hop1),
        "hop2_episode_count": len(hop2),
        "aggregate_stats": dict(sorted(aggregate.items())),
        "episodes": episodes,
    }
    sidecar["canonical_content_sha256"] = canonical_sha256(sidecar)
    return sidecar


def validate_sidecar(
    sidecar: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    run: Path,
    data: Path,
) -> None:
    if sidecar.get("schema_version") != SCHEMA_VERSION:
        raise ScoringSidecarError("sidecar schema mismatch")
    actual_canonical = canonical_sha256(
        sidecar, exclude_fields=("canonical_content_sha256",)
    )
    if sidecar.get("canonical_content_sha256") != actual_canonical:
        raise ScoringSidecarError("sidecar canonical content hash mismatch")
    sidecar_path = run / SIDECAR_NAME
    if manifest.get("sidecar_file_sha256") != sha256_file(sidecar_path):
        raise ScoringSidecarError("sidecar byte hash mismatch")
    if manifest.get("sidecar_canonical_content_sha256") != actual_canonical:
        raise ScoringSidecarError("manifest does not bind canonical sidecar content")
    if manifest.get("data_sha256") != sha256_file(data):
        raise ScoringSidecarError("manifest data hash mismatch")
    if manifest.get("shared_manifest_content_sha256") != EXPECTED_SHARED_CONTENT_HASH:
        raise ScoringSidecarError("manifest shared content hash mismatch")
    if manifest.get("shared_manifest_index_sha256") != EXPECTED_SHARED_INDEX_HASH:
        raise ScoringSidecarError("manifest shared index hash mismatch")
    if manifest.get("episode_counts") != {"hop1": 100, "hop2": 64}:
        raise ScoringSidecarError("manifest episode counts mismatch")
    expected_manifest_hash = canonical_sha256(
        manifest, exclude_fields=("manifest_sha256",)
    )
    if manifest.get("manifest_sha256") != expected_manifest_hash:
        raise ScoringSidecarError("v2 manifest canonical hash mismatch")


def generate(run: Path = DEFAULT_RUN) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _json(run / "config.json")
    data = Path(config["data_path"])
    before = _existing_hashes(run)
    sidecar = build_sidecar(run, data)
    sidecar_path = run / SIDECAR_NAME
    if sidecar_path.exists() and _json(sidecar_path) != sidecar:
        raise ScoringSidecarError("existing v2 sidecar differs; refusing overwrite")
    if not sidecar_path.exists():
        atomic_write_json(sidecar_path, sidecar)
    after_sidecar = _existing_hashes(run)
    if before != after_sidecar:
        raise ScoringSidecarError("pre-existing shared-run files changed during generation")

    source_hashes = {name: sha256_file(ROOT / name) for name in SOURCE_FILES}
    manifest: dict[str, Any] = {
        "schema_version": "p1-same-session-gold-scoring-side-v2-manifest",
        "sidecar_schema_version": SCHEMA_VERSION,
        "mapping_rule_version": RULE_VERSION,
        "sidecar_path": SIDECAR_NAME,
        "sidecar_file_sha256": sha256_file(sidecar_path),
        "sidecar_canonical_content_sha256": sidecar["canonical_content_sha256"],
        "generation_source_hashes": source_hashes,
        "data_path": str(data),
        "data_sha256": sha256_file(data),
        "shared_manifest_content_sha256": EXPECTED_SHARED_CONTENT_HASH,
        "shared_manifest_index_sha256": EXPECTED_SHARED_INDEX_HASH,
        "shared_manifest_index_file_sha256": sha256_file(
            run / "shared_manifest_index.json"
        ),
        "episode_counts": {"hop1": 100, "hop2": 64},
        "preexisting_file_count": len(before),
        "preexisting_hash_map_sha256_before": canonical_sha256(before),
        "preexisting_hash_map_sha256_after": canonical_sha256(after_sidecar),
        "preexisting_files_unchanged": before == after_sidecar,
        "scoring_only": True,
        "model_calls": 0,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    manifest_path = run / MANIFEST_NAME
    if manifest_path.exists() and _json(manifest_path) != manifest:
        raise ScoringSidecarError("existing v2 manifest differs; refusing overwrite")
    if not manifest_path.exists():
        atomic_write_json(manifest_path, manifest)
    final_existing = _existing_hashes(run)
    if final_existing != before:
        raise ScoringSidecarError("pre-existing shared-run files changed after v2 freeze")
    validate_sidecar(sidecar, manifest, run=run, data=data)
    return sidecar, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        sidecar = _json(args.run / SIDECAR_NAME)
        manifest = _json(args.run / MANIFEST_NAME)
        data = Path(_json(args.run / "config.json")["data_path"])
        validate_sidecar(sidecar, manifest, run=args.run, data=data)
    else:
        sidecar, manifest = generate(args.run)
        print(
            json.dumps(
                {
                    "schema_version": sidecar["schema_version"],
                    "canonical_content_sha256": sidecar[
                        "canonical_content_sha256"
                    ],
                    "file_sha256": manifest["sidecar_file_sha256"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "aggregate_stats": sidecar["aggregate_stats"],
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
