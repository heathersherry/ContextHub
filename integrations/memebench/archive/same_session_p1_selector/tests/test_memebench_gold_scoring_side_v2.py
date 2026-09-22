from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.memebench.build_gold_scoring_side_v2 import (
    DEFAULT_RUN,
    MANIFEST_NAME,
    RULE_VERSION,
    SCHEMA_VERSION,
    SIDECAR_NAME,
    ScoringSidecarError,
    _mapping_rows,
    gold_edge_identity_v1,
    validate_sidecar,
)
from integrations.memebench.gold_edge_audit import canonical_sha256, sha256_file
from integrations.memebench.same_session_selector_intervention import build_selector_cases


def test_gold_edge_v1_hash_round_trip():
    expected = canonical_sha256({"source": "a", "target": "b", "hop": 2})
    assert gold_edge_identity_v1("a", "b", 2) == expected


def test_mapping_is_many_to_many_and_marks_ambiguity():
    frozen = {
        "alignments": [
            {
                "node_id": "n1",
                "evidence_id": "e1",
                "text": "The code is abc; the backup remains abc.",
                "status": "aligned",
                "method": "exact",
            },
            {
                "node_id": "n2",
                "evidence_id": None,
                "text": "unrelated",
                "status": "ambiguous",
                "method": "ambiguous",
            },
        ]
    }
    case = SimpleNamespace(
        entities={
            "primary": SimpleNamespace(before="abc"),
            "backup": SimpleNamespace(before="abc"),
        }
    )
    nodes, evidence, stats = _mapping_rows(frozen, case)
    assert nodes[0]["matched_entity_count"] == 2
    assert nodes[0]["entity_mapping_ambiguous"]
    assert nodes[1]["unmapped"] and nodes[1]["alignment_ambiguous"]
    assert evidence[0]["source_identity_ambiguity"]
    assert stats["node_entity_ambiguous_count"] == 1


def test_runtime_adapter_does_not_accept_scoring_sidecar():
    with pytest.raises(TypeError):
        build_selector_cases([], scoring_sidecar={"forbidden": True})


@pytest.mark.skipif(
    not (DEFAULT_RUN / SIDECAR_NAME).is_file(), reason="formal v2 sidecar absent"
)
def test_formal_v2_fields_hashes_and_completeness():
    sidecar = json.loads((DEFAULT_RUN / SIDECAR_NAME).read_text())
    manifest = json.loads((DEFAULT_RUN / MANIFEST_NAME).read_text())
    assert sidecar["schema_version"] == SCHEMA_VERSION
    assert sidecar["mapping_rule_version"] == RULE_VERSION
    assert sidecar["scoring_only"] and not sidecar["runtime_input"]
    assert sidecar["hop1_episode_count"] == 100
    assert sidecar["hop2_episode_count"] == 64
    assert len(sidecar["episodes"]) == 100
    assert sum(
        len(row["scoring_records"]) == 2 for row in sidecar["episodes"]
    ) == 64
    for episode in sidecar["episodes"]:
        assert episode["node_to_entity_mappings"]
        for record in episode["scoring_records"]:
            assert record["loader_edge_count"] == len(record["gold_edges"])
            for edge in record["gold_edges"]:
                assert {
                    "source_entity",
                    "target_entity",
                    "source_before_value",
                    "target_before_value",
                    "edge_hop",
                    "edge_pattern",
                } <= edge.keys()
                assert edge["gold_edge_identity_v1"] == gold_edge_identity_v1(
                    edge["source_entity"], edge["target_entity"], edge["edge_hop"]
                )
    assert manifest["sidecar_file_sha256"] == sha256_file(DEFAULT_RUN / SIDECAR_NAME)
    assert manifest["preexisting_files_unchanged"] is True
    assert manifest["preexisting_hash_map_sha256_before"] == manifest[
        "preexisting_hash_map_sha256_after"
    ]


@pytest.mark.skipif(
    not (DEFAULT_RUN / SIDECAR_NAME).is_file(), reason="formal v2 sidecar absent"
)
def test_tampered_sidecar_is_rejected():
    sidecar = json.loads((DEFAULT_RUN / SIDECAR_NAME).read_text())
    manifest = json.loads((DEFAULT_RUN / MANIFEST_NAME).read_text())
    tampered = copy.deepcopy(sidecar)
    tampered["episodes"][0]["episode_id"] = "tampered"
    data = Path(json.loads((DEFAULT_RUN / "config.json").read_text())["data_path"])
    with pytest.raises(ScoringSidecarError, match="canonical content hash"):
        validate_sidecar(tampered, manifest, run=DEFAULT_RUN, data=data)
