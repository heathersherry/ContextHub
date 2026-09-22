from __future__ import annotations

import copy
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.memebench.run_same_session_manual_validation import (
    _recover_stale,
    _run_config,
    _successes,
    _bind_config,
    RUN_SOURCE_FILES,
    exclusive_run_lock,
    validation_account,
)
from integrations.memebench.finalize_same_session_analysis import (
    ANALYSIS_SOURCE_FILES,
    assert_formal_baseline,
)
from integrations.memebench.gold_edge_audit import canonical_sha256, read_jsonl_tolerant
from integrations.memebench.prepare_same_session_manual_decisions import _decision
from integrations.memebench.same_session_manual_validation import (
    SCHEMA_VERSION,
    build_checkpoint_identities,
    prepare_validation_manifest,
    summarize_manual,
    validate_analysis_bundle,
    validate_manual_record,
    validate_selection_membership,
)
from integrations.memebench.write_same_session_manual_labels import materialize


ROOT = Path(__file__).resolve().parents[1]
DATA = Path("/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json")
OLD_AUDIT = ROOT / "integrations/memebench/runs/p1_gold_edge_audit_20260822"
FORMAL = ROOT / "integrations/memebench/runs/chronological_20260820/formal"
OUT = ROOT / "integrations/memebench/runs/p1_same_session_manual_validation_20260823"


def _manual(edge: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "semantic_edge_id": edge["semantic_edge_id"],
        "episode_id": edge["episode_id"],
        "gold_source_entity": edge["gold_source_entity"],
        "gold_target_entity": edge["gold_target_entity"],
        "source_before_value": edge["source_before_value"],
        "target_before_value": edge["target_before_value"],
        "hop_edge_units": edge["hop_edge_units"],
        "original_turn_quotes": [{"session_id": "s", "turn_index": 0, "quote": "q"}],
        "source_node_matches": [{"node_id": "s", "text": "source"}],
        "target_node_matches": [{"node_id": "t", "text": "target"}],
        "source_node_semantically_valid": "yes",
        "target_node_semantically_valid": "yes",
        "distinct_propositions": "yes",
        "same_session_confirmed": "yes",
        "earlier_valid_source_exists": "no",
        "boundary_causal_supported": "yes",
        "evidence": "reviewed source and target text against original turn",
        "reason": "both propositions are explicit and no earlier source exists",
    }


@pytest.fixture(scope="module")
def manifest():
    return prepare_validation_manifest(data=DATA, old_audit=OLD_AUDIT, formal=FORMAL)


def test_manifest_freezes_exact_selection_only_edge_set(manifest) -> None:
    assert manifest["selection_only"] is True
    assert manifest["policy"] == "R_full_cheap"
    assert manifest["episode_count"] == 9
    assert manifest["semantic_edge_count"] == 13
    assert len(manifest["relation_types"]) == 5
    assert all(
        len(unit["original_audit_record_sha256"]) == 64
        and len(unit["selection_split_hash"]) == 64
        for edge in manifest["semantic_edges"]
        for unit in edge["hop_edge_units"]
    )
    assert not any(
        unit["case_key"].endswith("R_full_verify")
        for edge in manifest["semantic_edges"]
        for unit in edge["hop_edge_units"]
    )


def test_manifest_is_deterministic_and_detects_old_record_change(manifest, tmp_path) -> None:
    again = prepare_validation_manifest(data=DATA, old_audit=OLD_AUDIT, formal=FORMAL)
    assert again == manifest
    copied = tmp_path / "audit"
    copied.mkdir()
    for name in ("audit_manifest.json", "gold_edge_audit.jsonl", "gold_edge_audit_reclassified.jsonl"):
        (copied / name).write_bytes((OLD_AUDIT / name).read_bytes())
    with (copied / "gold_edge_audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{}\n")
    with pytest.raises((KeyError, ValueError)):
        prepare_validation_manifest(data=DATA, old_audit=copied, formal=FORMAL)


def test_manual_schema_and_boundary_prerequisites(manifest) -> None:
    record = _manual(manifest["semantic_edges"][0])
    validate_manual_record(record)
    record["earlier_valid_source_exists"] = "yes"
    with pytest.raises(ValueError, match="prerequisite"):
        validate_manual_record(record)
    record["boundary_causal_supported"] = "no"
    validate_manual_record(record)
    record["same_session_confirmed"] = "maybe"
    with pytest.raises(ValueError, match="yes/no/ambiguous"):
        validate_manual_record(record)


def test_summary_is_reproducible_and_requires_all_edges(manifest) -> None:
    records = [_manual(edge) for edge in manifest["semantic_edges"]]
    assert summarize_manual(records, manifest) == summarize_manual(records, manifest)
    with pytest.raises(ValueError, match="edge set mismatch"):
        summarize_manual(records[:-1], manifest)


