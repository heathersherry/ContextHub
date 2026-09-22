"""Offline, read-only cross-audit of the full100 cheap and verify selector arms."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from integrations.memebench.gold_edge_audit import (
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
CHEAP_RUN = RUNS / "p1_same_session_selector_full100_cheap_20260824"
VERIFY_RUN = RUNS / "p1_same_session_selector_full100_verify_20260824"
SHARED = RUNS / "p1_same_session_selector_full100_shared_20260824"
DEFAULT_OUT = RUNS / "p1_same_session_selector_full100_comparison_20260824"

EXPECTED_SHARED_CONTENT = "b36add2260393f14f0b24ba82162db5f83dda99b66c84c9226c107f87738cce5"
EXPECTED_SHARED_INDEX = "47241616abc53ead10517b26ba84a00b314c9e3c7cb7ca60ccb1dc965870dfad"
EXPECTED_V2_CANONICAL = "76f2eef111e573dab437aa1bf4a41f428a90998232c3881cd6c8ca35680afcea"
EXPECTED_V2_FILE = "15972d6a77b4624360134334aed4be0f6170bccf540595decf3aa0340f9d9f8d"
EXPECTED_V2_MANIFEST_CANONICAL = "0b2e83df9160846337cb6f1990d544e8bc6ea4a099522793f9d0566fdfaefcd1"
EXPECTED_V2_MANIFEST_FILE = "42676461d9033ba424271fb6a86d867b0c1b8c6270f97a9a6986b598602af68b"
EXPECTED_CONFIGS = {
    "cheap": "c38e4225a249830ceece5436763f099eef62ce55b2a740b7f5195ab3ca3be312",
    "verify": "048595f6c6d7ae82f2e178626d5aa9193e73383f200cf7a46a284d4eb95c6ee4",
}
ARMS = {"cheap": "turn_full_cheap", "verify": "turn_full_verify"}
MODELS = {
    "cheap": {"gpt-4o-mini"},
    "verify": {"gpt-4o-mini", "gpt-4.1-mini"},
}
PRICING = {
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4.1-mini": {"prompt": 0.40, "completion": 1.60},
}
MAPPING_RULE = "edge-pr-raw-normalized-before-substring-v1"
INPUT_FILES = {
    "cheap": CHEAP_RUN,
    "verify": VERIFY_RUN,
    "shared": SHARED,
}


class AuditError(RuntimeError):
    """Fail-closed validation error."""


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"{path} must contain a JSON object")
    return value


def _canonical_without(value: Mapping[str, Any], field: str) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != field})


def _tree_hashes() -> dict[str, str]:
    return {
        f"{label}/{path.relative_to(root)}": sha256_file(path)
        for label, root in INPUT_FILES.items()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * q))]


def distribution(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean": mean(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "total": sum(values),
    }


def compare_distributions(
    cheap: Mapping[str, float | None], verify: Mapping[str, float | None]
) -> dict[str, dict[str, float | None]]:
    result = {}
    for key in ("mean", "p50", "p95", "total"):
        left, right = cheap[key], verify[key]
        result[key] = {
            "cheap": left,
            "verify": right,
            "verify_minus_cheap": None if left is None or right is None else right - left,
            "verify_over_cheap": None if not left or right is None else right / left,
        }
    return result


def _mapping_entities(mapping: Mapping[str, Any]) -> set[str]:
    return {str(row["entity"]) for row in mapping.get("matched_entities", ())}


def _mapping_flags(mapping: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "unmapped": bool(mapping.get("unmapped")),
        "multi_entity": bool(
            mapping.get("entity_mapping_ambiguous")
            or int(mapping.get("matched_entity_count", 0)) > 1
        ),
        "alignment_ambiguous": bool(mapping.get("has_alignment_ambiguous_alias")),
        "reason_clause_or_source_identity_risk": bool(
            mapping.get("source_identity_ambiguity")
        ),
    }


def _load_shared() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    index = _json(SHARED / "shared_manifest_index.json")
    content = canonical_sha256(
        [
            {
                "episode_id": row["episode_id"],
                "manifest_file_sha256": row["manifest_file_sha256"],
            }
            for row in index["episodes"]
        ]
    )
    index_hash = _canonical_without(index, "index_sha256")
    if (
        content != EXPECTED_SHARED_CONTENT
        or index_hash != EXPECTED_SHARED_INDEX
        or index.get("manifest_content_sha256") != content
        or index.get("index_sha256") != index_hash
    ):
        raise AuditError("shared manifest hash mismatch")
    episodes = []
    for row in index["episodes"]:
        path = SHARED / row["manifest_path"]
        if sha256_file(path) != row["manifest_file_sha256"]:
            raise AuditError(f"shared shard hash mismatch: {row['episode_id']}")
        episode = _json(path)
        if episode.get("episode_manifest_sha256") != row["episode_manifest_sha256"]:
            raise AuditError(f"shared episode self-hash mismatch: {row['episode_id']}")
        episodes.append(episode)
    cases = [case for episode in episodes for case in episode["selector_cases"]]
    if (
        len(episodes) != 100
        or len(cases) != 2209
        or sum(bool(row["hop2_applicable"]) for row in index["episodes"]) != 64
    ):
        raise AuditError("shared completeness must be 100/64 episodes and 2209 targets")

    side_path = SHARED / "gold_scoring_side_v2.json"
    side_manifest_path = SHARED / "gold_scoring_side_v2_manifest.json"
    side = _json(side_path)
    side_manifest = _json(side_manifest_path)
    if (
        sha256_file(side_path) != EXPECTED_V2_FILE
        or _canonical_without(side, "canonical_content_sha256") != EXPECTED_V2_CANONICAL
        or sha256_file(side_manifest_path) != EXPECTED_V2_MANIFEST_FILE
        or _canonical_without(side_manifest, "manifest_sha256")
        != EXPECTED_V2_MANIFEST_CANONICAL
        or side.get("mapping_rule_version") != MAPPING_RULE
        or side_manifest.get("mapping_rule_version") != MAPPING_RULE
        or side.get("runtime_input") is not False
        or side.get("selector_adapter_input") is not False
    ):
        raise AuditError("v2 scoring sidecar hash/schema mismatch")
    hop_counts = {
        hop: sum(
            any(record["hop"] == hop for record in episode["scoring_records"])
            for episode in side["episodes"]
        )
        for hop in (1, 2)
    }
    if len(side["episodes"]) != 100 or hop_counts != {1: 100, 2: 64}:
        raise AuditError("v2 scoring completeness mismatch")
    return index, cases, side


def _expected_from_config(config: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = list(config.get("expected_cases", ()))
    if len(rows) != 2209:
        raise AuditError("run config does not freeze 2209 cases")
    result = {str(row["case_key"]): row for row in rows}
    if len(result) != 2209:
        raise AuditError("duplicate expected case key")
    return result


def _validate_run(
    label: str,
    path: Path,
    shared_cases: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = _json(path / "config.json")
    if (
        config.get("config_sha256") != EXPECTED_CONFIGS[label]
        or _canonical_without(config, "config_sha256") != EXPECTED_CONFIGS[label]
        or config.get("arm") != ARMS[label]
    ):
        raise AuditError(f"{label} config hash/arm mismatch")
    configured_models = (
        {str(config["model"])}
        if label == "cheap"
        else {str(value) for value in config["models"].values()}
    )
    if configured_models != MODELS[label]:
        raise AuditError(f"{label} configured models mismatch")
    expected = _expected_from_config(config)
    shared_by_target = {
        (str(case["episode_id"]), str(case["target_evidence_id"])): case
        for case in shared_cases
    }
    for row in expected.values():
        shared = shared_by_target.get(
            (str(row["episode_id"]), str(row["target_evidence_id"]))
        )
        mapping_hash = row.get("candidate_mapping_hash", row.get("candidate_mapping_sha256"))
        if (
            shared is None
            or row.get("input_hash") != shared["input_hash"]
            or row.get("candidate_identity_hash") != shared["candidate_identity_hash"]
            or mapping_hash != canonical_sha256(shared["candidates"])
        ):
            raise AuditError(f"{label} expected case differs from shared candidates")

    rows = read_jsonl_tolerant(path / "case_success.jsonl")
    if len(rows) != 2209:
        raise AuditError(f"{label} checkpoint line count is not 2209")
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("case_key"))
        frozen = expected.get(key)
        mapping_hash = row.get("candidate_mapping_hash", row.get("candidate_mapping_sha256"))
        frozen_mapping_hash = (
            frozen.get("candidate_mapping_hash", frozen.get("candidate_mapping_sha256"))
            if frozen
            else None
        )
        if (
            frozen is None
            or key in by_key
            or row.get("config_sha256") != config["config_sha256"]
            or row.get("arm") != ARMS[label]
            or row.get("input_hash") != frozen["input_hash"]
            or row.get("candidate_identity_hash") != frozen["candidate_identity_hash"]
            or mapping_hash != frozen_mapping_hash
            or canonical_sha256(row.get("candidate_mapping", ())) != frozen_mapping_hash
            or not isinstance(row.get("selected_sources"), list)
            or not isinstance(row.get("model_calls"), list)
            or row.get("cost_incomplete") is not False
        ):
            raise AuditError(f"{label} invalid checkpoint: {key}")
        observed_models = {str(call["model"]) for call in row["model_calls"]}
        if not observed_models <= MODELS[label]:
            raise AuditError(f"{label} checkpoint used unexpected model")
        if any(call.get("usage") is None or call.get("cost_incomplete") for call in row["model_calls"]):
            raise AuditError(f"{label} checkpoint has incomplete cost")
        by_key[key] = row
    if len(by_key) != 2209:
        raise AuditError(f"{label} valid checkpoint count is not 2209")

    attempts = read_jsonl_tolerant(path / "attempts.jsonl")
    if any(
        row.get("status") in {"retryable_error", "fatal_error"} for row in attempts
    ):
        raise AuditError(f"{label} contains failed attempts")
    summary = _json(path / "summary.json")
    if summary.get("config_sha256") != config["config_sha256"]:
        raise AuditError(f"{label} summary is not config-bound")
    return config, list(by_key.values())


def validate_inputs() -> dict[str, Any]:
    index, cases, side = _load_shared()
    configs: dict[str, Any] = {}
    successes: dict[str, list[dict[str, Any]]] = {}
    for label, path in (("cheap", CHEAP_RUN), ("verify", VERIFY_RUN)):
        configs[label], successes[label] = _validate_run(label, path, cases)
    case_fields = {
        label: {
            (row["episode_id"], row["target_evidence_id"]): (
                row["input_hash"],
                row["candidate_identity_hash"],
                canonical_sha256(row["candidate_mapping"]),
            )
            for row in rows
        }
        for label, rows in successes.items()
    }
    if case_fields["cheap"] != case_fields["verify"]:
        raise AuditError("arms do not share identical candidate/input checkpoints")
    return {
        "index": index,
        "cases": cases,
        "side": side,
        "configs": configs,
        "successes": successes,
    }


def _gold_match(
    source_entities: set[str],
    target_entities: set[str],
    scoring_records: Iterable[Mapping[str, Any]],
) -> set[str]:
    return {
        str(edge["gold_edge_identity_v1"])
        for record in scoring_records
        for edge in record["gold_edges"]
        if edge["source_entity"] in source_entities
        and edge["target_entity"] in target_entities
    }


def score_arm(
    successes: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    side: Mapping[str, Any],
) -> dict[str, Any]:
    success_by_target = {
        (str(row["episode_id"]), str(row["target_evidence_id"])): row
        for row in successes
    }
    cases_by_episode: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        cases_by_episode[str(case["episode_id"])].append(case)
    output_edges: list[dict[str, Any]] = []
    per_episode: list[dict[str, Any]] = []
    hop_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    flags = defaultdict(int)

    for episode in side["episodes"]:
        episode_id = str(episode["episode_id"])
        mappings = {
            str(row["evidence_id"]): row
            for row in episode["evidence_to_entity_mappings"]
        }
        possible_same: set[str] = set()
        episode_edges = []
        for case in cases_by_episode[episode_id]:
            target_id = str(case["target_evidence_id"])
            target_map = mappings[target_id]
            target_entities = _mapping_entities(target_map)
            for candidate in case["candidates"]:
                if candidate["source_origin"] == "same_session_turn_envelope":
                    possible_same |= _gold_match(
                        _mapping_entities(mappings[str(candidate["evidence_id"])]),
                        target_entities,
                        episode["scoring_records"],
                    )
            success = success_by_target[(episode_id, target_id)]
            for selected in success["selected_sources"]:
                source_id = str(selected["source_evidence_id"])
                source_map = mappings[source_id]
                matched = _gold_match(
                    _mapping_entities(source_map),
                    target_entities,
                    episode["scoring_records"],
                )
                source_flags = {
                    f"source_{key}": value
                    for key, value in _mapping_flags(source_map).items()
                }
                target_flags = {
                    f"target_{key}": value
                    for key, value in _mapping_flags(target_map).items()
                }
                edge = {
                    "edge_id": canonical_sha256((episode_id, source_id, target_id)),
                    "episode_id": episode_id,
                    "source_evidence_id": source_id,
                    "target_evidence_id": target_id,
                    "source_origin": selected["source_origin"],
                    "source_text": selected.get("source_text", ""),
                    "target_text": case["target_text"],
                    "matched_gold_edge_identities": sorted(matched),
                    **source_flags,
                    **target_flags,
                }
                for key, value in source_flags.items():
                    flags[key] += int(value)
                for key, value in target_flags.items():
                    flags[key] += int(value)
                output_edges.append(edge)
                episode_edges.append(edge)

        episode_hops = {}
        for record in episode["scoring_records"]:
            hop = int(record["hop"])
            gold = set(map(str, record["gold_edge_identity_v1_set"]))
            hit = {
                identity
                for edge in episode_edges
                for identity in edge["matched_gold_edge_identities"]
                if identity in gold
            }
            same_gold = gold & possible_same
            same_hit = {
                identity
                for edge in episode_edges
                if edge["source_origin"] == "same_session_turn_envelope"
                for identity in edge["matched_gold_edge_identities"]
                if identity in same_gold
            }
            row = {
                "episode_id": episode_id,
                "hop": hop,
                "gold_edge_identities": sorted(gold),
                "hit_edge_identities": sorted(hit),
                "missed_edge_identities": sorted(gold - hit),
                "same_session_coverable_edge_identities": sorted(same_gold),
                "same_session_hit_edge_identities": sorted(same_hit),
                "any_miss": bool(gold - hit),
                "graph_miss": bool(gold - hit),
            }
            hop_rows[hop].append(row)
            episode_hops[f"hop{hop}"] = row
        per_episode.append({"episode_id": episode_id, "hops": episode_hops})

    by_hop = {}
    for hop, rows in sorted(hop_rows.items()):
        gold = sum(len(row["gold_edge_identities"]) for row in rows)
        hit = sum(len(row["hit_edge_identities"]) for row in rows)
        same_gold = sum(len(row["same_session_coverable_edge_identities"]) for row in rows)
        same_hit = sum(len(row["same_session_hit_edge_identities"]) for row in rows)
        by_hop[f"hop{hop}"] = {
            "episode_count": len(rows),
            "unique_gold_edge_count": gold,
            "recalled_unique_gold_edge_count": hit,
            "unique_gold_edge_recall": hit / gold if gold else None,
            "same_session_coverable_unique_gold_edge_count": same_gold,
            "recalled_same_session_unique_gold_edge_count": same_hit,
            "same_session_unique_gold_edge_recall": same_hit / same_gold if same_gold else None,
            "episode_any_miss_count": sum(row["any_miss"] for row in rows),
            "episode_graph_miss_count": sum(row["graph_miss"] for row in rows),
        }
    evidence_edge_ids = {row["edge_id"] for row in output_edges}
    return {
        "by_hop": by_hop,
        "output_graph": {
            "selected_edge_incidence_count": len(output_edges),
            "unique_evidence_edge_count": len(evidence_edge_ids),
            "duplicate_evidence_edge_incidence_count": len(output_edges)
            - len(evidence_edge_ids),
            "history_incidence_count": sum(
                row["source_origin"] == "history_snapshot" for row in output_edges
            ),
            "same_session_incidence_count": sum(
                row["source_origin"] == "same_session_turn_envelope"
                for row in output_edges
            ),
            "unique_non_gold_unmatched_evidence_edge_count": len(
                {
                    row["edge_id"]
                    for row in output_edges
                    if not row["matched_gold_edge_identities"]
                }
            ),
        },
        "mapping_flags_on_output_edge_incidences": dict(sorted(flags.items())),
        "output_edges": output_edges,
        "per_episode": per_episode,
        "unit_contract": {
            "quality": "within each episode and hop, gold_edge_identity_v1 is set-deduplicated before counting",
            "output_incidence": "one selected source-target row emitted by one target checkpoint",
            "unique_evidence_edge": "set-deduplicated (episode_id, source_evidence_id, target_evidence_id)",
            "cross_hop": "the same semantic identity may occur in both hop views and is never summed as a new physical output edge",
            "aliases": "multiple evidence-to-entity aliases can match one gold identity, but the gold identity is counted once per episode/hop",
        },
    }


def _call_cost(call: Mapping[str, Any]) -> float:
    usage = call.get("usage")
    if not isinstance(usage, Mapping):
        raise AuditError("model call lacks usage")
    model = str(call["model"])
    if model not in PRICING:
        raise AuditError(f"unexpected priced model: {model}")
    price = PRICING[model]
    return (
        int(usage["prompt_tokens"]) * price["prompt"]
        + int(usage["completion_tokens"]) * price["completion"]
    ) / 1_000_000


def compute_costs(
    successes: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    shared = {
        str(row["episode_id"]): float(row["shared_preprocessing_total"]["known_usd"])
        for row in read_jsonl_tolerant(SHARED / "per_episode_cost.jsonl")
    }
    if len(shared) != 100:
        raise AuditError("shared per-episode cost must contain 100 rows")
    rows_by_arm: dict[str, dict[str, Any]] = {}
    summary: dict[str, Any] = {}
    for label, arm_rows in successes.items():
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in arm_rows:
            grouped[str(row["episode_id"])].append(row)
        by_episode = {}
        for episode_id in sorted(shared):
            calls = [
                call
                for success in grouped[episode_id]
                for call in success["model_calls"]
            ]
            selector = sum(_call_cost(call) for call in calls)
            by_episode[episode_id] = {
                "selector": selector,
                "shared_preprocessing": shared[episode_id],
                "deployment_total": selector + shared[episode_id],
                "selector_call_count": len(calls),
            }
        rows_by_arm[label] = by_episode
        summary[label] = {
            name: distribution([row[name] for row in by_episode.values()])
            for name in ("selector", "shared_preprocessing", "deployment_total")
        }
    summary["comparison"] = {
        name: compare_distributions(summary["cheap"][name], summary["verify"][name])
        for name in ("selector", "shared_preprocessing", "deployment_total")
    }
    return summary, rows_by_arm


def compare_misses(
    scored: Mapping[str, Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    episode_rows = {
        label: {
            row["episode_id"]: row
            for row in score["per_episode"]
        }
        for label, score in scored.items()
    }
    rows = []
    aggregate: dict[str, dict[str, Any]] = {}
    for episode_id in sorted(episode_rows["cheap"]):
        row: dict[str, Any] = {"episode_id": episode_id, "hops": {}}
        for hop in ("hop1", "hop2"):
            cheap_hop = episode_rows["cheap"][episode_id]["hops"].get(hop)
            verify_hop = episode_rows["verify"][episode_id]["hops"].get(hop)
            if cheap_hop is None or verify_hop is None:
                continue
            cheap_missed = set(cheap_hop["missed_edge_identities"])
            verify_missed = set(verify_hop["missed_edge_identities"])
            row["hops"][hop] = {
                "cheap_missed": sorted(cheap_missed),
                "verify_missed": sorted(verify_missed),
                "common_missed": sorted(cheap_missed & verify_missed),
                "cheap_only_missed_verify_recovers": sorted(cheap_missed - verify_missed),
                "verify_only_missed_cheap_recovers": sorted(verify_missed - cheap_missed),
            }
        rows.append(row)
    for hop in ("hop1", "hop2"):
        hop_rows = [row["hops"][hop] for row in rows if hop in row["hops"]]
        aggregate[hop] = {
            key: {
                "edge_identity_incidence_count": sum(len(row[key]) for row in hop_rows),
                "episode_count": sum(bool(row[key]) for row in hop_rows),
            }
            for key in (
                "common_missed",
                "cheap_only_missed_verify_recovers",
                "verify_only_missed_cheap_recovers",
            )
        }
    return rows, aggregate


def _exploratory_packet(scored: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    edge_maps = {
        label: {row["edge_id"]: row for row in score["output_edges"]}
        for label, score in scored.items()
    }
    cheap_ids, verify_ids = set(edge_maps["cheap"]), set(edge_maps["verify"])
    strata = {
        "cheap_only_unmatched": [
            edge_maps["cheap"][edge_id]
            for edge_id in cheap_ids - verify_ids
            if not edge_maps["cheap"][edge_id]["matched_gold_edge_identities"]
        ],
        "verify_only_unmatched": [
            edge_maps["verify"][edge_id]
            for edge_id in verify_ids - cheap_ids
            if not edge_maps["verify"][edge_id]["matched_gold_edge_identities"]
        ],
        "common_unmatched": [
            edge_maps["cheap"][edge_id]
            for edge_id in cheap_ids & verify_ids
            if not edge_maps["cheap"][edge_id]["matched_gold_edge_identities"]
        ],
    }
    seed = "p1-full100-posthoc-exploratory-cross-arm-v1"
    samples = {}
    for name, candidates in strata.items():
        ranked = sorted(
            candidates,
            key=lambda row: canonical_sha256(
                {"seed": seed, "stratum": name, "edge_id": row["edge_id"]}
            ),
        )
        selected, episodes = [], set()
        for row in ranked:
            if row["episode_id"] not in episodes:
                selected.append(row)
                episodes.add(row["episode_id"])
            if len(selected) == 10:
                break
        samples[name] = selected
    packet: dict[str, Any] = {
        "schema_version": "p1-full100-posthoc-exploratory-cross-arm-review-v1",
        "status": "packet_only_no_decisions",
        "post_hoc_exploratory_not_preregistered_primary_evidence": True,
        "population_precision_comparison_forbidden": True,
        "sampling": {
            "method": "fixed-hash rank, at most one edge per episode per stratum",
            "seed": seed,
            "max_per_stratum": 10,
        },
        "labels_required_if_reviewed": ["yes", "no", "ambiguous"],
        "stratum_population_counts": {
            name: len(rows) for name, rows in strata.items()
        },
        "samples": samples,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet


def _output_edge_comparison(scored: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    edge_ids = {
        label: {row["edge_id"] for row in score["output_edges"]}
        for label, score in scored.items()
    }
    cheap, verify = edge_ids["cheap"], edge_ids["verify"]
    return {
        "identity_unit": "unique evidence edge: (episode_id, source_evidence_id, target_evidence_id)",
        "common": len(cheap & verify),
        "cheap_only": len(cheap - verify),
        "verify_only": len(verify - cheap),
        "union": len(cheap | verify),
    }


def _independent_run_caveat(
    successes: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    cheap_calls = {
        (row["episode_id"], row["target_evidence_id"]): row["model_calls"][0]
        for row in successes["cheap"]
        if row["model_calls"]
    }
    verify_calls = {
        (row["episode_id"], row["target_evidence_id"]): row["model_calls"][0]
        for row in successes["verify"]
        if row["model_calls"]
    }
    common = sorted(set(cheap_calls) & set(verify_calls))
    same_prompt = sum(
        cheap_calls[key]["prompt_sha256"] == verify_calls[key]["prompt_sha256"]
        for key in common
    )
    same_answer = sum(
        cheap_calls[key]["answer_sha256"] == verify_calls[key]["answer_sha256"]
        for key in common
    )
    return {
        "cheap_llm_runs_are_independent": True,
        "shared_candidates_do_not_imply_reused_cheap_verdicts": True,
        "paired_episode_alignment_is_descriptive_not_deterministic_strong_only_counterfactual": True,
        "common_cheap_call_targets": len(common),
        "same_prompt_hash_count": same_prompt,
        "same_answer_hash_count": same_answer,
        "different_answer_hash_count": len(common) - same_answer,
    }


def run_audit(out: Path = DEFAULT_OUT) -> dict[str, Any]:
    if out in INPUT_FILES.values():
        raise AuditError("comparison output must not overlap an input directory")
    before = _tree_hashes()
    validated = validate_inputs()
    scored = {
        label: score_arm(rows, validated["cases"], validated["side"])
        for label, rows in validated["successes"].items()
    }
    costs, cost_rows = compute_costs(validated["successes"])
    miss_rows, miss_aggregate = compare_misses(scored)
    caveat = _independent_run_caveat(validated["successes"])
    output_comparison = _output_edge_comparison(scored)

    out.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": "p1-same-session-selector-full100-comparison-v1",
        "offline_only_no_model_or_api_calls": True,
        "inputs_read_only": {label: str(path) for label, path in INPUT_FILES.items()},
        "input_config_sha256": EXPECTED_CONFIGS,
        "shared_hashes": {
            "manifest_content": EXPECTED_SHARED_CONTENT,
            "manifest_index": EXPECTED_SHARED_INDEX,
            "v2_canonical": EXPECTED_V2_CANONICAL,
            "v2_file": EXPECTED_V2_FILE,
            "v2_manifest_canonical": EXPECTED_V2_MANIFEST_CANONICAL,
            "v2_manifest_file": EXPECTED_V2_MANIFEST_FILE,
        },
        "expected": {"targets": 2209, "hop1_episodes": 100, "hop2_episodes": 64},
        "scoring_rule": MAPPING_RULE,
        "same_session_denominator": (
            "gold identities coverable by at least one shared candidate with "
            "source_origin=same_session_turn_envelope"
        ),
        "source_sha256": sha256_file(Path(__file__)),
    }
    config["config_sha256"] = canonical_sha256(config)
    atomic_write_json(out / "config.json", config)
    atomic_write_json(out / "input_hashes_before.json", before)

    metrics = {
        "schema_version": "p1-full100-unified-metrics-v1",
        "scoring_rule": MAPPING_RULE,
        "approximate_diagnostic_only": True,
        "exact_precision_claim": False,
        "arms": {
            label: {
                key: value
                for key, value in score.items()
                if key not in {"output_edges", "per_episode"}
            }
            for label, score in scored.items()
        },
        "output_edge_comparison": output_comparison,
        "cost_usd": costs,
        "independent_run_caveat": caveat,
    }
    atomic_write_json(out / "unified_metrics.json", metrics)

    per_episode_payload = []
    score_episode = {
        label: {row["episode_id"]: row for row in score["per_episode"]}
        for label, score in scored.items()
    }
    miss_episode = {row["episode_id"]: row for row in miss_rows}
    for episode_id in sorted(score_episode["cheap"]):
        cheap_cost, verify_cost = cost_rows["cheap"][episode_id], cost_rows["verify"][episode_id]
        per_episode_payload.append(
            {
                "episode_id": episode_id,
                "cheap": score_episode["cheap"][episode_id] | {"cost_usd": cheap_cost},
                "verify": score_episode["verify"][episode_id] | {"cost_usd": verify_cost},
                "miss_differences": miss_episode[episode_id]["hops"],
                "cost_difference_usd": {
                    key: verify_cost[key] - cheap_cost[key]
                    for key in ("selector", "shared_preprocessing", "deployment_total")
                },
                "cost_ratio_verify_over_cheap": {
                    key: verify_cost[key] / cheap_cost[key] if cheap_cost[key] else None
                    for key in ("selector", "shared_preprocessing", "deployment_total")
                },
            }
        )
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in per_episode_payload
    )
    (out / "per_episode_comparison.jsonl").write_text(payload, encoding="utf-8")
    with (out / "per_episode_comparison.jsonl").open("rb") as handle:
        os.fsync(handle.fileno())
    atomic_write_json(
        out / "missed_edge_differences.json",
        {"aggregate": miss_aggregate, "per_episode": miss_rows},
    )
    atomic_write_json(out / "exploratory_review_packet.json", _exploratory_packet(scored))

    cheap_quality = scored["cheap"]["by_hop"]
    verify_quality = scored["verify"]["by_hop"]
    formal_dominance = all(
        cheap_quality[hop]["unique_gold_edge_recall"]
        > verify_quality[hop]["unique_gold_edge_recall"]
        and cheap_quality[hop]["episode_graph_miss_count"]
        < verify_quality[hop]["episode_graph_miss_count"]
        for hop in ("hop1", "hop2")
    ) and (
        costs["cheap"]["selector"]["total"] < costs["verify"]["selector"]["total"]
        and costs["cheap"]["deployment_total"]["total"]
        < costs["verify"]["deployment_total"]["total"]
    )
    summary = {
        "schema_version": "p1-full100-comparison-summary-v1",
        "config_sha256": config["config_sha256"],
        "completion": {
            "cheap": "2209/2209, no missing/failure/cost-incomplete",
            "verify": "2209/2209, no missing/failure/cost-incomplete",
            "episodes": "100 hop1 / 64 hop2",
        },
        "unified_quality": {
            label: score["by_hop"] for label, score in scored.items()
        },
        "output_graph": {
            label: score["output_graph"] for label, score in scored.items()
        }
        | {"cross_arm": output_comparison},
        "cost_usd": costs,
        "missed_edge_differences": miss_aggregate,
        "formal_p1_objective": {
            "priority": "first reduce graph miss, then cost",
            "cheap_dominates_verify_on_formal_quality_and_cost_dimensions": formal_dominance,
            "not_all_dimension_dominance": True,
            "verify_has_smaller_output_graph": (
                scored["verify"]["output_graph"]["unique_evidence_edge_count"]
                < scored["cheap"]["output_graph"]["unique_evidence_edge_count"]
            ),
            "approximate_scoring_limits_causal_and_precision_claims": True,
        },
        "independent_run_caveat": caveat,
        "exploratory_review": {
            "packet_only": True,
            "no_cross_run_precision_claim_from_original_100_vs_20_samples": True,
        },
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "summary.json", summary)

    after = _tree_hashes()
    atomic_write_json(out / "input_hashes_after.json", after)
    if before != after:
        raise AuditError("an input file changed during the read-only audit")
    output_files = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name != "output_hashes.json"
    }
    atomic_write_json(
        out / "output_hashes.json",
        {"files": output_files, "artifact_sha256": canonical_sha256(output_files)},
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    print(json.dumps(run_audit(args.out), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
