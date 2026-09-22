"""Render compact, deterministic human-review packets from persisted evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
)


def render(out: Path) -> list[dict]:
    manifest = json.loads((out / "validation_manifest.json").read_text(encoding="utf-8"))
    successes = {
        (row["case_key"], row["attempt_id"])
        for row in read_jsonl_tolerant(out / "case_success.jsonl")
        if row.get("account_cleanup_complete") is True
    }
    evidence_rows = [
        row
        for row in read_jsonl_tolerant(out / "case_evidence.jsonl")
        if (row["case_key"], row["attempt_id"]) in successes
    ]
    by_episode = {str(row["episode_id"]): row for row in evidence_rows}
    packets = []
    for edge in manifest["semantic_edges"]:
        evidence = by_episode[str(edge["episode_id"])]
        match = next(
            item
            for item in evidence["gold_node_matches"]
            if item["semantic_edge_id"] == edge["semantic_edge_id"]
        )
        target_ids = {node["node_id"] for node in match["target_nodes"]}
        sessions = {}
        matched_nodes = match["source_nodes"] + match["target_nodes"]
        wanted = {
            (int(node["session_index"]), str(node["original_session_id"]))
            for node in matched_nodes
        }
        for node in evidence["nodes"]:
            key = (int(node["session_index"]), str(node["original_session_id"]))
            if key in wanted:
                sessions[key] = {
                    "session_index": key[0],
                    "original_session_id": key[1],
                    "original_turns": node["original_turns"],
                }
        packets.append(
            {
                **edge,
                "case_evidence_record_sha256": canonical_sha256(evidence),
                "case_evidence_file_sha256": sha256_file(out / "case_evidence.jsonl"),
                "run_config_hash": evidence["run_config_hash"],
                "source_nodes": match["source_nodes"],
                "target_nodes": match["target_nodes"],
                "matched_session_evidence": [sessions[key] for key in sorted(sessions)],
                "target_candidate_traces": [
                    trace
                    for trace in evidence["candidate_traces"]
                    if trace["node_id"] in target_ids
                ],
                "all_extracted_nodes": [
                    {
                        "node_id": node["node_id"],
                        "text": node["text"],
                        "session_index": node["session_index"],
                        "original_session_id": node["original_session_id"],
                    }
                    for node in evidence["nodes"]
                ],
            }
        )
    return packets


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
    packets = render(args.out)
    packet_path = args.out / "review_packets_v2.json"
    atomic_write_json(packet_path, packets)
    index = {
        "schema_version": "p1-same-session-review-index-v2",
        "case_evidence_file_sha256": sha256_file(args.out / "case_evidence.jsonl"),
        "packets": {
            packet["semantic_edge_id"]: canonical_sha256(packet) for packet in packets
        },
    }
    index["index_sha256"] = canonical_sha256(index)
    atomic_write_json(args.out / "review_packet_index_v2.json", index)
    packet_dir = args.out / "review_packets_v2"
    packet_dir.mkdir(parents=True, exist_ok=True)
    for index, packet in enumerate(packets, 1):
        lines = [
            f"# {packet['semantic_edge_id']}",
            "",
            f"- source: {packet['gold_source_entity']} = {packet['source_before_value']}",
            f"- target: {packet['gold_target_entity']} = {packet['target_before_value']}",
            f"- hop units: {[unit['hop'] for unit in packet['hop_edge_units']]}",
            "",
            "## Source matches",
        ]
        for node in packet["source_nodes"]:
            lines.append(
                f"- `{node['node_id']}` session {node['session_index']} "
                f"`{node['original_session_id']}`: {node['text']}"
            )
        lines.extend(("", "## Target matches"))
        for node in packet["target_nodes"]:
            lines.append(
                f"- `{node['node_id']}` session {node['session_index']} "
                f"`{node['original_session_id']}`: {node['text']}"
            )
        lines.extend(("", "## Original user turns"))
        for session in packet["matched_session_evidence"]:
            lines.append(
                f"### session {session['session_index']} `{session['original_session_id']}`"
            )
            for turn in session["original_turns"]:
                if turn["role"] == "user":
                    lines.append(f"- turn {turn['turn_index']}: {turn['content']}")
        node_text = {
            node["node_id"]: node["text"] for node in packet["all_extracted_nodes"]
        }
        source_ids = {node["node_id"] for node in packet["source_nodes"]}
        lines.extend(("", "## Target candidate traces"))
        for trace in packet["target_candidate_traces"]:
            snapshot = set(trace["candidate_snapshot_ids"])
            lines.append(
                f"- target `{trace['node_id']}` session {trace['session_index']}; "
                f"source matches in snapshot={sorted(source_ids & snapshot)}; "
                f"snapshot size={len(snapshot)}"
            )
            for candidate_id in trace["candidate_snapshot_ids"]:
                lines.append(f"  - `{candidate_id}`: {node_text.get(candidate_id, '<missing>')}")
        lines.extend(("", "## All extracted nodes (earlier-source review)"))
        for node in packet["all_extracted_nodes"]:
            lines.append(
                f"- session {node['session_index']} `{node['original_session_id']}` "
                f"`{node['node_id']}`: {node['text']}"
            )
        path = packet_dir / f"{index:02d}_{packet['episode_id']}_{packet['gold_source_entity']}_{packet['gold_target_entity']}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"review_packets={len(packets)}")
    print(f"review_packets_sha256={sha256_file(packet_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
