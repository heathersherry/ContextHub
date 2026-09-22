"""Materialize v2 labels only from the immutable manual-decision artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from integrations.memebench.gold_edge_audit import (
    atomic_write_text,
    canonical_json_bytes,
    canonical_sha256,
)
from integrations.memebench.same_session_manual_validation import (
    SCHEMA_VERSION,
    validate_manual_record,
)


def _compact(node: dict) -> dict:
    return {
        "node_id": node["node_id"],
        "text": node["text"],
        "session_index": node["session_index"],
        "original_session_id": node["original_session_id"],
        "source_span": node["source_span"],
    }


def materialize(out: Path) -> list[dict]:
    packets = json.loads((out / "review_packets_v2.json").read_text(encoding="utf-8"))
    artifact = json.loads((out / "manual_decisions_v2.json").read_text(encoding="utf-8"))
    by_packet = {packet["semantic_edge_id"]: packet for packet in packets}
    records = []
    for decision in artifact["decisions"]:
        edge_id = decision["semantic_edge_id"]
        packet = by_packet[edge_id]
        source = next(
            node
            for node in packet["source_nodes"]
            if node["node_id"] == decision["reviewed_valid_source_node_id"]
        )
        target = next(
            node
            for node in packet["target_nodes"]
            if node["node_id"] == decision["reviewed_valid_target_node_id"]
        )
        quotes = []
        for role, node in (("source", source), ("target", target)):
            span = node["source_span"]
            quotes.append(
                {
                    "role": role,
                    "session_index": node["session_index"],
                    "original_session_id": node["original_session_id"],
                    "turn_index": span["turn_index"],
                    "quote": span["quote"],
                    "span_method": span["method"],
                    "start": span["start"],
                    "end": span["end"],
                }
            )
        false_positive_source_matches = [
            node["node_id"]
            for node in packet["source_nodes"]
            if node["node_id"] != source["node_id"]
        ]
        record = {
            "schema_version": SCHEMA_VERSION,
            "semantic_edge_id": edge_id,
            "episode_id": packet["episode_id"],
            "gold_source_entity": packet["gold_source_entity"],
            "gold_target_entity": packet["gold_target_entity"],
            "source_before_value": packet["source_before_value"],
            "target_before_value": packet["target_before_value"],
            "relation_type": packet["relation_type"],
            "relation_patterns": packet["relation_patterns"],
            "hop_edge_units": packet["hop_edge_units"],
            "original_turn_quotes": quotes,
            "source_node_matches": [_compact(node) for node in packet["source_nodes"]],
            "target_node_matches": [_compact(node) for node in packet["target_nodes"]],
            "reviewed_valid_source_node_id": decision["reviewed_valid_source_node_id"],
            "reviewed_valid_target_node_id": decision["reviewed_valid_target_node_id"],
            "substring_only_source_match_node_ids": false_positive_source_matches,
            "source_node_semantically_valid": decision["source_node_semantically_valid"],
            "target_node_semantically_valid": decision["target_node_semantically_valid"],
            "distinct_propositions": decision["distinct_propositions"],
            "same_session_confirmed": decision["same_session_confirmed"],
            "earlier_valid_source_exists": decision["earlier_valid_source_exists"],
            "boundary_causal_supported": decision["boundary_causal_supported"],
            "reviewer_type": decision["reviewer_type"],
            "manual_decision_sha256": canonical_sha256(decision),
            "evidence_packet_sha256": decision["evidence_packet_sha256"],
            "earlier_valid_source_review": decision["earlier_valid_source_review"],
            "evidence": (
                f"source node {source['node_id']} (session {source['session_index']}) "
                f"states {packet['gold_source_entity']}={packet['source_before_value']!r}; "
                f"target node {target['node_id']} states "
                f"{packet['gold_target_entity']}={packet['target_before_value']!r}."
            ),
            "reason": decision["reason"],
        }
        validate_manual_record(record)
        records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent
        / "runs"
        / "p1_same_session_manual_validation_20260823",
    )
    args = parser.parse_args()
    records = materialize(args.out)
    rendered = b"".join(canonical_json_bytes(record) + b"\n" for record in records)
    atomic_write_text(args.out / "manual_validation_v2.jsonl", rendered.decode("utf-8"))
    print(f"manual_validation_edges={len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
