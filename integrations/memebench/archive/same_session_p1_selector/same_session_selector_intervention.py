"""Benchmark-only adapter for turn-aware candidates and the real P1 selector."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.cascade_router import (
    route_candidate_selection,
    route_edge_discovery,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from integrations.memebench.gold_edge_audit import canonical_sha256
from integrations.memebench.turn_candidate_mechanism import generate_turn_candidates


SCHEMA_VERSION = "p1-same-session-selector-intervention-v1"
ARM_CONFIGS = {
    "turn_full_cheap": {
        "candidate_route": {"tau": 1.0, "k": 5, "recency": False},
        "edge_route": {"tau": 0.0, "lam": None},
        "edge_semantics": "cheap-only",
    },
    "turn_full_verify": {
        "candidate_route": {"tau": 1.0, "k": 5, "recency": False},
        "edge_route": {"tau": 0.0, "lam": 0.0},
        "edge_semantics": "always-verify-if-cheap-proposes",
        "controlled_contrast_note": (
            "Only R_full_verify edge verification is varied; its unrelated "
            "tau_disamb=1 policy is intentionally not copied."
        ),
    },
}
UUID_NAMESPACE = uuid.UUID("ee58e5d8-c710-5ef5-8271-4f50c8f75ac2")


def stable_candidate_uuid(evidence_id: str) -> uuid.UUID:
    return uuid.uuid5(UUID_NAMESPACE, evidence_id)


def _ids_hash(values: Sequence[str]) -> str:
    return canonical_sha256(sorted(map(str, values)))


def _node_map(nodes: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(row["node_id"]): row for row in nodes}


def build_selector_cases(evidence_records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build arrival-time union cases without accepting gold/manual inputs."""

    cases: list[dict[str, Any]] = []
    for record in sorted(evidence_records, key=lambda row: str(row["episode_id"])):
        episode_id = str(record["episode_id"])
        nodes = list(record["nodes"])
        node_by_id = _node_map(nodes)
        mechanism = generate_turn_candidates(episode_id, nodes)
        alignment_by_node = {
            str(row["node_id"]): row for row in mechanism["alignments"]
        }
        aligned_by_evidence: dict[str, dict[str, Any]] = {}
        for row in mechanism["alignments"]:
            evidence_id = row.get("evidence_id")
            if not evidence_id:
                continue
            bucket = aligned_by_evidence.setdefault(
                str(evidence_id),
                {
                    "evidence_id": str(evidence_id),
                    "text": str(row["text"]),
                    "node_ids": [],
                    "session_index": int(row["session_index"]),
                    "turn_index": int(row["turn_index"]),
                },
            )
            bucket["node_ids"].append(str(row["node_id"]))
        for bucket in aligned_by_evidence.values():
            bucket["node_ids"] = sorted(set(bucket["node_ids"]))

        same_incoming: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for candidate in mechanism["candidates"]:
            same_incoming[str(candidate["target_evidence_id"])].append(candidate)
        trace_by_node = {
            str(row["node_id"]): row for row in record["candidate_traces"]
        }

        # Aliases of one target proposition produce exactly one selector case.
        for target_evidence_id, target in sorted(aligned_by_evidence.items()):
            target_aliases = target["node_ids"]
            traces = [trace_by_node[node_id] for node_id in target_aliases]
            snapshot_ids = sorted(
                {
                    str(candidate_id)
                    for trace in traces
                    for candidate_id in trace["candidate_snapshot_ids"]
                }
            )
            history_by_evidence: dict[str, dict[str, Any]] = {}
            for node_id in snapshot_ids:
                node = node_by_id.get(node_id)
                if node is None:
                    raise ValueError(f"snapshot node {node_id} missing in {episode_id}")
                alignment = alignment_by_node[node_id]
                # Ambiguous evidence cannot receive a stable proposition identity.
                if alignment.get("status") != "aligned":
                    continue
                evidence_id = str(alignment["evidence_id"])
                bucket = history_by_evidence.setdefault(
                    evidence_id,
                    {
                        "evidence_id": evidence_id,
                        "text": str(node["text"]),
                        "node_ids": [],
                        "source_origin": "history_snapshot",
                        "session_index": int(node["session_index"]),
                        "turn_index": int(alignment["turn_index"]),
                    },
                )
                bucket["node_ids"].append(node_id)
            for bucket in history_by_evidence.values():
                bucket["node_ids"] = sorted(set(bucket["node_ids"]))

            same_by_evidence: dict[str, dict[str, Any]] = {}
            for edge in same_incoming.get(target_evidence_id, ()):
                evidence_id = str(edge["source_evidence_id"])
                same_by_evidence[evidence_id] = {
                    "evidence_id": evidence_id,
                    "text": str(edge["source_text"]),
                    "node_ids": sorted(map(str, edge["source_node_ids"])),
                    "source_origin": "same_session_turn_envelope",
                    "session_index": int(edge["session_index"]),
                    "turn_index": int(edge["source_turn_index"]),
                    "envelope_edge_id": str(edge["edge_id"]),
                }
            overlap = set(history_by_evidence) & set(same_by_evidence)
            if overlap:
                raise ValueError(f"history/same-session overlap in {episode_id}: {overlap}")
            sources = [
                *history_by_evidence.values(),
                *same_by_evidence.values(),
            ]
            sources.sort(key=lambda row: (row["source_origin"], row["evidence_id"]))
            if any(
                row["source_origin"] == "same_session_turn_envelope"
                and (
                    row["session_index"] != target["session_index"]
                    or row["turn_index"] >= target["turn_index"]
                )
                for row in sources
            ):
                raise ValueError("same-session temporal invariant failed")
            if any(
                row["source_origin"] == "history_snapshot"
                and row["session_index"] >= target["session_index"]
                for row in sources
            ):
                raise ValueError("history snapshot contains non-earlier session")
            source_ids = [row["evidence_id"] for row in sources]
            case = {
                "episode_id": episode_id,
                "target_evidence_id": target_evidence_id,
                "target_text": target["text"],
                "target_node_ids": target_aliases,
                "target_session_index": target["session_index"],
                "target_turn_index": target["turn_index"],
                "candidates": sources,
                "history_candidate_count": len(history_by_evidence),
                "same_session_candidate_count": len(same_by_evidence),
                "candidate_identity_hash": _ids_hash(source_ids),
            }
            case["input_hash"] = canonical_sha256(case)
            cases.append(case)
    return sorted(cases, key=lambda row: (row["episode_id"], row["target_evidence_id"]))