def test_checkpoint_requires_evidence_cleanup_and_terminal_success(tmp_path) -> None:
    config = "cfg"
    started = {
        "event": "attempt_started",
        "case_key": "case",
        "attempt_id": "a",
        "attempt_number": 1,
        "run_config_hash": config,
    }
    (tmp_path / "attempts.jsonl").write_text(json.dumps(started) + "\n", encoding="utf-8")
    assert _recover_stale(tmp_path, config) == 1
    assert _successes(tmp_path, config) == set()
    terminal = {
        **started,
        "event": "attempt_finished",
        "status": "success",
    }
    with (tmp_path / "attempts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(terminal) + "\n")
    (tmp_path / "case_evidence.jsonl").write_text(
        json.dumps({"case_key": "case", "attempt_id": "a", "run_config_hash": config}) + "\n",
        encoding="utf-8",
    )
    assert _successes(tmp_path, config) == set()
    (tmp_path / "case_success.jsonl").write_text(
        json.dumps(
            {
                "case_key": "case",
                "attempt_id": "a",
                "run_config_hash": config,
                "account_cleanup_complete": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert _successes(tmp_path, config) == {"case"}


def test_validation_account_is_new_namespace() -> None:
    account = validation_account("pl_001")
    assert account.startswith("p1sameval-v1-")
    assert not account.startswith(("p1audit-v1-", "ch1", "ch2"))


def _bundle():
    return {
        "manifest": json.loads((OUT / "validation_manifest.json").read_text()),
        "evidence_records": read_jsonl_tolerant(OUT / "case_evidence.jsonl"),
        "packets": json.loads((OUT / "review_packets_v2.json").read_text()),
        "packet_index": json.loads((OUT / "review_packet_index_v2.json").read_text()),
        "decisions_artifact": json.loads((OUT / "manual_decisions_v2.json").read_text()),
        "manual_records": read_jsonl_tolerant(OUT / "manual_validation_v2.jsonl"),
    }


def test_v2_bundle_binds_all_structural_prerequisites() -> None:
    assert validate_analysis_bundle(**_bundle()) == {
        "unique_episode_ids": 9,
        "semantic_edges": 13,
        "hop_edge_units": 22,
    }


def test_writer_uses_per_edge_decision_instead_of_default_labels(tmp_path) -> None:
    (tmp_path / "review_packets_v2.json").write_bytes(
        (OUT / "review_packets_v2.json").read_bytes()
    )
    artifact = json.loads((OUT / "manual_decisions_v2.json").read_text())
    artifact["decisions"][0]["boundary_causal_supported"] = "no"
    artifact["decisions_sha256"] = canonical_sha256(
        artifact, exclude_fields=("decisions_sha256",)
    )
    (tmp_path / "manual_decisions_v2.json").write_text(json.dumps(artifact))
    assert materialize(tmp_path)[0]["boundary_causal_supported"] == "no"
    with pytest.raises(TypeError):
        _decision("edge", "source", "target", "reason")


def _reseal(bundle: dict, edge_index: int) -> None:
    packet = bundle["packets"][edge_index]
    edge_id = packet["semantic_edge_id"]
    packet_hash = canonical_sha256(packet)
    bundle["packet_index"]["packets"][edge_id] = packet_hash
    bundle["packet_index"]["index_sha256"] = canonical_sha256(
        bundle["packet_index"], exclude_fields=("index_sha256",)
    )
    decision = bundle["decisions_artifact"]["decisions"][edge_index]
    decision["evidence_packet_sha256"] = packet_hash
    decision["evidence_citations"]["review_packet_sha256"] = packet_hash
    bundle["manual_records"][edge_index]["evidence_packet_sha256"] = packet_hash
    bundle["manual_records"][edge_index]["manual_decision_sha256"] = canonical_sha256(
        decision
    )
    bundle["decisions_artifact"]["decisions_sha256"] = canonical_sha256(
        bundle["decisions_artifact"], exclude_fields=("decisions_sha256",)
    )


def test_v2_rejects_packet_hash_and_reviewed_node_tampering() -> None:
    bundle = _bundle()
    bundle["packets"][0]["source_nodes"][0]["text"] += " tampered"
    with pytest.raises(ValueError, match="packet hash"):
        validate_analysis_bundle(**bundle)

    bundle = _bundle()
    bundle["decisions_artifact"]["decisions"][0]["reviewed_source_text"] = "wrong"
    bundle["decisions_artifact"]["decisions_sha256"] = canonical_sha256(
        bundle["decisions_artifact"], exclude_fields=("decisions_sha256",)
    )
    with pytest.raises(ValueError, match="reviewed node text"):
        validate_analysis_bundle(**bundle)


def test_v2_rejects_same_node_and_source_in_target_snapshot() -> None:
    bundle = _bundle()
    decision = bundle["decisions_artifact"]["decisions"][0]
    decision["reviewed_valid_source_node_id"] = decision[
        "reviewed_valid_target_node_id"
    ]
    bundle["decisions_artifact"]["decisions_sha256"] = canonical_sha256(
        bundle["decisions_artifact"], exclude_fields=("decisions_sha256",)
    )
    with pytest.raises(ValueError, match="identical|not in evidence"):
        validate_analysis_bundle(**bundle)

    bundle = _bundle()
    source_id = bundle["decisions_artifact"]["decisions"][0][
        "reviewed_valid_source_node_id"
    ]
    bundle["packets"][0]["target_candidate_traces"][0][
        "candidate_snapshot_ids"
    ].append(source_id)
    _reseal(bundle, 0)
    with pytest.raises(ValueError, match="already in target snapshot"):
        validate_analysis_bundle(**bundle)


def test_selection_cleanup_checkpoint_and_full_fingerprint_are_bound() -> None:
    bundle = _bundle()
    validate_selection_membership(bundle["manifest"])
    bad = copy.deepcopy(bundle["manifest"])
    bad["semantic_edges"][0]["hop_edge_units"][0]["selection_split_hash"] = "0" * 64
    with pytest.raises(ValueError, match="split hash"):
        validate_selection_membership(bad)
    checkpoint = build_checkpoint_identities(
        bundle["manifest"], bundle["evidence_records"]
    )
    assert len(checkpoint["cases"]) == 9
    assert all(len(row["gold_edge_ids_sha256"]) == 64 for row in checkpoint["cases"])
    analysis = json.loads((OUT / "analysis_config_v2.json").read_text())
    assert analysis["input_file_sha256"]["account_cleanup_verification.json"]
    assert set(ANALYSIS_SOURCE_FILES) == set(analysis["source_hashes"])
    assert {
        "src/contexthub/db/repository.py",
        "src/contexthub/llm/chat_client.py",
        "src/contexthub/llm/openai_client.py",
    } <= set(analysis["source_hashes"])


def test_future_run_fingerprint_covers_dependencies_and_rejects_change(
    tmp_path,
) -> None:
    manifest = json.loads((OUT / "validation_manifest.json").read_text())
    args = SimpleNamespace(
        chat_model="gpt-4.1-mini",
        extract_model="gpt-4.1-mini",
        p1_cheap_model="gpt-4o-mini",
        p1_strong_model="gpt-4.1-mini",
        embedding_provider="aliyun",
        embedding_model="text-embedding-v4",
        provider="openlux",
        case_timeout=1800.0,
        max_attempts=5,
    )
    config = _run_config(args, manifest)
    assert set(config["source_hashes"]) == set(RUN_SOURCE_FILES)
    assert config["prompt_hashes"]
    assert config["provider_config_sha256"]
    _bind_config(tmp_path, config)
    changed = copy.deepcopy(config)
    changed["provider_config_sha256"] = "0" * 64
    changed["run_config_hash"] = canonical_sha256(
        changed, exclude_fields=("run_config_hash",)
    )
    with pytest.raises(RuntimeError, match="run config hash mismatch"):
        _bind_config(tmp_path, changed)


def test_live_attempt_and_exclusive_lock_are_not_recovered(tmp_path) -> None:
    started = {
        "event": "attempt_started",
        "case_key": "case",
        "attempt_id": "live",
        "timestamp": 0,
        "heartbeat_timestamp": 0,
        "owner_pid": os.getpid(),
        "owner_host": socket.gethostname(),
        "run_config_hash": "cfg",
    }
    (tmp_path / "attempts.jsonl").write_text(json.dumps(started) + "\n")
    assert _recover_stale(tmp_path, "cfg", stale_after=10**12) == 0
    with exclusive_run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="another same-session runner"):
            with exclusive_run_lock(tmp_path):
                pass


def test_formal_baseline_manifest_hash_cannot_be_synchronized_away(tmp_path) -> None:
    manifest = json.loads((OUT / "validation_manifest.json").read_text())
    for name in (
        "formal_artifact_hashes_before.json",
        "formal_artifact_hashes_after.json",
    ):
        (tmp_path / name).write_bytes((OUT / name).read_bytes())
    assert_formal_baseline(tmp_path, manifest, FORMAL)
    before = json.loads((tmp_path / "formal_artifact_hashes_before.json").read_text())
    before["invented"] = "0" * 64
    (tmp_path / "formal_artifact_hashes_before.json").write_text(json.dumps(before))
    with pytest.raises(ValueError, match="validation manifest"):
        assert_formal_baseline(tmp_path, manifest, FORMAL)
