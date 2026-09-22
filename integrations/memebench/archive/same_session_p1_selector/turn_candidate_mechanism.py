"""Offline, turn-aware same-session candidate generation for a MEME dev audit.

The generator consumes only frozen node evidence. Gold entities, reviewed node
IDs, manual labels, extractor positions, embeddings, and model services are
intentionally absent from its interface.
"""

from __future__ import annotations

import random
import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from integrations.memebench.gold_edge_audit import canonical_sha256


SCHEMA_VERSION = "p1-same-session-turn-candidate-mechanism-v1"
DEFAULT_MIN_TOKEN_OVERLAP = 0.50
PERMUTATION_SEEDS = (7, 19, 43, 101, 211, 509, 997, 20260824)
TOKEN_RE = re.compile(r"\w+")


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.casefold()))


def _evidence_identity(
    episode_id: str,
    node: Mapping[str, Any],
    alignment: Mapping[str, Any],
) -> str:
    """Stable identity derived from evidence, never extractor position or UUID."""

    payload = {
        "episode_id": episode_id,
        "session_index": int(node["session_index"]),
        "original_session_id": str(node["original_session_id"]),
        "turn_index": alignment.get("turn_index"),
        "start": alignment.get("start"),
        "end": alignment.get("end"),
        "method": alignment["method"],
        "text": str(node["text"]),
    }
    return f"ev-{canonical_sha256(payload)}"


def align_node_to_raw_turn(
    episode_id: str,
    node: Mapping[str, Any],
    *,
    min_token_overlap: float = DEFAULT_MIN_TOKEN_OVERLAP,
) -> dict[str, Any]:
    """Align one extracted proposition to a unique raw turn.

    Exact matches win. Multiple exact turns, tied token-overlap maxima, missing
    turns, and low-overlap matches are conservatively marked ambiguous.
    """

    text = str(node.get("text") or "")
    turns = [
        {
            "turn_index": int(turn["turn_index"]),
            "role": str(turn.get("role") or ""),
            "content": str(turn.get("content") or ""),
        }
        for turn in node.get("original_turns", ())
        if turn.get("content") is not None
    ]
    base = {
        "node_id": str(node["node_id"]),
        "text": text,
        "session_index": int(node["session_index"]),
        "original_session_id": str(node["original_session_id"]),
    }
    if not text or not turns:
        return {
            **base,
            "evidence_id": None,
            "status": "ambiguous",
            "method": "ambiguous",
            "reason": "missing_text_or_turns",
            "turn_index": None,
            "start": None,
            "end": None,
            "quote": "",
            "token_overlap": 0.0,
        }

    exact = []
    for turn in turns:
        start = turn["content"].find(text)
        if start >= 0:
            exact.append((turn, start))
    if len(exact) == 1:
        turn, start = exact[0]
        result = {
            **base,
            "status": "aligned",
            "method": "exact",
            "reason": None,
            "turn_index": turn["turn_index"],
            "start": start,
            "end": start + len(text),
            "quote": turn["content"][start : start + len(text)],
            "token_overlap": 1.0,
        }
        result["evidence_id"] = _evidence_identity(episode_id, node, result)
        return result
    if len(exact) > 1:
        return {
            **base,
            "evidence_id": None,
            "status": "ambiguous",
            "method": "ambiguous",
            "reason": "multiple_exact_turns",
            "turn_index": None,
            "candidate_turn_indices": sorted(turn["turn_index"] for turn, _ in exact),
            "start": None,
            "end": None,
            "quote": "",
            "token_overlap": 1.0,
        }

    node_tokens = _tokens(text)
    scored = []
    for turn in turns:
        score = (
            len(node_tokens & _tokens(turn["content"])) / len(node_tokens)
            if node_tokens
            else 0.0
        )
        scored.append((score, turn))
    best_score = max(score for score, _ in scored)
    best = [turn for score, turn in scored if abs(score - best_score) <= 1e-12]
    if best_score < min_token_overlap:
        return {
            **base,
            "evidence_id": None,
            "status": "ambiguous",
            "method": "ambiguous",
            "reason": "token_overlap_below_threshold",
            "turn_index": None,
            "candidate_turn_indices": sorted(turn["turn_index"] for turn in best),
            "start": None,
            "end": None,
            "quote": "",
            "token_overlap": best_score,
        }
    if len(best) != 1:
        return {
            **base,
            "evidence_id": None,
            "status": "ambiguous",
            "method": "ambiguous",
            "reason": "token_overlap_tie",
            "turn_index": None,
            "candidate_turn_indices": sorted(turn["turn_index"] for turn in best),
            "start": None,
            "end": None,
            "quote": "",
            "token_overlap": best_score,
        }
    turn = best[0]
    result = {
        **base,
        "status": "aligned",
        "method": "token-overlap",
        "reason": None,
        "turn_index": turn["turn_index"],
        "start": None,
        "end": None,
        "quote": turn["content"],
        "token_overlap": best_score,
    }
    result["evidence_id"] = _evidence_identity(episode_id, node, result)
    return result


