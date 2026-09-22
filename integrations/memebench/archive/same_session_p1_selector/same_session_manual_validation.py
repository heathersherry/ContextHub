"""Reproducible scoring-side helpers for P1 same-session manual validation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from integrations.memebench.gold_edge_audit import (
    canonical_json_bytes,
    canonical_sha256,
    manifest_sha256,
    read_jsonl_tolerant,
    sha256_file,
    validate_manifest_hash,
)
from integrations.memebench.loader import extract_cascade_cases, load_episodes


SCHEMA_VERSION = "p1-same-session-manual-validation-v1"
ANALYSIS_VERSION = "p1-same-session-manual-validation-v2"
DECISION_SCHEMA_VERSION = "p1-same-session-manual-decisions-v2"
LABELS = frozenset({"yes", "no", "ambiguous"})
LABEL_FIELDS = (
    "source_node_semantically_valid",
    "target_node_semantically_valid",
    "distinct_propositions",
    "same_session_confirmed",
    "earlier_valid_source_exists",
    "boundary_causal_supported",
)


def record_sha256(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(record)).hexdigest()


def semantic_edge_key(record: Mapping[str, Any]) -> str:
    return "|".join(
        (
            str(record["episode_id"]),
            str(record["gold_source_entity"]),
            str(record["gold_target_entity"]),
        )
    )


def prepare_validation_manifest(
    *,
    data: str | Path,
    old_audit: str | Path,
    formal: str | Path,
) -> dict[str, Any]:
    audit_root = Path(old_audit)
    old_manifest_path = audit_root / "audit_manifest.json"
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    validate_manifest_hash(old_manifest)
    reclassified_path = audit_root / "gold_edge_audit_reclassified.jsonl"
    raw_path = audit_root / "gold_edge_audit.jsonl"
    reclassified = read_jsonl_tolerant(reclassified_path)
    raw = read_jsonl_tolerant(raw_path)
    raw_by_identity = {
        (
            str(row["case_key"]),
            str(row["gold_edge_id"]),
            str(row["policy"]),
        ): row
        for row in raw
    }
    selected = [
        row
        for row in reclassified
        if row.get("first_failure_stage") == "same_session_atomic_boundary"
        and row.get("policy") == "R_full_cheap"
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in selected:
        grouped.setdefault(semantic_edge_key(row), []).append(row)
    if len(grouped) != 13:
        raise ValueError(f"expected 13 semantic edges, got {len(grouped)}")
    if len({row["episode_id"] for row in selected}) != 9:
        raise ValueError("expected 9 episode IDs")

    cases = {
        (case.hop, case.episode_id, case.target_entity): case
        for hop in (1, 2)
        for case in extract_cascade_cases(load_episodes(data), hop=hop)
    }
    semantic_edges: list[dict[str, Any]] = []
    relation_types: set[str] = set()
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda item: (int(item["hop"]), item["case_key"]))
        units = []
        patterns: set[str] = set()
        for row in rows:
            raw_key = (str(row["case_key"]), str(row["gold_edge_id"]), str(row["policy"]))
            original = raw_by_identity.get(raw_key)
            if original is None:
                raise ValueError(f"missing original audit record for {raw_key}")
            case = cases[(int(row["hop"]), str(row["episode_id"]), str(row["target_entity"]))]
            edge_patterns = {
                edge.pattern
                for edge in case.edges
                if edge.source == row["gold_source_entity"]
                and edge.target == row["gold_target_entity"]
            }
            patterns.update(edge_patterns)
            split = old_manifest["splits"][str(row["hop"])]
            episode_id = str(row["episode_id"])
            selection_ids = set(map(str, split["selection_ids"]))
            evaluation_ids = set(map(str, split["evaluation_ids"]))
            if episode_id not in selection_ids:
                raise ValueError(
                    f"{episode_id} hop {row['hop']} is not in the selection split"
                )
            if episode_id in evaluation_ids:
                raise ValueError(
                    f"{episode_id} hop {row['hop']} is in the evaluation split"
                )
            units.append(
                {
                    "hop": int(row["hop"]),
                    "case_key": str(row["case_key"]),
                    "gold_edge_id": str(row["gold_edge_id"]),
                    "target_entity": str(row["target_entity"]),
                    "original_audit_record_sha256": record_sha256(original),
                    "reclassified_record_sha256": record_sha256(row),
                    "selection_split_hash": str(split["split_hash"]),
                    "selection_split_file_sha256": str(split["sha256"]),
                }
            )
        first = rows[0]
        relation_types.add(
            f"{first['gold_source_entity']}->{first['gold_target_entity']}"
        )
        semantic_edges.append(
            {
                "semantic_edge_id": key,
                "episode_id": str(first["episode_id"]),
                "gold_source_entity": str(first["gold_source_entity"]),
                "gold_target_entity": str(first["gold_target_entity"]),
                "source_before_value": first["source_before_value"],
                "target_before_value": first["target_before_value"],
                "relation_type": (
                    f"{first['gold_source_entity']}->{first['gold_target_entity']}"
                ),
                "relation_patterns": sorted(patterns),
                "hop_edge_units": units,
            }
        )
    if len(relation_types) != 5:
        raise ValueError(f"expected 5 relation types, got {sorted(relation_types)}")

    episodes = []
    for episode_id in sorted({edge["episode_id"] for edge in semantic_edges}):
        edge_units = [
            unit
            for edge in semantic_edges
            if edge["episode_id"] == episode_id
            for unit in edge["hop_edge_units"]
        ]
        representative = min(edge_units, key=lambda item: (item["hop"], item["case_key"]))
        episodes.append(
            {
                "episode_id": episode_id,
                "representative_hop": representative["hop"],
                "representative_target_entity": representative["target_entity"],
                "semantic_edge_ids": [
                    edge["semantic_edge_id"]
                    for edge in semantic_edges
                    if edge["episode_id"] == episode_id
                ],
            }
        )
    manifest: dict[str, Any] = {
        "manifest_version": SCHEMA_VERSION,
        "selection_only": True,
        "policy": "R_full_cheap",
        "data_path": str(Path(data)),
        "data_sha256": sha256_file(data),
        "old_audit_root": str(audit_root),
        "old_audit_manifest_sha256": old_manifest["manifest_sha256"],
        "old_audit_reclassified_sha256": sha256_file(reclassified_path),
        "old_audit_raw_sha256": sha256_file(raw_path),
        "formal_root": str(Path(formal)),
        "semantic_edge_count": len(semantic_edges),
        "episode_count": len(episodes),
        "relation_types": sorted(relation_types),
        "semantic_edges": semantic_edges,
        "episodes": episodes,
    }
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    return manifest


def validate_selection_membership(manifest: Mapping[str, Any]) -> None:
    """Re-check every frozen hop-edge unit against the original split manifest."""

    old_root = Path(str(manifest["old_audit_root"]))
    old_manifest_path = old_root / "audit_manifest.json"
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    validate_manifest_hash(old_manifest)
    if old_manifest["manifest_sha256"] != manifest["old_audit_manifest_sha256"]:
        raise ValueError("old audit manifest canonical hash mismatch")
    for edge in manifest["semantic_edges"]:
        for unit in edge["hop_edge_units"]:
            split = old_manifest["splits"][str(unit["hop"])]
            episode_id = str(edge["episode_id"])
            if episode_id not in set(map(str, split["selection_ids"])):
                raise ValueError(f"{episode_id} hop {unit['hop']} is not selection")
            if episode_id in set(map(str, split["evaluation_ids"])):
                raise ValueError(f"{episode_id} hop {unit['hop']} is evaluation")
            if unit["selection_split_hash"] != split["split_hash"]:
                raise ValueError(f"{episode_id} hop {unit['hop']} split hash mismatch")
            if unit["selection_split_file_sha256"] != split["sha256"]:
                raise ValueError(f"{episode_id} hop {unit['hop']} split file mismatch")


def build_checkpoint_identities(
    manifest: Mapping[str, Any],
    evidence_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind legacy evidence to the exact representative case and gold-edge set."""

    cases = {
        (case.hop, case.episode_id, case.target_entity): case
        for hop in (1, 2)
        for case in extract_cascade_cases(load_episodes(manifest["data_path"]), hop=hop)
    }
    evidence_by_episode = {str(row["episode_id"]): row for row in evidence_records}
    rows = []
    for episode in manifest["episodes"]:
        key = (
            int(episode["representative_hop"]),
            str(episode["episode_id"]),
            str(episode["representative_target_entity"]),
        )
        case = cases[key]
        evidence = evidence_by_episode[key[1]]
        case_key = f"same-session|{key[1]}|R_full_cheap"
        if evidence.get("case_key") != case_key or evidence.get("episode_id") != key[1]:
            raise ValueError(f"{key[1]}: evidence case identity mismatch")
        gold_edge_ids = sorted(
            f"{case.episode_id}|{case.hop}|{edge.source}|{edge.target}"
            for edge in case.edges
        )
        rows.append(
            {
                "case_key": case_key,
                "episode_id": case.episode_id,
                "hop": case.hop,
                "policy": "R_full_cheap",
                "target_entity": case.target_entity,
                "gold_edge_ids": gold_edge_ids,
                "gold_edge_ids_sha256": canonical_sha256(gold_edge_ids),
                "case_evidence_record_sha256": canonical_sha256(evidence),
            }
        )
    artifact = {
        "schema_version": "p1-same-session-checkpoint-identities-v2",
        "manifest_sha256": manifest["manifest_sha256"],
        "cases": rows,
    }
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    return artifact