def selector_case_key(case: Mapping[str, Any], arm: str) -> str:
    return f"{case['episode_id']}|{arm}|{case['target_evidence_id']}"


class AuditedChatClient(BaseChatClient):
    """Capture exact prompts and real provider usage; never estimate missing usage."""

    def __init__(self, inner: BaseChatClient, model: str, provider: str):
        self.inner = inner
        self.model = model
        self.provider = provider
        self.calls: list[dict[str, Any]] = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        started = time.perf_counter()
        answer = await self.inner.complete(prompt, max_tokens=max_tokens)
        elapsed = time.perf_counter() - started
        usage = getattr(self.inner, "last_usage", None)
        clean_usage = None
        if isinstance(usage, dict) and usage.get("total_tokens"):
            clean_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
            }
        self.calls.append(
            {
                "model": self.model,
                "provider": self.provider,
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "max_tokens": max_tokens,
                "answer": answer,
                "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
                "usage": clean_usage,
                "cost_incomplete": clean_usage is None,
                "latency_seconds": elapsed,
            }
        )
        return answer


async def run_selector_case(
    case: Mapping[str, Any],
    arm: str,
    *,
    cheap: DependencyDiscoveryService,
    strong: DependencyDiscoveryService,
    cheap_audit: AuditedChatClient,
    strong_audit: AuditedChatClient,
) -> dict[str, Any]:
    """Call the actual candidate and edge routers; no copied prompt/parser."""

    if arm not in ARM_CONFIGS:
        raise ValueError(f"unknown arm {arm!r}")
    config = ARM_CONFIGS[arm]
    candidates = [
        CandidateFact(
            id=stable_candidate_uuid(str(row["evidence_id"])),
            text=str(row["text"]),
            embedding=None,
        )
        for row in case["candidates"]
    ]
    by_uuid = {
        str(stable_candidate_uuid(str(row["evidence_id"]))): row
        for row in case["candidates"]
    }
    route = route_candidate_selection(
        str(case["target_text"]),
        None,
        candidates,
        **config["candidate_route"],
    )
    expected = {item.id for item in candidates}
    valid_tier = route.tier == "full" if candidates else route.tier == "block"
    if not valid_tier or {item.id for item in route.candidates} != expected:
        raise RuntimeError("full candidate route did not preserve the union")
    cheap_before, strong_before = len(cheap_audit.calls), len(strong_audit.calls)
    edge = await route_edge_discovery(
        str(case["target_text"]),
        route.candidates,
        config["edge_route"]["tau"],
        cheap=cheap,
        strong=strong,
        lam=config["edge_route"]["lam"],
    )
    selected = []
    for selected_id in edge.sources:
        source = by_uuid.get(str(selected_id))
        if source is None:
            raise RuntimeError("selector returned an unknown source UUID")
        selected.append(
            {
                "source_evidence_id": source["evidence_id"],
                "source_node_ids": source["node_ids"],
                "source_origin": source["source_origin"],
                "source_text": source["text"],
            }
        )
    calls = [
        *cheap_audit.calls[cheap_before:],
        *strong_audit.calls[strong_before:],
    ]
    return {
        "case_key": selector_case_key(case, arm),
        "episode_id": case["episode_id"],
        "arm": arm,
        "target_evidence_id": case["target_evidence_id"],
        "target_node_ids": case["target_node_ids"],
        "target_text": case["target_text"],
        "input_hash": case["input_hash"],
        "candidate_identity_hash": case["candidate_identity_hash"],
        "candidate_tier": route.tier,
        "candidate_count": len(route.candidates),
        "history_candidate_count": case["history_candidate_count"],
        "same_session_candidate_count": case["same_session_candidate_count"],
        "edge_tier": edge.tier,
        "selected_sources": selected,
        "selected_source_count": len(selected),
        "model_calls": calls,
        "cost_incomplete": any(row["cost_incomplete"] for row in calls),
    }


