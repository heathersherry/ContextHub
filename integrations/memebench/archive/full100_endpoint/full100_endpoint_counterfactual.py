"""Offline endpoint adjudication and provenance-alignment counterfactuals.

Runtime alignment consumes frozen shared shards only. Gold and adjudication are
accepted by separate scoring functions and never enter candidate generation.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from contexthub.planning.statistics import clopper_pearson_upper
from integrations.memebench.gold_edge_audit import canonical_sha256


DECISION_CLASSES = {
    "confirmed_scoring_false_miss",
    "unobservable_unscorable_gold_target",
    "gold_evidence_mismatch_unknown",
    "true_candidate_envelope_miss",
}
ENDPOINT_VERDICTS = {"confirmed_hit", "unscorable", "unknown", "confirmed_miss"}
TOKEN_RE = re.compile(r"[a-z0-9]+")
DEFAULT_MIN_OVERLAP = 0.20
PERMUTATION_SEEDS = (7, 43, 509, 20260824)


class CounterfactualError(RuntimeError):
    """Fail-closed validation error."""


def validate_ledger(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    if ledger.get("schema_version") != "p1-full100-endpoint-adjudication-v1":
        raise CounterfactualError("unsupported adjudication schema")
    if ledger.get("immutable") is not True:
        raise CounterfactualError("adjudication ledger must declare immutable=true")
    rows = list(ledger.get("decisions") or ())
    if len(rows) != 19:
        raise CounterfactualError("adjudication ledger must contain 19 decisions")
    keys = [(str(row.get("episode_id")), str(row.get("gold_identity"))) for row in rows]
    if len(set(keys)) != len(keys):
        raise CounterfactualError("duplicate episode/gold identity decision")
    required = {
        "decision_id",
        "episode_id",
        "gold_identity",
        "hop_views",
        "source_gold_endpoint",
        "target_gold_endpoint",
        "source_evidence",
        "target_evidence",
        "candidate_status",
        "selection_status",
        "decision_class",
        "endpoint_verdict",
        "evidence_explanation",
    }
    for row in rows:
        missing = required - set(row)
        if missing:
            raise CounterfactualError(f"decision lacks fields: {sorted(missing)}")
        if row["decision_class"] not in DECISION_CLASSES:
            raise CounterfactualError("invalid decision class")
        if row["endpoint_verdict"] not in ENDPOINT_VERDICTS:
            raise CounterfactualError("invalid endpoint verdict")
        hops = list(row["hop_views"])
        if not hops or not set(hops) <= {1, 2} or len(hops) != len(set(hops)):
            raise CounterfactualError("invalid hop views")
        for endpoint in ("source_evidence", "target_evidence"):
            evidence = row[endpoint]
            if not isinstance(evidence, Mapping) or not evidence.get("status"):
                raise CounterfactualError(f"{endpoint} must contain explicit evidence status")
        overlap = set(row.get("secondary_flags") or ())
        allowed_flags = {
            "target_surface_mapping_mismatch",
            "source_surface_mapping_mismatch",
            "source_identity_mapping_mismatch",
            "conditional_null_not_asserted",
        }
        if not overlap <= allowed_flags:
            raise CounterfactualError("unknown secondary classification flag")
    expected_verdict = {
        "confirmed_scoring_false_miss": "confirmed_hit",
        "unobservable_unscorable_gold_target": "unscorable",
        "gold_evidence_mismatch_unknown": "unknown",
        "true_candidate_envelope_miss": "confirmed_miss",
    }
    if any(expected_verdict[row["decision_class"]] != row["endpoint_verdict"] for row in rows):
        raise CounterfactualError("primary class and endpoint verdict disagree")
    decision_hash = canonical_sha256(ledger, exclude_fields=("decisions_sha256",))
    if ledger.get("decisions_sha256") != decision_hash:
        raise CounterfactualError("adjudication content hash mismatch")
    counts = Counter(row["decision_class"] for row in rows)
    if counts != Counter(
        {
            "confirmed_scoring_false_miss": 10,
            "unobservable_unscorable_gold_target": 2,
            "gold_evidence_mismatch_unknown": 1,
            "true_candidate_envelope_miss": 6,
        }
    ):
        raise CounterfactualError("adjudication class counts differ from reviewed ledger")
    return rows


def _fraction(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _cp(misses: int, denominator: int, alpha: float = 0.05) -> dict[str, Any]:
    return {
        **_fraction(misses, denominator),
        "one_sided_confidence": 1 - alpha,
        "clopper_pearson_upper": (
            clopper_pearson_upper(misses, denominator, alpha) if denominator else None
        ),
    }


def recompute_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    raw: Mapping[int, Mapping[str, int]],
    epsilons: Sequence[float] = (0.05, 0.10, 0.15),
) -> dict[str, Any]:
    """Recompute endpoint metrics from frozen raw totals plus reviewed decisions."""

    by_hop: dict[str, Any] = {}
    for hop in (1, 2):
        hop_rows = [row for row in rows if hop in row["hop_views"]]
        false = [row for row in hop_rows if row["endpoint_verdict"] == "confirmed_hit"]
        pipeline = [row for row in hop_rows if row["endpoint_verdict"] == "confirmed_miss"]
        excluded = [
            row for row in hop_rows if row["endpoint_verdict"] in {"unscorable", "unknown"}
        ]
        total = int(raw[hop]["gold_edges"])
        raw_hits = int(raw[hop]["hits"])
        raw_episode_misses = int(raw[hop]["episode_misses"])
        episodes = int(raw[hop]["episodes"])
        excluded_only_episodes = {
            str(row["episode_id"]) for row in excluded
        } - {
            str(row["episode_id"])
            for row in hop_rows
            if row["endpoint_verdict"] == "confirmed_miss"
        }
        conservative_miss_episodes = {
            str(row["episode_id"])
            for row in hop_rows
            if row["endpoint_verdict"] in {"confirmed_miss", "unscorable", "unknown"}
        }
        corrected_denominator = total - len(excluded)
        corrected_hits = raw_hits + len(false)
        observable_episode_denominator = episodes - len(excluded_only_episodes)
        pipeline_episode_ids = {str(row["episode_id"]) for row in pipeline}
        by_hop[f"hop{hop}"] = {
            "raw_approximate": {
                "edge_recall": _fraction(raw_hits, total),
                "graph_miss": _cp(raw_episode_misses, episodes),
            },
            "corrected_observable_denominator": {
                "edge_recall": _fraction(corrected_hits, corrected_denominator),
                "graph_miss": _cp(
                    len(pipeline_episode_ids), observable_episode_denominator
                ),
                "excluded_unscorable_or_unknown_edges": len(excluded),
                "excluded_only_episode_clusters": len(excluded_only_episodes),
            },
            "conservative_unknown_as_miss": {
                "edge_recall": _fraction(
                    total - len(pipeline) - len(excluded), total
                ),
                "graph_miss": _cp(len(conservative_miss_episodes), episodes),
            },
            "confirmed_pipeline_miss": {
                "edge_unit": _cp(len(pipeline), corrected_denominator),
                "episode_cluster_all_eligible": _cp(len(pipeline_episode_ids), episodes),
                "episode_cluster_observable": _cp(
                    len(pipeline_episode_ids), observable_episode_denominator
                ),
            },
        }

    false_rows = [row for row in rows if row["endpoint_verdict"] == "confirmed_hit"]
    pipeline_rows = [row for row in rows if row["endpoint_verdict"] == "confirmed_miss"]
    excluded_rows = [
        row for row in rows if row["endpoint_verdict"] in {"unscorable", "unknown"}
    ]
    pipeline_episodes = {str(row["episode_id"]) for row in pipeline_rows}
    excluded_only = {str(row["episode_id"]) for row in excluded_rows} - pipeline_episodes
    conservative_episodes = {
        str(row["episode_id"])
        for row in rows
        if row["endpoint_verdict"] in {"confirmed_miss", "unscorable", "unknown"}
    }
    unique_dependency = {
        "raw_approximate_miss": _fraction(19, 19),
        "corrected_observable_miss": _fraction(len(pipeline_rows), 19 - len(excluded_rows)),
        "conservative_unknown_as_miss": _fraction(
            len(pipeline_rows) + len(excluded_rows), 19
        ),
        "confirmed_pipeline_miss": _fraction(len(pipeline_rows), 19 - len(excluded_rows)),
        "confirmed_scoring_false_miss_count": len(false_rows),
    }
    episode_cluster = {
        "confirmed_pipeline_miss_all_100": _cp(len(pipeline_episodes), 100),
        "confirmed_pipeline_miss_observable": _cp(
            len(pipeline_episodes), 100 - len(excluded_only)
        ),
        "conservative_unknown_as_miss": _cp(len(conservative_episodes), 100),
    }
    primary = episode_cluster["confirmed_pipeline_miss_all_100"]
    sensitivity = []
    for epsilon in epsilons:
        sensitivity.append(
            {
                "epsilon_graph": epsilon,
                "point_estimate_pass": bool(primary["rate"] <= epsilon),
                "cp_upper_pass": bool(primary["clopper_pearson_upper"] <= epsilon),
                "must_be_prefrozen_before_new_held_out_certification": True,
            }
        )
    return {
        "schema_version": "p1-full100-offline-counterfactual-metrics-v1",
        "development_diagnostic_only": True,
        "held_out_certification": False,
        "by_hop_edge_and_episode": by_hop,
        "unique_dependency_unit": unique_dependency,
        "episode_cluster_unit": episode_cluster,
        "epsilon_sensitivity_primary_confirmed_pipeline_all_100": sensitivity,
        "primary_graph_miss_definition": (
            "episodes with at least one confirmed true candidate-envelope miss / "
            "all 100 full100 development episodes"
        ),
    }


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.casefold()))


def align_node_multicandidate(
    episode_id: str,
    node: Mapping[str, Any],
    *,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
) -> dict[str, Any]:
    """Approximate missing extraction-time provenance with retained turn hypotheses."""

    text = str(node.get("text") or "")
    turns = [
        {
            "turn_index": int(turn["turn_index"]),
            "role": str(turn.get("role") or ""),
            "content": str(turn.get("content") or ""),
            "turn_sha256": str(turn.get("turn_sha256") or ""),
        }
        for turn in node.get("original_turns", ())
        if turn.get("content") is not None
    ]
    base = {
        "episode_id": episode_id,
        "node_id": str(node["node_id"]),
        "text": text,
        "session_index": int(node["session_index"]),
        "original_session_id": str(node["original_session_id"]),
    }
    if not text or not turns:
        return {**base, "status": "quarantine", "reason": "missing_text_or_turns", "hypotheses": []}
    exact = []
    for turn in turns:
        start = turn["content"].find(text)
        if start >= 0:
            exact.append(
                {
                    **turn,
                    "start": start,
                    "end": start + len(text),
                    "quote": text,
                    "overlap": 1.0,
                    "method": "exact",
                }
            )
    if exact:
        hypotheses = exact
        reason = None if len(exact) == 1 else "multiple_exact_retained"
    else:
        node_tokens = _tokens(text)
        scored = []
        for turn in turns:
            overlap = (
                len(node_tokens & _tokens(turn["content"])) / len(node_tokens)
                if node_tokens
                else 0.0
            )
            scored.append((overlap, turn))
        user_scored = [row for row in scored if row[1]["role"] == "user"]
        pool = user_scored if user_scored and max(score for score, _ in user_scored) > 0 else scored
        best = max(score for score, _ in pool)
        if best < min_overlap:
            return {
                **base,
                "status": "quarantine",
                "reason": "overlap_below_minimum",
                "best_overlap": best,
                "hypotheses": [],
            }
        hypotheses = [
            {
                **turn,
                "start": None,
                "end": None,
                "quote": turn["content"],
                "overlap": score,
                "method": "token-overlap-user-preferred",
            }
            for score, turn in pool
            if math.isclose(score, best, abs_tol=1e-12)
        ]
        reason = None if len(hypotheses) == 1 else "tied_best_user_turns_retained"
    hypotheses.sort(key=lambda row: (row["turn_index"], row["turn_sha256"]))
    identity = canonical_sha256(
        {
            "episode_id": episode_id,
            "session_index": base["session_index"],
            "original_session_id": base["original_session_id"],
            "text": text,
            "hypotheses": [
                {
                    key: row[key]
                    for key in ("turn_index", "turn_sha256", "start", "end", "method")
                }
                for row in hypotheses
            ],
        }
    )
    return {
        **base,
        "evidence_id": f"pev-{identity}",
        "status": "aligned" if len(hypotheses) == 1 else "multi_candidate",
        "reason": reason,
        "hypotheses": hypotheses,
    }


def generate_provenance_candidates(
    episode_id: str,
    nodes: Sequence[Mapping[str, Any]],
    *,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
) -> dict[str, Any]:
    """Generate pair-provenanced same-session candidates without gold inputs."""

    alignments = [
        align_node_multicandidate(episode_id, node, min_overlap=min_overlap)
        for node in nodes
    ]
    candidates: list[dict[str, Any]] = []
    same_turn_unresolved: set[str] = set()
    for source in alignments:
        for target in alignments:
            if source["node_id"] == target["node_id"]:
                continue
            if source["session_index"] > target["session_index"]:
                continue
            same_session = (
                source["session_index"] == target["session_index"]
                and source["original_session_id"] == target["original_session_id"]
            )
            if source["session_index"] == target["session_index"] and not same_session:
                continue
            if same_session:
                source_turns = {
                    int(row["turn_index"]) for row in source["hypotheses"]
                }
                target_turns = {
                    int(row["turn_index"]) for row in target["hypotheses"]
                }
                for turn_index in source_turns & target_turns:
                    same_turn_unresolved.add(
                        canonical_sha256(
                            {
                                "episode_id": episode_id,
                                "node_ids": sorted(
                                    (source["node_id"], target["node_id"])
                                ),
                                "turn_index": turn_index,
                            }
                        )
                    )
                # A directed edge is emitted only when every retained source
                # hypothesis strictly precedes every target hypothesis.
                if not source_turns or not target_turns or max(source_turns) >= min(
                    target_turns
                ):
                    continue
            for source_hypothesis in source["hypotheses"]:
                for target_hypothesis in target["hypotheses"]:
                    source_turn = int(source_hypothesis["turn_index"])
                    target_turn = int(target_hypothesis["turn_index"])
                    if same_session and source_turn == target_turn:
                        same_turn_unresolved.add(
                            canonical_sha256(
                                {
                                    "episode_id": episode_id,
                                    "node_ids": sorted((source["node_id"], target["node_id"])),
                                    "turn_index": source_turn,
                                }
                            )
                        )
                        continue
                    if same_session and source_turn > target_turn:
                        continue
                    identity = {
                        "episode_id": episode_id,
                        "source_node_id": source["node_id"],
                        "target_node_id": target["node_id"],
                        "source_turn_index": source_turn,
                        "target_turn_index": target_turn,
                        "source_turn_sha256": source_hypothesis["turn_sha256"],
                        "target_turn_sha256": target_hypothesis["turn_sha256"],
                        "source_session_index": source["session_index"],
                        "target_session_index": target["session_index"],
                    }
                    candidates.append(
                        {
                            "candidate_id": f"pcand-{canonical_sha256(identity)}",
                            **identity,
                            "source_text": source["text"],
                            "target_text": target["text"],
                            "source_evidence_id": source.get("evidence_id"),
                            "target_evidence_id": target.get("evidence_id"),
                            "source_alignment_status": source["status"],
                            "target_alignment_status": target["status"],
                        }
                    )
    by_id = {row["candidate_id"]: row for row in candidates}
    return {
        "schema_version": "p1-provenance-multicandidate-approximation-v1",
        "episode_id": episode_id,
        "alignments": sorted(alignments, key=lambda row: row["node_id"]),
        "candidates": [by_id[key] for key in sorted(by_id)],
        "same_turn_unresolved_count": len(same_turn_unresolved),
    }


def audit_alignment_invariants(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    candidates = [row for result in results for row in result["candidates"]]
    adjacency: dict[str, set[str]] = defaultdict(set)
    for row in candidates:
        adjacency[row["source_node_id"]].add(row["target_node_id"])
    color: dict[str, int] = {}
    cycle = False

    def visit(node: str) -> None:
        nonlocal cycle
        color[node] = 1
        for target in adjacency[node]:
            if color.get(target) == 1:
                cycle = True
            elif color.get(target, 0) == 0:
                visit(target)
        color[node] = 2

    for node in sorted(set(adjacency) | {n for values in adjacency.values() for n in values}):
        if color.get(node, 0) == 0:
            visit(node)
    alignments = [row for result in results for row in result["alignments"]]
    return {
        "candidate_count": len(candidates),
        "future_to_past_count": sum(
            row["source_session_index"] > row["target_session_index"]
            or (
                row["source_session_index"] == row["target_session_index"]
                and row["source_turn_index"] > row["target_turn_index"]
            )
            for row in candidates
        ),
        "same_turn_directed_count": sum(
            row["source_session_index"] == row["target_session_index"]
            and row["source_turn_index"] == row["target_turn_index"]
            for row in candidates
        ),
        "self_loop_count": sum(
            row["source_node_id"] == row["target_node_id"] for row in candidates
        ),
        "directed_cycle_or_nontrivial_scc_count": int(cycle),
        "alignment_node_count": len(alignments),
        "aligned_unique_count": sum(row["status"] == "aligned" for row in alignments),
        "multi_candidate_count": sum(row["status"] == "multi_candidate" for row in alignments),
        "quarantine_count": sum(row["status"] == "quarantine" for row in alignments),
        "same_turn_unresolved_count": sum(
            int(result["same_turn_unresolved_count"]) for result in results
        ),
    }


def verify_alignment_permutation(
    episode_id: str,
    nodes: Sequence[Mapping[str, Any]],
    *,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    seeds: Sequence[int] = PERMUTATION_SEEDS,
) -> bool:
    baseline = {
        row["candidate_id"]
        for row in generate_provenance_candidates(
            episode_id, nodes, min_overlap=min_overlap
        )["candidates"]
    }
    orders = [list(reversed(nodes))]
    for seed in seeds:
        shuffled = list(nodes)
        random.Random(seed).shuffle(shuffled)
        orders.append(shuffled)
    return all(
        {
            row["candidate_id"]
            for row in generate_provenance_candidates(
                episode_id, order, min_overlap=min_overlap
            )["candidates"]
        }
        == baseline
        for order in orders
    )


def score_alignment_recovery(
    results: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Gold-isolated post-generation scoring against adjudicated node endpoints."""

    candidates = [row for result in results for row in result["candidates"]]
    by_episode: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in candidates:
        by_episode[str(row["episode_id"])].append(row)
    rows = []
    for decision in decisions:
        source_ids = set(map(str, decision["source_evidence"].get("node_ids") or ()))
        target_ids = set(map(str, decision["target_evidence"].get("node_ids") or ()))
        matches = [
            row
            for row in by_episode[str(decision["episode_id"])]
            if row["source_node_id"] in source_ids and row["target_node_id"] in target_ids
        ]
        baseline_confirmed = (
            decision["decision_class"] == "confirmed_scoring_false_miss"
        )
        rows.append(
            {
                "decision_id": decision["decision_id"],
                "episode_id": decision["episode_id"],
                "decision_class": decision["decision_class"],
                "candidate_covered_after_counterfactual": baseline_confirmed
                or bool(matches),
                "coverage_source": (
                    "frozen_checkpoint_adjudication"
                    if baseline_confirmed
                    else "offline_alignment_counterfactual"
                    if matches
                    else "not_covered_or_unscorable"
                ),
                "candidate_ids": sorted(row["candidate_id"] for row in matches),
            }
        )
    true_rows = [row for row in rows if row["decision_class"] == "true_candidate_envelope_miss"]
    observable_rows = [
        row
        for row in rows
        if row["decision_class"]
        not in {
            "unobservable_unscorable_gold_target",
            "gold_evidence_mismatch_unknown",
        }
    ]
    return {
        "all_19_audited_candidate_coverage": {
            "numerator": sum(
                row["candidate_covered_after_counterfactual"] for row in rows
            ),
            "denominator": len(rows),
        },
        "observable_audited_candidate_coverage": {
            "numerator": sum(
                row["candidate_covered_after_counterfactual"]
                for row in observable_rows
            ),
            "denominator": len(observable_rows),
        },
        "confirmed_true_misses_recovered": sum(
            row["candidate_covered_after_counterfactual"] for row in true_rows
        ),
        "confirmed_true_miss_count": len(true_rows),
        "rows": rows,
        "gold_used_only_after_generation": True,
    }