def validate_manual_record(record: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "semantic_edge_id",
        "episode_id",
        "gold_source_entity",
        "gold_target_entity",
        "source_before_value",
        "target_before_value",
        "hop_edge_units",
        "original_turn_quotes",
        "source_node_matches",
        "target_node_matches",
        "evidence",
        "reason",
        *LABEL_FIELDS,
    }
    missing = sorted(required - set(record))
    if missing:
        raise ValueError(f"manual validation record missing fields: {missing}")
    if record["schema_version"] != SCHEMA_VERSION:
        raise ValueError("manual validation schema mismatch")
    for field in LABEL_FIELDS:
        if record[field] not in LABELS:
            raise ValueError(f"{field} must be yes/no/ambiguous")
    prerequisite = all(
        record[field] == "yes"
        for field in (
            "source_node_semantically_valid",
            "target_node_semantically_valid",
            "distinct_propositions",
            "same_session_confirmed",
        )
    ) and record["earlier_valid_source_exists"] == "no"
    if record["boundary_causal_supported"] == "yes" and not prerequisite:
        raise ValueError("boundary causal label violates prerequisite rule")
    if not record["source_node_matches"] or not record["target_node_matches"]:
        raise ValueError("manual record must preserve all reviewed node matches")
    if not record["original_turn_quotes"]:
        raise ValueError("manual record must contain reviewable turn quotes")