def _merge_alignment_aliases(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_evidence: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence_id = row.get("evidence_id")
        if not evidence_id:
            continue
        if evidence_id not in by_evidence:
            by_evidence[evidence_id] = {
                **row,
                "node_ids": [str(row["node_id"])],
            }
        else:
            by_evidence[evidence_id]["node_ids"].append(str(row["node_id"]))
    for row in by_evidence.values():
        row["node_ids"] = sorted(set(row["node_ids"]))
    return sorted(by_evidence.values(), key=lambda row: str(row["evidence_id"]))


def generate_turn_candidates(
    episode_id: str,
    nodes: Sequence[Mapping[str, Any]],
    *,
    min_token_overlap: float = DEFAULT_MIN_TOKEN_OVERLAP,
) -> dict[str, Any]:
    """Generate every evidence-safe same-session, strict-prior-turn envelope edge."""

    raw_alignments = [
        align_node_to_raw_turn(
            episode_id,
            node,
            min_token_overlap=min_token_overlap,
        )
        for node in nodes
    ]
    aligned = _merge_alignment_aliases(raw_alignments)
    candidates: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for source in aligned:
        for target in aligned:
            if source["evidence_id"] == target["evidence_id"]:
                continue
            same_session = (
                source["session_index"] == target["session_index"]
                and source["original_session_id"] == target["original_session_id"]
            )
            if not same_session:
                continue
            source_turn = int(source["turn_index"])
            target_turn = int(target["turn_index"])
            if source_turn == target_turn:
                pair = sorted((source["evidence_id"], target["evidence_id"]))
                if source["evidence_id"] == pair[0]:
                    unresolved.append(
                        {
                            "unresolved_id": f"same-turn-{canonical_sha256(pair)}",
                            "episode_id": episode_id,
                            "session_index": source["session_index"],
                            "original_session_id": source["original_session_id"],
                            "turn_index": source_turn,
                            "evidence_ids": pair,
                            "reason": "same_turn_no_explicit_causal_evidence",
                        }
                    )
                continue
            if source_turn > target_turn:
                continue
            identity = {
                "episode_id": episode_id,
                "source_evidence_id": source["evidence_id"],
                "target_evidence_id": target["evidence_id"],
            }
            candidates.append(
                {
                    "edge_id": f"cand-{canonical_sha256(identity)}",
                    **identity,
                    "source_node_ids": source["node_ids"],
                    "target_node_ids": target["node_ids"],
                    "source_text": source["text"],
                    "target_text": target["text"],
                    "session_index": source["session_index"],
                    "original_session_id": source["original_session_id"],
                    "source_turn_index": source_turn,
                    "target_turn_index": target_turn,
                    "source_alignment_method": source["method"],
                    "target_alignment_method": target["method"],
                    "alignment_stratum": (
                        "exact"
                        if source["method"] == target["method"] == "exact"
                        else "token-overlap"
                    ),
                    "source_span": {
                        key: source.get(key)
                        for key in ("turn_index", "start", "end", "quote", "token_overlap")
                    },
                    "target_span": {
                        key: target.get(key)
                        for key in ("turn_index", "start", "end", "quote", "token_overlap")
                    },
                }
            )
    candidates.sort(key=lambda row: row["edge_id"])
    unresolved.sort(key=lambda row: row["unresolved_id"])
    raw_alignments.sort(
        key=lambda row: (
            str(row.get("evidence_id") or ""),
            str(row["node_id"]),
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode_id,
        "alignments": raw_alignments,
        "candidates": candidates,
        "same_turn_unresolved": unresolved,
    }


def verify_permutation_invariance(
    episode_id: str,
    nodes: Sequence[Mapping[str, Any]],
    *,
    min_token_overlap: float = DEFAULT_MIN_TOKEN_OVERLAP,
    seeds: Sequence[int] = PERMUTATION_SEEDS,
) -> dict[str, Any]:
    baseline = generate_turn_candidates(
        episode_id, nodes, min_token_overlap=min_token_overlap
    )
    expected = {
        (
            row["edge_id"],
            row["source_evidence_id"],
            row["target_evidence_id"],
        )
        for row in baseline["candidates"]
    }
    checks = []
    orders: list[tuple[str, list[Mapping[str, Any]]]] = [
        ("reverse", list(reversed(nodes))),
    ]
    for seed in seeds:
        shuffled = list(nodes)
        random.Random(seed).shuffle(shuffled)
        orders.append((f"seed-{seed}", shuffled))
    for name, order in orders:
        observed = generate_turn_candidates(
            episode_id, order, min_token_overlap=min_token_overlap
        )
        identity = {
            (
                row["edge_id"],
                row["source_evidence_id"],
                row["target_evidence_id"],
            )
            for row in observed["candidates"]
        }
        checks.append(
            {
                "permutation": name,
                "candidate_count": len(identity),
                "identical": identity == expected,
            }
        )
    return {
        "episode_id": episode_id,
        "permutation_count": len(checks),
        "all_identical": all(row["identical"] for row in checks),
        "checks": checks,
    }


def audit_candidate_invariants(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    candidates = [
        candidate for result in results for candidate in result["candidates"]
    ]
    future_to_past = [
        row for row in candidates if row["source_turn_index"] > row["target_turn_index"]
    ]
    same_turn_directed = [
        row for row in candidates if row["source_turn_index"] == row["target_turn_index"]
    ]
    self_loops = [
        row
        for row in candidates
        if row["source_evidence_id"] == row["target_evidence_id"]
    ]
    missing_evidence = [
        row
        for row in candidates
        if not row.get("source_span")
        or not row.get("target_span")
        or row["source_span"].get("turn_index") is None
        or row["target_span"].get("turn_index") is None
    ]

    adjacency: dict[str, set[str]] = defaultdict(set)
    vertices: set[str] = set()
    for row in candidates:
        source = row["source_evidence_id"]
        target = row["target_evidence_id"]
        adjacency[source].add(target)
        vertices.update((source, target))
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

    for vertex in sorted(vertices):
        if color.get(vertex, 0) == 0:
            visit(vertex)
    return {
        "candidate_count": len(candidates),
        "future_to_past_count": len(future_to_past),
        "same_turn_directed_count": len(same_turn_directed),
        "self_loop_count": len(self_loops),
        "missing_evidence_count": len(missing_evidence),
        "directed_cycle_or_nontrivial_scc_count": int(cycle),
        "same_turn_unresolved_count": sum(
            len(result["same_turn_unresolved"]) for result in results
        ),
        "ambiguous_alignment_count": sum(
            row["status"] == "ambiguous"
            for result in results
            for row in result["alignments"]
        ),
    }


def score_with_manual_gold(
    results: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    manual_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Scoring-side only. Manual identities never flow back into generation."""

    candidates = [
        candidate for result in results for candidate in result["candidates"]
    ]
    by_episode = defaultdict(list)
    for row in candidates:
        by_episode[row["episode_id"]].append(row)
    manual_by_edge = {
        str(record["semantic_edge_id"]): record for record in manual_records
    }
    edge_rows = []
    recovered_candidate_ids: set[str] = set()
    for decision in decisions:
        edge_id = str(decision["semantic_edge_id"])
        source_id = str(decision["reviewed_valid_source_node_id"])
        target_id = str(decision["reviewed_valid_target_node_id"])
        episode_candidates = by_episode[str(decision["episode_id"])]
        valid = [
            row
            for row in episode_candidates
            if source_id in row["source_node_ids"] and target_id in row["target_node_ids"]
        ]
        manual = manual_by_edge[edge_id]
        substring_only_ids = set(
            map(str, manual.get("substring_only_source_match_node_ids", ()))
        )
        invalid_to_gold_target = [
            row
            for row in episode_candidates
            if substring_only_ids.intersection(row["source_node_ids"])
            and target_id in row["target_node_ids"]
        ]
        invalid_anywhere = [
            row
            for row in episode_candidates
            if substring_only_ids.intersection(row["source_node_ids"])
        ]
        source_alignment = next(
            (
                alignment
                for result in results
                if result["episode_id"] == decision["episode_id"]
                for alignment in result["alignments"]
                if alignment["node_id"] == source_id
            ),
            None,
        )
        target_alignment = next(
            (
                alignment
                for result in results
                if result["episode_id"] == decision["episode_id"]
                for alignment in result["alignments"]
                if alignment["node_id"] == target_id
            ),
            None,
        )
        if (
            source_alignment is None
            or target_alignment is None
            or source_alignment["status"] == "ambiguous"
            or target_alignment["status"] == "ambiguous"
        ):
            stratum = "ambiguous"
        elif (
            source_alignment["method"] == "exact"
            and target_alignment["method"] == "exact"
        ):
            stratum = "exact"
        else:
            stratum = "token-overlap"
        recovered_candidate_ids.update(row["edge_id"] for row in valid)
        edge_rows.append(
            {
                "semantic_edge_id": edge_id,
                "episode_id": str(decision["episode_id"]),
                "recovered": bool(valid),
                "alignment_stratum": stratum,
                "candidate_edge_ids": sorted(row["edge_id"] for row in valid),
                "substring_only_to_gold_target_count": len(invalid_to_gold_target),
                "substring_only_to_gold_target_candidate_edge_ids": sorted(
                    row["edge_id"] for row in invalid_to_gold_target
                ),
                "substring_only_outgoing_candidate_count": len(invalid_anywhere),
                "substring_only_outgoing_candidate_edge_ids": sorted(
                    row["edge_id"] for row in invalid_anywhere
                ),
                "substring_only_source_match_node_ids": sorted(substring_only_ids),
                "substring_only_candidate_classification": (
                    "reference/identity-ambiguous"
                    if invalid_anywhere
                    else "not_in_candidate_envelope"
                ),
                "substring_only_matches_counted_as_recovery": 0,
            }
        )
    strata = {}
    for stratum in ("exact", "token-overlap", "ambiguous"):
        rows = [row for row in edge_rows if row["alignment_stratum"] == stratum]
        strata[stratum] = {
            "gold_edge_count": len(rows),
            "recovered_count": sum(row["recovered"] for row in rows),
            "failed_edge_ids": sorted(
                row["semantic_edge_id"] for row in rows if not row["recovered"]
            ),
            "candidate_count": sum(
                row["alignment_stratum"] == stratum for row in candidates
            ),
        }
    episodes = {}
    for episode_id in sorted({row["episode_id"] for row in edge_rows}):
        rows = [row for row in edge_rows if row["episode_id"] == episode_id]
        episodes[episode_id] = {
            "gold_edge_count": len(rows),
            "recovered_count": sum(row["recovered"] for row in rows),
            "all_recovered": all(row["recovered"] for row in rows),
        }
    return {
        "semantic_edge_count": len(edge_rows),
        "semantic_edge_recovered_count": sum(row["recovered"] for row in edge_rows),
        "episode_count": len(episodes),
        "episode_all_edges_recovered_count": sum(
            row["all_recovered"] for row in episodes.values()
        ),
        "edge_results": edge_rows,
        "episode_results": episodes,
        "alignment_strata": strata,
        "recovered_candidate_edge_ids": sorted(recovered_candidate_ids),
    }


def candidate_inflation(
    results: Sequence[Mapping[str, Any]],
    evidence_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    traces = {
        (str(record["episode_id"]), str(trace["node_id"])): trace
        for record in evidence_records
        for trace in record["candidate_traces"]
    }
    incoming: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for result in results:
        for candidate in result["candidates"]:
            for target_node_id in candidate["target_node_ids"]:
                incoming[(result["episode_id"], target_node_id)].append(candidate)
    per_node = []
    for result in results:
        for alignment in result["alignments"]:
            key = (result["episode_id"], alignment["node_id"])
            baseline = int(traces.get(key, {}).get("candidate_snapshot_size", 0))
            added = len(incoming.get(key, ()))
            combined = baseline + added
            per_node.append(
                {
                    "episode_id": result["episode_id"],
                    "node_id": alignment["node_id"],
                    "evidence_id": alignment.get("evidence_id"),
                    "alignment_status": alignment["status"],
                    "history_only_candidate_count": baseline,
                    "added_same_session_cross_turn_candidate_count": added,
                    "combined_candidate_count": combined,
                    "candidate_multiplier": combined / baseline if baseline else None,
                    "added_share_of_combined": added / combined if combined else 0.0,
                }
            )
    per_episode = {}
    for episode_id in sorted({row["episode_id"] for row in per_node}):
        rows = [row for row in per_node if row["episode_id"] == episode_id]
        baseline = sum(row["history_only_candidate_count"] for row in rows)
        added = sum(row["added_same_session_cross_turn_candidate_count"] for row in rows)
        combined = baseline + added
        per_episode[episode_id] = {
            "node_count": len(rows),
            "history_only_candidate_count": baseline,
            "added_same_session_cross_turn_candidate_count": added,
            "combined_candidate_count": combined,
            "candidate_multiplier": combined / baseline if baseline else None,
            "absolute_delta": added,
            "added_share_of_combined": added / combined if combined else 0.0,
        }
    baseline = sum(row["history_only_candidate_count"] for row in per_node)
    added = sum(row["added_same_session_cross_turn_candidate_count"] for row in per_node)
    combined = baseline + added
    return {
        "history_only_same_session_candidate_count": 0,
        "history_only_candidate_count": baseline,
        "added_same_session_cross_turn_candidate_count": added,
        "combined_candidate_count": combined,
        "candidate_multiplier": combined / baseline if baseline else None,
        "absolute_delta": added,
        "added_share_of_combined": added / combined if combined else 0.0,
        "per_episode": per_episode,
        "per_node": sorted(
            per_node, key=lambda row: (row["episode_id"], str(row["evidence_id"]))
        ),
    }


def build_non_gold_review_packet(
    results: Sequence[Mapping[str, Any]],
    recovered_candidate_edge_ids: Sequence[str],
    *,
    sample_per_episode: int = 2,
    sample_seed: str = "p1-turn-mechanism-review-v1",
) -> dict[str, Any]:
    recovered = set(recovered_candidate_edge_ids)
    unmatched = [
        candidate
        for result in results
        for candidate in result["candidates"]
        if candidate["edge_id"] not in recovered
    ]
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in unmatched:
        by_episode[row["episode_id"]].append(row)
    sample = []
    for episode_id in sorted(by_episode):
        ranked = sorted(
            by_episode[episode_id],
            key=lambda row: canonical_sha256(
                {"seed": sample_seed, "edge_id": row["edge_id"]}
            ),
        )
        sample.extend(ranked[:sample_per_episode])
    packet = {
        "schema_version": "p1-turn-candidate-non-gold-review-packet-v1",
        "reviewer_guidance": {
            "labels": ["yes", "no", "ambiguous"],
            "yes": "source proposition is a semantic dependency of target proposition",
            "no": "source proposition is not a semantic dependency of target proposition",
            "ambiguous": "frozen text is insufficient for a confident decision",
            "non_gold_is_not_false_positive": True,
        },
        "sampling": {
            "method": "deterministic_hash_rank_within_episode",
            "uses_candidate_score": False,
            "seed": sample_seed,
            "sample_per_episode": sample_per_episode,
        },
        "unmatched_candidate_count": len(unmatched),
        "sample_count": len(sample),
        "sample": sample,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet


def summarize_manual_review(
    packet: Mapping[str, Any], decisions: Mapping[str, Any] | None
) -> dict[str, Any]:
    if decisions is None:
        return {
            "status": "not_reviewed",
            "sample_count": int(packet["sample_count"]),
            "counts": {"yes": 0, "no": 0, "ambiguous": 0},
        }
    if decisions.get("packet_sha256") != packet["packet_sha256"]:
        raise ValueError("manual review decisions are not bound to review packet")
    rows = list(decisions.get("decisions") or ())
    expected = {row["edge_id"] for row in packet["sample"]}
    observed = {str(row["edge_id"]) for row in rows}
    if len(rows) != len(observed) or observed != expected:
        raise ValueError("manual review decision edge set differs from packet sample")
    allowed = {"yes", "no", "ambiguous"}
    if any(row.get("label") not in allowed for row in rows):
        raise ValueError("manual review labels must be yes/no/ambiguous")
    decision_hash = canonical_sha256(
        decisions, exclude_fields=("decisions_sha256",)
    )
    if decisions.get("decisions_sha256") not in (None, decision_hash):
        raise ValueError("manual review decisions hash mismatch")
    if decisions.get("immutable") is not True:
        raise ValueError("manual review decisions must declare immutable=true")
    counts = Counter(str(row["label"]) for row in rows)
    return {
        "status": "complete",
        "reviewer_type": decisions.get("reviewer_type"),
        "decision_artifact_sha256": decision_hash,
        "sample_count": len(rows),
        "counts": {label: counts[label] for label in ("yes", "no", "ambiguous")},
        "development_mechanism_diagnostic_only": True,
    }