def score_successes(
    successes: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    manual_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Scoring-only join; gold/manual inputs are absent from routing interfaces."""

    case_by_target_node: dict[tuple[str, str], Mapping[str, Any]] = {}
    for case in cases:
        for node_id in case["target_node_ids"]:
            case_by_target_node[(str(case["episode_id"]), str(node_id))] = case
    success_by_key = {str(row["case_key"]): row for row in successes}
    manual_by_edge = {
        str(row["semantic_edge_id"]): row for row in manual_records
    }
    edge_results = []
    for decision in decisions:
        episode_id = str(decision["episode_id"])
        target_node = str(decision["reviewed_valid_target_node_id"])
        source_node = str(decision["reviewed_valid_source_node_id"])
        case = case_by_target_node[(episode_id, target_node)]
        for arm in ARM_CONFIGS:
            success = success_by_key.get(selector_case_key(case, arm))
            selected = success["selected_sources"] if success else []
            recovered = any(source_node in row["source_node_ids"] for row in selected)
            substring_ids = set(
                map(
                    str,
                    manual_by_edge[str(decision["semantic_edge_id"])].get(
                        "substring_only_source_match_node_ids", ()
                    ),
                )
            )
            identity_violations = [
                row
                for row in selected
                if substring_ids.intersection(map(str, row["source_node_ids"]))
            ]
            edge_results.append(
                {
                    "semantic_edge_id": decision["semantic_edge_id"],
                    "episode_id": episode_id,
                    "arm": arm,
                    "case_key": selector_case_key(case, arm),
                    "case_success": success is not None,
                    "routed_visible": any(
                        source_node in row["node_ids"] for row in case["candidates"]
                    ),
                    "selected": recovered,
                    "source_identity_violation_count": len(identity_violations),
                    "source_identity_violation_evidence_ids": sorted(
                        row["source_evidence_id"] for row in identity_violations
                    ),
                }
            )
    arm_summary = {}
    for arm in ARM_CONFIGS:
        rows = [row for row in edge_results if row["arm"] == arm]
        episode_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            episode_groups[row["episode_id"]].append(row)
        arm_summary[arm] = {
            "gold_edge_count": len(rows),
            "candidate_routing_visible_count": sum(row["routed_visible"] for row in rows),
            "selected_gold_edge_count": sum(row["selected"] for row in rows),
            "episodes_all_gold_selected_count": sum(
                all(row["selected"] for row in group)
                for group in episode_groups.values()
            ),
            "episode_graph_miss_count": sum(
                not all(row["selected"] for row in group)
                for group in episode_groups.values()
            ),
            "source_identity_violation_count": sum(
                row["source_identity_violation_count"] for row in rows
            ),
        }
    return {"edge_results": edge_results, "arms": arm_summary}


def summarize_outputs(
    successes: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    case_by_key = {
        selector_case_key(case, arm): case
        for case in cases
        for arm in ARM_CONFIGS
    }
    result: dict[str, Any] = {}
    for arm in ARM_CONFIGS:
        rows = [row for row in successes if row["arm"] == arm]
        calls = [call for row in rows for call in row["model_calls"]]
        usages = [call["usage"] for call in calls if call["usage"] is not None]
        latencies = sorted(float(call["latency_seconds"]) for call in calls)
        selected = [source for row in rows for source in row["selected_sources"]]
        expected = [key for key in case_by_key if f"|{arm}|" in key]
        result[arm] = {
            "expected_case_count": len(expected),
            "successful_case_count": len(rows),
            "input_history_candidate_count": sum(
                int(row["history_candidate_count"]) for row in rows
            ),
            "input_same_session_candidate_count": sum(
                int(row["same_session_candidate_count"]) for row in rows
            ),
            "output_edge_count": len(selected),
            "output_history_edge_count": sum(
                row["source_origin"] == "history_snapshot" for row in selected
            ),
            "output_same_session_edge_count": sum(
                row["source_origin"] == "same_session_turn_envelope" for row in selected
            ),
            "same_session_compression_rate": (
                1
                - sum(
                    row["source_origin"] == "same_session_turn_envelope"
                    for row in selected
                )
                / sum(int(row["same_session_candidate_count"]) for row in rows)
                if sum(int(row["same_session_candidate_count"]) for row in rows)
                else None
            ),
            "cheap_call_count": sum(call["model"] == "gpt-4o-mini" for call in calls),
            "strong_call_count": sum(call["model"] == "gpt-4.1-mini" for call in calls),
            "prompt_tokens": sum(int(row["prompt_tokens"]) for row in usages),
            "completion_tokens": sum(int(row["completion_tokens"]) for row in usages),
            "cost_incomplete_call_count": sum(
                call["cost_incomplete"] for call in calls
            ),
            "latency_p50_seconds": _percentile(latencies, 0.50),
            "latency_p95_seconds": _percentile(latencies, 0.95),
        }
    return result


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * q)))
    return values[index]


def build_review_packet(
    successes: Sequence[Mapping[str, Any]],
    gold_selected_pairs: set[tuple[str, str, str]],
    *,
    sample_per_episode_arm: int = 2,
    seed: str = "p1-selector-intervention-review-v1",
) -> dict[str, Any]:
    rows = []
    for success in successes:
        for source in success["selected_sources"]:
            identity = (
                str(success["arm"]),
                str(source["source_evidence_id"]),
                str(success["target_evidence_id"]),
            )
            if identity in gold_selected_pairs:
                continue
            rows.append(
                {
                    "edge_id": canonical_sha256(identity),
                    "episode_id": success["episode_id"],
                    "arm": success["arm"],
                    "source_evidence_id": source["source_evidence_id"],
                    "source_text": source["source_text"],
                    "source_origin": source["source_origin"],
                    "target_evidence_id": success["target_evidence_id"],
                    "target_text": success["target_text"],
                }
            )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["episode_id"], row["arm"])].append(row)
    sample = []
    for key in sorted(grouped):
        ranked = sorted(
            grouped[key],
            key=lambda row: canonical_sha256({"seed": seed, "edge_id": row["edge_id"]}),
        )
        sample.extend(ranked[:sample_per_episode_arm])
    packet = {
        "schema_version": "p1-selector-non-gold-review-packet-v1",
        "sampling": {
            "method": "episode-arm-stratified-hash-rank",
            "uses_scores": False,
            "seed": seed,
            "sample_per_episode_arm": sample_per_episode_arm,
        },
        "labels": ["yes", "no", "ambiguous"],
        "non_gold_unmatched_output_count": len(rows),
        "sample_count": len(sample),
        "sample": sample,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet
