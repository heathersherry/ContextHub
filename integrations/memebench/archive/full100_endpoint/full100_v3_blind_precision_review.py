"""Offline blind review and precision analysis for MEME full100 v3.

This module never calls a model or external API.  Blinding reads only the
frozen review packet and gold-free shared episode manifests.  Unblinding is
refused until a complete, hash-bound decision artifact has been frozen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from integrations.memebench.gold_edge_audit import canonical_sha256, sha256_file


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
SOURCE_RUN = RUNS / "p1_full100_candidate_envelope_reevaluation_20260824_v2"
SHARED = RUNS / "p1_same_session_selector_full100_shared_20260824"
DEFAULT_OUT = RUNS / "p1_full100_v3_blind_precision_review_20260824_v1"
DEFAULT_DECISIONS = (
    ROOT
    / "integrations"
    / "memebench"
    / "adjudications"
    / "p1_full100_v3_blind_edge_decisions_v1.json"
)
BLIND_SEED = "p1-full100-v3-semantic-edge-blind-order-v1"
BOOTSTRAP_SEED = 20260824
BOOTSTRAP_REPLICATES = 20_000
LABELS = frozenset({"yes", "no", "ambiguous"})
CONFIDENCE = frozenset({"high", "medium", "low"})
EXPECTED_PACKET_FILE_SHA256 = (
    "8ce79ab75cee3b9dc25d0b0f298a3d03254530205ed6aa62bd48bbf5f60fbd02"
)
FORBIDDEN_BLIND_KEYS = frozenset(
    {
        "stratum",
        "arm",
        "old_arm",
        "new_arm",
        "gold",
        "matched_gold_edge_identities",
        "source_origin",
        "edge_id",
        "episode_id",
        "source_evidence_id",
        "target_evidence_id",
        "source_node_ids",
        "target_node_ids",
        "source_entities",
        "target_entities",
        "approximately_matched",
    }
)


class ReviewError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewError(f"{path} must contain a JSON object")
    return value


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise ReviewError(f"immutable artifact already differs: {path}")
        return
    path.write_text(payload, encoding="utf-8")


def _verify_embedded_hash(
    value: Mapping[str, Any], field: str, *, expected: str | None = None
) -> str:
    observed = str(value.get(field) or "")
    payload = {key: item for key, item in value.items() if key != field}
    actual = canonical_sha256(payload)
    if observed != actual or (expected is not None and observed != expected):
        raise ReviewError(f"{field} mismatch: embedded={observed}, actual={actual}")
    return actual


def verify_source_packet(packet_path: Path) -> dict[str, Any]:
    packet = _json(packet_path)
    _verify_embedded_hash(
        packet,
        "packet_sha256",
        expected="0fe221255a71ad46a09c17163a8c7ba108536b3ca031738b3d7b0ada302245cf",
    )
    if sha256_file(packet_path) != EXPECTED_PACKET_FILE_SHA256:
        raise ReviewError("review packet byte hash changed")
    expected_population = {
        "common_unmatched": 489,
        "newly_added": 282,
        "removed_old": 164,
    }
    if packet.get("population_counts") != expected_population:
        raise ReviewError("population counts changed")
    samples = packet.get("samples")
    if not isinstance(samples, Mapping) or set(samples) != set(expected_population):
        raise ReviewError("unexpected packet strata")
    all_edges: list[str] = []
    for stratum, rows in samples.items():
        if not isinstance(rows, list) or len(rows) != 30:
            raise ReviewError(f"{stratum} must contain exactly 30 samples")
        episode_counts = Counter(str(row["episode_id"]) for row in rows)
        if max(episode_counts.values(), default=0) > 2:
            raise ReviewError(f"{stratum} exceeds per-episode cap")
        edge_ids = [str(row["edge_id"]) for row in rows]
        if len(edge_ids) != len(set(edge_ids)):
            raise ReviewError(f"{stratum} contains duplicate edges")
        all_edges.extend(edge_ids)
    if len(all_edges) != len(set(all_edges)):
        raise ReviewError("packet contains cross-stratum duplicate edges")
    return packet


def _shared_episode_paths() -> dict[str, Path]:
    index_path = SHARED / "shared_manifest_index.json"
    index = _json(index_path)
    paths: dict[str, Path] = {}
    for row in index["episodes"]:
        path = SHARED / str(row["manifest_path"])
        if sha256_file(path) != row["manifest_file_sha256"]:
            raise ReviewError(f"shared episode hash mismatch: {row['episode_id']}")
        paths[str(row["episode_id"])] = path
    if len(paths) != 100:
        raise ReviewError("shared manifest must bind 100 episodes")
    return paths


def _node_context(
    episode: Mapping[str, Any], node_ids: Sequence[str]
) -> dict[str, Any]:
    wanted = set(map(str, node_ids))
    nodes = {
        str(row["node_id"]): row
        for row in episode.get("nodes", ())
        if str(row["node_id"]) in wanted
    }
    alignments = {
        str(row["node_id"]): row
        for row in episode.get("alignments", ())
        if str(row["node_id"]) in wanted
    }
    if set(nodes) != wanted:
        raise ReviewError(f"shared nodes missing: {sorted(wanted - set(nodes))}")
    sessions = sorted({int(row["session_index"]) for row in nodes.values()})
    turns = sorted(
        {
            int(row["turn_index"])
            for row in alignments.values()
            if row.get("turn_index") is not None
        }
    )
    quotes = sorted(
        {
            str(row["quote"]).strip()
            for row in alignments.values()
            if str(row.get("quote") or "").strip()
        }
    )
    return {"sessions": sessions, "turns": turns, "quotes": quotes}


def _temporal(source: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    source_session = min(source["sessions"])
    target_session = min(target["sessions"])
    source_turn = min(source["turns"]) if source["turns"] else None
    target_turn = min(target["turns"]) if target["turns"] else None
    if source_session < target_session:
        relation = "source_in_earlier_session"
    elif source_session > target_session:
        relation = "source_in_later_session"
    elif source_turn is None or target_turn is None:
        relation = "same_session_turn_order_unresolved"
    elif source_turn < target_turn:
        relation = "source_earlier_in_same_session"
    elif source_turn == target_turn:
        relation = "same_turn"
    else:
        relation = "source_later_in_same_session"
    return {
        "relation": relation,
        "session_distance": target_session - source_session,
        "turn_distance_if_same_session": (
            target_turn - source_turn
            if source_session == target_session
            and source_turn is not None
            and target_turn is not None
            else None
        ),
    }


def build_blinded_artifacts(
    packet_path: Path, out: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet = verify_source_packet(packet_path)
    episode_paths = _shared_episode_paths()
    episodes: dict[str, dict[str, Any]] = {}
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for stratum, rows in packet["samples"].items():
        candidates.extend((str(stratum), row) for row in rows)
    candidates.sort(
        key=lambda item: canonical_sha256(
            {"seed": BLIND_SEED, "edge_id": item[1]["edge_id"]}
        )
    )
    blinded_rows = []
    sealed_rows = []
    used_shared: dict[str, str] = {}
    for index, (stratum, row) in enumerate(candidates, start=1):
        episode_id = str(row["episode_id"])
        if episode_id not in episodes:
            episodes[episode_id] = _json(episode_paths[episode_id])
            used_shared[episode_id] = sha256_file(episode_paths[episode_id])
        episode = episodes[episode_id]
        source = _node_context(episode, row["source_node_ids"])
        target = _node_context(episode, row["target_node_ids"])
        blind_id = f"BR-{index:03d}-{hashlib.sha256((BLIND_SEED + str(row['edge_id'])).encode()).hexdigest()[:10]}"
        blinded_rows.append(
            {
                "blind_review_id": blind_id,
                "source_text": row["source_text"],
                "target_text": row["target_text"],
                "source_original_turn_excerpt": source["quotes"],
                "target_original_turn_excerpt": target["quotes"],
                "temporal_information": _temporal(source, target),
            }
        )
        sealed_rows.append(
            {
                "blind_review_id": blind_id,
                "edge_id": row["edge_id"],
                "episode_id": episode_id,
                "stratum": stratum,
                "approximately_matched": bool(row["matched_gold_edge_identities"]),
            }
        )
    blinded: dict[str, Any] = {
        "schema_version": "p1-full100-v3-semantic-edge-blinded-packet-v1",
        "status": "ready_for_blind_review",
        "order_seed": BLIND_SEED,
        "sample_count": len(blinded_rows),
        "review_instructions": {
            "labels": ["yes", "no", "ambiguous"],
            "yes": (
                "source is an explicit semantic prerequisite, cause, constraint, "
                "or derivation input for target; changing source could change target"
            ),
            "no": (
                "topic/entity/time/wording overlap or old-to-new replacement without "
                "the required derivational dependency"
            ),
            "ambiguous": "available evidence cannot reliably distinguish yes from no",
        },
        "rows": blinded_rows,
        "source_packet_file_sha256": sha256_file(packet_path),
        "source_packet_canonical_sha256": packet["packet_sha256"],
        "shared_episode_file_sha256": used_shared,
    }
    blinded["blinded_packet_sha256"] = canonical_sha256(blinded)
    sealed: dict[str, Any] = {
        "schema_version": "p1-full100-v3-semantic-edge-sealed-map-v1",
        "status": "sealed_until_decisions_frozen",
        "blinded_packet_sha256": blinded["blinded_packet_sha256"],
        "rows": sealed_rows,
    }
    sealed["sealed_map_sha256"] = canonical_sha256(sealed)
    serialized = json.dumps(blinded, sort_keys=True)
    if any(f'"{key}"' in serialized for key in FORBIDDEN_BLIND_KEYS):
        raise ReviewError("blinded packet leaks a forbidden key")
    _write_new(out / "blinded_review_packet.json", blinded)
    _write_new(out / "sealed_unblinding_map.json", sealed)
    return blinded, sealed


def validate_decisions(
    blinded: Mapping[str, Any], decisions: Mapping[str, Any]
) -> list[dict[str, Any]]:
    _verify_embedded_hash(blinded, "blinded_packet_sha256")
    if decisions.get("status") != "frozen_complete":
        raise ReviewError("decisions must be frozen_complete before unblinding")
    if decisions.get("blinded_packet_sha256") != blinded["blinded_packet_sha256"]:
        raise ReviewError("decision artifact is bound to another blinded packet")
    rows = decisions.get("decisions")
    if not isinstance(rows, list):
        raise ReviewError("decisions must be a list")
    expected = {str(row["blind_review_id"]) for row in blinded["rows"]}
    observed = [str(row.get("blind_review_id")) for row in rows]
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise ReviewError("decisions are incomplete, duplicated, or contain unknown IDs")
    for row in rows:
        if row.get("label") not in LABELS:
            raise ReviewError(f"invalid label: {row.get('label')}")
        if row.get("confidence") not in CONFIDENCE:
            raise ReviewError(f"invalid confidence: {row.get('confidence')}")
        if not isinstance(row.get("needs_more_context"), bool):
            raise ReviewError("needs_more_context must be boolean")
        if not str(row.get("rationale") or "").strip():
            raise ReviewError("every decision requires a rationale")
    expected_hash = canonical_sha256(rows)
    if decisions.get("decisions_canonical_sha256") != expected_hash:
        raise ReviewError("decision rows hash mismatch")
    _verify_embedded_hash(decisions, "decision_artifact_sha256")
    return [dict(row) for row in rows]


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total <= 0 or successes < 0 or successes > total:
        raise ReviewError("invalid Wilson inputs")
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
        / denominator
    )
    lower = 0.0 if successes == 0 else max(0.0, center - margin)
    upper = 1.0 if successes == total else min(1.0, center + margin)
    return [lower, upper]


def _label_value(label: str, ambiguous_as_yes: bool) -> int:
    return int(label == "yes" or (label == "ambiguous" and ambiguous_as_yes))


def _cluster_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    *,
    ambiguous_as_yes: bool,
    seed: int,
    replicates: int,
) -> list[float]:
    clusters: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        clusters[str(row["episode_id"])].append(
            _label_value(str(row["label"]), ambiguous_as_yes)
        )
    keys = sorted(clusters)
    if not keys:
        raise ReviewError("cluster bootstrap requires observations")
    rng = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        selected = [rng.choice(keys) for _ in keys]
        values = [value for key in selected for value in clusters[key]]
        estimates.append(sum(values) / len(values))
    estimates.sort()
    return [
        estimates[int(0.025 * (replicates - 1))],
        estimates[int(0.975 * (replicates - 1))],
    ]


def _rate_summary(
    rows: Sequence[Mapping[str, Any]], *, seed_offset: int = 0
) -> dict[str, Any]:
    counts = Counter(str(row["label"]) for row in rows)
    total = len(rows)
    lower_success = counts["yes"]
    upper_success = counts["yes"] + counts["ambiguous"]
    return {
        "n": total,
        "counts": {label: counts[label] for label in ("yes", "no", "ambiguous")},
        "lower_bound_ambiguous_as_no": {
            "point": lower_success / total,
            "wilson95": wilson_interval(lower_success, total),
            "episode_cluster_bootstrap95_exploratory": _cluster_bootstrap(
                rows,
                ambiguous_as_yes=False,
                seed=BOOTSTRAP_SEED + seed_offset,
                replicates=BOOTSTRAP_REPLICATES,
            ),
        },
        "upper_bound_ambiguous_as_yes": {
            "point": upper_success / total,
            "wilson95": wilson_interval(upper_success, total),
            "episode_cluster_bootstrap95_exploratory": _cluster_bootstrap(
                rows,
                ambiguous_as_yes=True,
                seed=BOOTSTRAP_SEED + seed_offset,
                replicates=BOOTSTRAP_REPLICATES,
            ),
        },
    }


def _weighted(
    parts: Sequence[tuple[int, Mapping[str, Any]]], bound: str
) -> dict[str, Any]:
    denominator = sum(weight for weight, _ in parts)
    point = sum(weight * float(summary[bound]["point"]) for weight, summary in parts)
    lower = sum(
        weight * float(summary[bound]["wilson95"][0]) for weight, summary in parts
    )
    upper = sum(
        weight * float(summary[bound]["wilson95"][1]) for weight, summary in parts
    )
    return {
        "population_n": denominator,
        "point": point / denominator,
        "conservative_weighted_wilson95": [lower / denominator, upper / denominator],
    }


def analyze(
    blinded_path: Path,
    sealed_path: Path,
    decisions_path: Path,
    out: Path,
) -> dict[str, Any]:
    blinded = _json(blinded_path)
    decisions = _json(decisions_path)
    decision_rows = validate_decisions(blinded, decisions)
    sealed = _json(sealed_path)
    _verify_embedded_hash(sealed, "sealed_map_sha256")
    if sealed.get("blinded_packet_sha256") != blinded["blinded_packet_sha256"]:
        raise ReviewError("sealed map is bound to another blinded packet")
    decision_by_id = {row["blind_review_id"]: row for row in decision_rows}
    unblinded = [
        {**row, **decision_by_id[row["blind_review_id"]]} for row in sealed["rows"]
    ]
    by_stratum = {
        name: [row for row in unblinded if row["stratum"] == name]
        for name in ("common_unmatched", "newly_added", "removed_old")
    }
    strata = {
        name: _rate_summary(rows, seed_offset=index * 1000)
        for index, (name, rows) in enumerate(by_stratum.items(), start=1)
    }
    added_unmatched = [
        row for row in by_stratum["newly_added"] if not row["approximately_matched"]
    ]
    removed_unmatched = [
        row for row in by_stratum["removed_old"] if not row["approximately_matched"]
    ]
    unmatched_subsets = {
        "newly_added_unmatched": _rate_summary(added_unmatched, seed_offset=4000),
        "removed_old_unmatched": _rate_summary(removed_unmatched, seed_offset=5000),
    }
    population = {
        "common_unmatched": 489,
        "newly_added": 282,
        "removed_old": 164,
        "newly_added_unmatched": 228,
        "removed_old_unmatched": 135,
        "new_unmatched": 717,
        "old_unmatched": 624,
    }
    bounds = (
        "lower_bound_ambiguous_as_no",
        "upper_bound_ambiguous_as_yes",
    )
    unmatched = {"new": {}, "old": {}}
    for bound in bounds:
        unmatched["new"][bound] = _weighted(
            [
                (489, strata["common_unmatched"]),
                (228, unmatched_subsets["newly_added_unmatched"]),
            ],
            bound,
        )
        unmatched["old"][bound] = _weighted(
            [
                (489, strata["common_unmatched"]),
                (135, unmatched_subsets["removed_old_unmatched"]),
            ],
            bound,
        )
    incremental = {}
    for bound in bounds:
        added = strata["newly_added"][bound]
        removed = strata["removed_old"][bound]
        true_point = 282 * added["point"] - 164 * removed["point"]
        false_point = 282 * (1 - added["point"]) - 164 * (1 - removed["point"])
        incremental[bound] = {
            "expected_net_true_edges_point": true_point,
            "expected_net_false_edges_point": false_point,
            "expected_net_true_edges_conservative_wilson_range": [
                282 * added["wilson95"][0] - 164 * removed["wilson95"][1],
                282 * added["wilson95"][1] - 164 * removed["wilson95"][0],
            ],
            "expected_net_false_edges_conservative_wilson_range": [
                282 * (1 - added["wilson95"][1])
                - 164 * (1 - removed["wilson95"][0]),
                282 * (1 - added["wilson95"][0])
                - 164 * (1 - removed["wilson95"][1]),
            ],
        }
    result: dict[str, Any] = {
        "schema_version": "p1-full100-v3-blind-precision-analysis-v1",
        "reviewer": {
            "type": "single_AI_assisted_offline_reviewer",
            "is_human_gold": False,
            "is_formal_population_truth": False,
        },
        "bindings": {
            "blinded_packet_sha256": blinded["blinded_packet_sha256"],
            "sealed_map_sha256": sealed["sealed_map_sha256"],
            "decision_artifact_sha256": decisions["decision_artifact_sha256"],
            "decisions_canonical_sha256": decisions["decisions_canonical_sha256"],
        },
        "statistics_config": {
            "wilson_confidence": 0.95,
            "ambiguous_bounds": {
                "lower": "ambiguous_as_no",
                "upper": "ambiguous_as_yes",
            },
            "cluster_bootstrap": {
                "unit": "episode",
                "seed": BOOTSTRAP_SEED,
                "replicates": BOOTSTRAP_REPLICATES,
                "status": "exploratory",
            },
            "weighted_interval_note": (
                "weighted Wilson endpoints are conservative component-wise bounds, "
                "not a joint model-based confidence interval"
            ),
        },
        "population_counts": population,
        "strata": strata,
        "unmatched_conditioned_subsets": unmatched_subsets,
        "weighted_unmatched_precision": unmatched,
        "incremental_edge_change": incremental,
    }
    result["analysis_canonical_sha256"] = canonical_sha256(result)
    _write_new(out / "precision_analysis.json", result)
    return result


def _protected_hashes() -> dict[str, str]:
    paths = [
        SOURCE_RUN / "review_packet.json",
        SOURCE_RUN / "summary.json",
        SOURCE_RUN / "metrics.json",
        SOURCE_RUN / "paired_comparison.json",
        SOURCE_RUN / "input_hashes_before.json",
        SOURCE_RUN / "input_hashes_after.json",
        SHARED / "shared_manifest_index.json",
    ]
    paths.extend(_shared_episode_paths().values())
    return {str(path): sha256_file(path) for path in sorted(paths)}


def write_manifest(
    out: Path, decisions_path: Path, protected_before: Mapping[str, str]
) -> dict[str, Any]:
    after = _protected_hashes()
    if dict(protected_before) != after:
        raise ReviewError("frozen inputs changed during analysis")
    files = [
        out / "blinded_review_packet.json",
        out / "sealed_unblinding_map.json",
        out / "precision_analysis.json",
        decisions_path,
    ]
    code_path = Path(__file__).resolve()
    manifest: dict[str, Any] = {
        "schema_version": "p1-full100-v3-blind-precision-manifest-v1",
        "zero_external_model_or_api_calls": True,
        "zero_paid_cost_usd": 0.0,
        "frozen_inputs_before": dict(protected_before),
        "frozen_inputs_after": after,
        "code_fingerprint": {str(code_path): sha256_file(code_path)},
        "statistics_config": {
            "bootstrap_seed": BOOTSTRAP_SEED,
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "wilson_confidence": 0.95,
        },
        "output_file_sha256": {str(path): sha256_file(path) for path in files},
    }
    manifest["manifest_canonical_sha256"] = canonical_sha256(manifest)
    _write_new(out / "manifest.json", manifest)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("blind", "analyze"))
    parser.add_argument("--packet", type=Path, default=SOURCE_RUN / "review_packet.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    args = parser.parse_args(argv)
    before = _protected_hashes()
    if args.command == "blind":
        blinded, sealed = build_blinded_artifacts(args.packet, args.out)
        result = {
            "blinded_packet_sha256": blinded["blinded_packet_sha256"],
            "sealed_map_sha256": sealed["sealed_map_sha256"],
            "sample_count": blinded["sample_count"],
        }
    else:
        result = analyze(
            args.out / "blinded_review_packet.json",
            args.out / "sealed_unblinding_map.json",
            args.decisions,
            args.out,
        )
        write_manifest(args.out, args.decisions, before)
    if before != _protected_hashes():
        raise ReviewError("frozen inputs changed")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
