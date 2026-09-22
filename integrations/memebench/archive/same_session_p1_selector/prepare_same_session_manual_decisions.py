"""Freeze the 13 explicit AI-assisted manual-review decisions (v2)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    sha256_file,
)


def _decision(
    edge,
    source,
    target,
    reason,
    *,
    source_valid,
    target_valid,
    distinct,
    same_session,
    earlier_source,
    boundary,
):
    # Labels are mandatory at every call: this helper has no semantic defaults.
    return {
        "semantic_edge_id": edge,
        "reviewed_valid_source_node_id": source,
        "reviewed_valid_target_node_id": target,
        "source_node_semantically_valid": source_valid,
        "target_node_semantically_valid": target_valid,
        "distinct_propositions": distinct,
        "same_session_confirmed": same_session,
        "earlier_valid_source_exists": earlier_source,
        "boundary_causal_supported": boundary,
        "reviewer_type": "AI-assisted manual review",
        "reason": reason,
    }


EXPLICIT_DECISIONS = (
    _decision("pl_001|exercise_routine|fitness_facility", "465ad2fe-84d1-4de9-b19a-1168569cf1bd", "b654af76-5f29-4e1e-bb16-38cf10eeb696", "Dedicated swimming-routine and Crysthene-Pool propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_008|work_location|commute_method", "939a1444-b03e-4c78-acbf-94df86870cd6", "1149505f-75b7-4cec-9c8e-4e2220b6a1df", "Dedicated Thornvale office and walking-commute propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_008|work_location|favorite_restaurant", "939a1444-b03e-4c78-acbf-94df86870cd6", "7aa3cd04-9b0c-4ab4-8318-1ad04124d33b", "Dedicated Thornvale office and Zefiro restaurant propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_009|exercise_routine|fitness_facility", "bcd227ee-eb3c-4149-9237-89c51fc25b9f", "807f1b49-3218-45f2-b7fe-736773f432e1", "Dedicated pilates-routine and Zorathel-Gym propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_013|work_location|commute_duration", "c28d5a4e-d10b-4933-94fc-d01ba6d14d05", "a9d5254d-ced4-4f08-b8b2-8f3b29fbba6b", "Dedicated Quivira office and 40-minute commute propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_013|work_location|fitness_facility", "c28d5a4e-d10b-4933-94fc-d01ba6d14d05", "ca4372cb-9768-4278-b44f-2206ef4dbc2b", "Dedicated Quivira office and Pyravar-CrossFit propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_021|work_location|favorite_restaurant", "70a0c035-7a65-4318-8305-5adeb7868079", "7ac10282-e40f-4a74-82a8-71cc691bef81", "Dedicated Dravenna office and Kinthar-Grill propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_021|work_location|fitness_facility", "70a0c035-7a65-4318-8305-5adeb7868079", "4dcb16f6-b789-46cb-89da-6e73e4fa9a42", "Dedicated Dravenna office and Pyravar-CrossFit propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_023|exercise_routine|fitness_facility", "34e0e1c8-b705-4e97-ba9f-906f45e79026", "e7209bcf-f697-45b6-bf4f-66df8f970883", "Dedicated yoga-routine and Kaelstrom-Fitness propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_033|work_location|commute_duration", "5ac99c14-c352-49f7-9236-8520ee0388a9", "eb18caf3-5b35-4955-9bb0-0f810e27a2e4", "Dedicated remote-office and 10-minute commute propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_033|work_location|favorite_restaurant", "5ac99c14-c352-49f7-9236-8520ee0388a9", "0ee8d081-0774-485a-aee5-1bf2b41d4fa6", "Dedicated remote-office and Lorwen-and-Sage propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_034|exercise_routine|fitness_facility", "e700ae0b-41d4-43d9-9d91-f73d6c6e7e8f", "cfa428a1-9918-4c2e-b26e-06af03b00dbc", "Dedicated running-routine and Kaelstrom-Fitness propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
    _decision("pl_042|exercise_routine|fitness_facility", "a207b9d3-1efb-40fe-83f2-bd3f05b59f81", "09066d30-f26d-4b44-a418-eadade82674d", "Dedicated walking-routine and Velthari-Studio propositions are separate nodes in session 3.", source_valid="yes", target_valid="yes", distinct="yes", same_session="yes", earlier_source="no", boundary="yes"),
)


def build_decisions(out: Path) -> dict:
    packets = json.loads((out / "review_packets_v2.json").read_text(encoding="utf-8"))
    index = json.loads((out / "review_packet_index_v2.json").read_text(encoding="utf-8"))
    by_edge = {packet["semantic_edge_id"]: packet for packet in packets}
    decisions = []
    for declared in EXPLICIT_DECISIONS:
        packet = by_edge[declared["semantic_edge_id"]]
        source = next(
            node
            for node in packet["source_nodes"]
            if node["node_id"] == declared["reviewed_valid_source_node_id"]
        )
        target = next(
            node
            for node in packet["target_nodes"]
            if node["node_id"] == declared["reviewed_valid_target_node_id"]
        )
        decisions.append(
            {
                **declared,
                "episode_id": packet["episode_id"],
                "gold_source_entity": packet["gold_source_entity"],
                "gold_target_entity": packet["gold_target_entity"],
                "source_before_value": packet["source_before_value"],
                "target_before_value": packet["target_before_value"],
                "hop_edge_units": packet["hop_edge_units"],
                "evidence_packet_sha256": index["packets"][packet["semantic_edge_id"]],
                "reviewed_source_text": source["text"],
                "reviewed_target_text": target["text"],
                "reviewed_source_session_index": source["session_index"],
                "reviewed_target_session_index": target["session_index"],
                "evidence_citations": {
                    "source_original_turn": source["source_span"],
                    "target_original_turn": target["source_span"],
                    "review_packet_sha256": index["packets"][packet["semantic_edge_id"]],
                },
                "earlier_valid_source_review": {
                    "judgment": "no",
                    "reviewed_session_indices": list(range(int(source["session_index"]))),
                    "evidence_reference": (
                        f"review_packets_v2/{packet['episode_id']}_all_extracted_nodes"
                    ),
                    "reason": (
                        "All extracted nodes from sessions before the reviewed source "
                        "were read; none states the gold source proposition."
                    ),
                },
            }
        )
    artifact = {
        "schema_version": "p1-same-session-manual-decisions-v2",
        "immutable": True,
        "reviewer_type": "AI-assisted manual review",
        "manifest_sha256": json.loads(
            (out / "validation_manifest.json").read_text(encoding="utf-8")
        )["manifest_sha256"],
        "review_packets_file_sha256": sha256_file(out / "review_packets_v2.json"),
        "review_packet_index_sha256": index["index_sha256"],
        "decisions": decisions,
    }
    artifact["decisions_sha256"] = canonical_sha256(artifact)
    return artifact


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
    artifact = build_decisions(args.out)
    path = args.out / "manual_decisions_v2.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != artifact:
        raise ValueError(f"immutable decision artifact differs: {path}")
    if not path.exists():
        atomic_write_json(path, artifact)
    print(f"decisions={len(artifact['decisions'])}")
    print(f"decisions_sha256={artifact['decisions_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