def validate_analysis_bundle(
    *,
    manifest: Mapping[str, Any],
    evidence_records: Sequence[Mapping[str, Any]],
    packets: Sequence[Mapping[str, Any]],
    packet_index: Mapping[str, Any],
    decisions_artifact: Mapping[str, Any],
    manual_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Machine-check every identity/structure premise around human labels."""

    validate_manifest_hash(manifest)
    validate_selection_membership(manifest)
    edges = {edge["semantic_edge_id"]: edge for edge in manifest["semantic_edges"]}
    evidence_by_episode = {str(record["episode_id"]): record for record in evidence_records}
    if len(evidence_by_episode) != len(evidence_records) or set(evidence_by_episode) != {
        str(item["episode_id"]) for item in manifest["episodes"]
    }:
        raise ValueError("case evidence episode set does not match manifest")
    packets_by_edge = {packet["semantic_edge_id"]: packet for packet in packets}
    decisions = {
        decision["semantic_edge_id"]: decision
        for decision in decisions_artifact["decisions"]
    }
    manuals = {record["semantic_edge_id"]: record for record in manual_records}
    expected = set(edges)
    for name, mapping in (
        ("packets", packets_by_edge),
        ("decisions", decisions),
        ("manual records", manuals),
    ):
        if set(mapping) != expected or len(mapping) != len(expected):
            raise ValueError(f"{name} edge set does not match manifest")
    if decisions_artifact.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise ValueError("decision artifact manifest mismatch")
    if decisions_artifact.get("schema_version") != DECISION_SCHEMA_VERSION:
        raise ValueError("manual decision artifact schema mismatch")
    if decisions_artifact.get("reviewer_type") != "AI-assisted manual review":
        raise ValueError("manual decision reviewer type mismatch")
    if canonical_sha256(
        decisions_artifact, exclude_fields=("decisions_sha256",)
    ) != decisions_artifact.get("decisions_sha256"):
        raise ValueError("manual decision artifact hash mismatch")
    if canonical_sha256(
        packet_index, exclude_fields=("index_sha256",)
    ) != packet_index.get("index_sha256"):
        raise ValueError("review packet index hash mismatch")

    for edge_id, edge in edges.items():
        packet = packets_by_edge[edge_id]
        decision = decisions[edge_id]
        manual = manuals[edge_id]
        evidence = evidence_by_episode[edge["episode_id"]]
        identity_fields = (
            "semantic_edge_id",
            "episode_id",
            "gold_source_entity",
            "gold_target_entity",
            "source_before_value",
            "target_before_value",
            "hop_edge_units",
        )
        for field in identity_fields:
            expected_value = edge[field]
            if packet.get(field) != expected_value or decision.get(field) != expected_value:
                raise ValueError(f"{edge_id}: {field} differs from manifest")
            if manual.get(field) != expected_value:
                raise ValueError(f"{edge_id}: manual {field} differs from manifest")
        packet_hash = canonical_sha256(packet)
        if packet_index["packets"].get(edge_id) != packet_hash:
            raise ValueError(f"{edge_id}: review packet hash mismatch")
        if decision.get("evidence_packet_sha256") != packet_hash:
            raise ValueError(f"{edge_id}: decision evidence packet hash mismatch")
        if manual.get("evidence_packet_sha256") != packet_hash:
            raise ValueError(f"{edge_id}: manual evidence packet hash mismatch")
        if packet.get("case_evidence_record_sha256") != canonical_sha256(evidence):
            raise ValueError(f"{edge_id}: evidence record hash mismatch")
        if evidence.get("run_config_hash") != packet.get("run_config_hash"):
            raise ValueError(f"{edge_id}: run config identity mismatch")

        evidence_nodes = {str(node["node_id"]): node for node in evidence["nodes"]}
        expected_all_nodes = [
            {
                "node_id": node["node_id"],
                "text": node["text"],
                "session_index": node["session_index"],
                "original_session_id": node["original_session_id"],
            }
            for node in evidence["nodes"]
        ]
        if packet.get("all_extracted_nodes") != expected_all_nodes:
            raise ValueError(f"{edge_id}: earlier-source node corpus differs from evidence")
        match = next(
            (
                item
                for item in evidence["gold_node_matches"]
                if item["semantic_edge_id"] == edge_id
            ),
            None,
        )
        if match is None:
            raise ValueError(f"{edge_id}: missing gold node match")
        if match["source_before_value"] != edge["source_before_value"] or match[
            "target_before_value"
        ] != edge["target_before_value"]:
            raise ValueError(f"{edge_id}: evidence values differ from manifest")
        if packet["source_nodes"] != match["source_nodes"] or packet[
            "target_nodes"
        ] != match["target_nodes"]:
            raise ValueError(f"{edge_id}: packet match nodes differ from evidence")
        for node in packet["source_nodes"] + packet["target_nodes"]:
            if evidence_nodes.get(str(node["node_id"])) != node:
                raise ValueError(f"{edge_id}: packet node identity/text/session mismatch")

        source_id = str(decision["reviewed_valid_source_node_id"])
        target_id = str(decision["reviewed_valid_target_node_id"])
        source_nodes = {str(node["node_id"]): node for node in packet["source_nodes"]}
        target_nodes = {str(node["node_id"]): node for node in packet["target_nodes"]}
        if source_id not in source_nodes or target_id not in target_nodes:
            raise ValueError(f"{edge_id}: reviewed node is not in evidence matches")
        if source_id == target_id:
            raise ValueError(f"{edge_id}: source and target node IDs are identical")
        source, target = source_nodes[source_id], target_nodes[target_id]
        if decision.get("reviewed_source_text") != source["text"] or decision.get(
            "reviewed_target_text"
        ) != target["text"]:
            raise ValueError(f"{edge_id}: reviewed node text mismatch")
        citations = decision.get("evidence_citations")
        if (
            not isinstance(citations, Mapping)
            or citations.get("source_original_turn") != source["source_span"]
            or citations.get("target_original_turn") != target["source_span"]
            or citations.get("review_packet_sha256") != packet_hash
        ):
            raise ValueError(f"{edge_id}: reviewed citations mismatch")
        for role, node in (("source", source), ("target", target)):
            span = node["source_span"]
            turns = {
                int(turn["turn_index"]): turn
                for turn in node.get("original_turns", ())
            }
            turn = turns.get(int(span["turn_index"]))
            if turn is None or str(span["quote"]) not in str(turn["content"]):
                raise ValueError(f"{edge_id}: {role} quote is not in original turns")
        same_session = (
            int(source["session_index"]) == int(target["session_index"])
            and source["original_session_id"] == target["original_session_id"]
        )
        if decision["same_session_confirmed"] == "yes" and not same_session:
            raise ValueError(f"{edge_id}: same-session label contradicts evidence")
        if source["text"] == target["text"]:
            raise ValueError(f"{edge_id}: source and target texts are identical")
        traces = {
            str(trace["node_id"]): trace for trace in packet["target_candidate_traces"]
        }
        if target_id not in traces:
            raise ValueError(f"{edge_id}: missing reviewed target candidate trace")
        if source_id in set(map(str, traces[target_id]["candidate_snapshot_ids"])):
            raise ValueError(f"{edge_id}: reviewed source is already in target snapshot")
        earlier = decision.get("earlier_valid_source_review")
        if (
            not isinstance(earlier, Mapping)
            or earlier.get("judgment") != decision["earlier_valid_source_exists"]
            or not earlier.get("evidence_reference")
            or not earlier.get("reason")
            or earlier.get("reviewed_session_indices") is None
        ):
            raise ValueError(f"{edge_id}: earlier-source manual review is incomplete")
        for field in LABEL_FIELDS:
            if decision.get(field) not in LABELS:
                raise ValueError(f"{edge_id}: invalid manual decision {field}")
            if manual.get(field) != decision.get(field):
                raise ValueError(f"{edge_id}: writer changed manual decision {field}")
        if manual.get("manual_decision_sha256") != canonical_sha256(decision):
            raise ValueError(f"{edge_id}: manual decision hash mismatch")
        validate_manual_record(manual)

    hop_edge_units = sum(len(edge["hop_edge_units"]) for edge in edges.values())
    if hop_edge_units != 22:
        raise ValueError(f"expected 22 hop-edge units, got {hop_edge_units}")
    return {
        "unique_episode_ids": len({edge["episode_id"] for edge in edges.values()}),
        "semantic_edges": len(edges),
        "hop_edge_units": hop_edge_units,
    }


def summarize_manual(
    records: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    validate_manifest_hash(manifest)
    expected = {edge["semantic_edge_id"] for edge in manifest["semantic_edges"]}
    observed = {str(record["semantic_edge_id"]) for record in records}
    if len(records) != len(observed):
        raise ValueError("duplicate semantic_edge_id in manual validation")
    if observed != expected:
        raise ValueError(
            f"manual edge set mismatch: missing={sorted(expected-observed)}, "
            f"extra={sorted(observed-expected)}"
        )
    for record in records:
        validate_manual_record(record)
    counts = Counter(str(row["boundary_causal_supported"]) for row in records)
    return {
        "schema_version": ANALYSIS_VERSION,
        "manifest_sha256": manifest["manifest_sha256"],
        "unique_episode_id_count": len({row["episode_id"] for row in records}),
        "semantic_edge_count": len(records),
        "hop_edge_unit_count": sum(
            len(edge["hop_edge_units"]) for edge in manifest["semantic_edges"]
        ),
        "relation_type_count": len(manifest["relation_types"]),
        "boundary_causal_supported_counts": {
            label: counts.get(label, 0) for label in ("yes", "no", "ambiguous")
        },
        "selection_only": True,
        "not_p1_certification": True,
        "not_held_out_evaluation": True,
        "not_deployment_claim": True,
    }
