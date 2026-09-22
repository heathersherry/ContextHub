"""Session-replay ingest with arrival-time candidate snapshots.

Benchmark-only. Delayed consolidation must use the snapshot taken when the
session arrived; workers must not read "whatever is in the DB now".
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from uuid import UUID

from contexthub.services.cascade_router import (
    _PRONOUN,
    route_candidate_selection,
    route_disambiguation,
    route_edge_discovery,
)
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from integrations.memebench.chronological_policy import (
    BuildPlan,
    ConsolidationSchedule,
    PendingFact,
    edge_discovery_args,
    registered_build_plans,
)
from integrations.memebench.ingest import (
    IngestedGraph,
    _proper_names,
    _session_text,
    _split_evidence,
    edge_pr_raw,
    embed_all,
    insert_memory,
)
from integrations.memebench.loader import CascadeCase


Clock = Callable[[], float]


@dataclass
class ChronologicalIngestResult:
    graph: IngestedGraph
    schedule: ConsolidationSchedule
    plan: BuildPlan
    tier_mix: dict
    timings: dict
    pending_audit: list[dict]
    node_audit: list[dict]
    envelope_audit: list[dict]
    audit_trace: dict[str, Any] | None


def _candidate_key(fact: CandidateFact) -> str:
    return str(fact.id)


def _ids_hash(ids: Sequence[str]) -> str:
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()


def _source_evidence(sess: dict[str, Any], node_text: str) -> dict[str, Any]:
    """Best-effort benchmark-only provenance for an extracted fact."""

    turns = [
        {
            "turn_index": index,
            "role": str(turn.get("role", "")),
            "content": str(turn.get("content", "")),
        }
        for index, turn in enumerate(sess.get("conversation", []))
        if turn.get("content")
    ]
    exact = [
        (turn, turn["content"].find(node_text))
        for turn in turns
        if node_text and node_text in turn["content"]
    ]
    if exact:
        turn, start = exact[0]
        match = {
            "method": "exact",
            "turn_index": turn["turn_index"],
            "start": start,
            "end": start + len(node_text),
            "quote": turn["content"][start : start + len(node_text)],
            "token_overlap": 1.0,
        }
    else:
        node_tokens = set(re.findall(r"\w+", node_text.casefold()))
        scored = []
        for turn in turns:
            turn_tokens = set(re.findall(r"\w+", turn["content"].casefold()))
            score = len(node_tokens & turn_tokens) / len(node_tokens) if node_tokens else 0.0
            scored.append((score, -int(turn["turn_index"]), turn))
        if scored:
            score, _, turn = max(scored)
            match = {
                "method": "token_overlap",
                "turn_index": turn["turn_index"],
                "start": None,
                "end": None,
                "quote": turn["content"],
                "token_overlap": score,
            }
        else:
            match = {
                "method": "none",
                "turn_index": None,
                "start": None,
                "end": None,
                "quote": "",
                "token_overlap": 0.0,
            }
    return {
        "original_session_id": str(sess.get("session_id", "")),
        "original_turns": turns,
        "source_span": match,
    }


def envelope_for_snapshot(
    new_text: str,
    new_emb: list[float] | None,
    snapshot: Sequence[CandidateFact],
    plans: Sequence[BuildPlan] | None = None,
) -> tuple[tuple[CandidateFact, ...], str]:
    """H^max = union of candidates any frozen plan might inspect.

    Candidate selection is LLM-free. The envelope is recorded for audit and
    gold scoring only; it is never passed to a path-risk planner.
    """

    menu = list(plans) if plans is not None else list(registered_build_plans().values())
    by_id: dict[UUID, CandidateFact] = {}
    for plan in menu:
        routed = route_candidate_selection(
            new_text,
            new_emb,
            list(snapshot),
            plan.tau_cand,
            k=plan.k,
            recency=plan.recency,
        )
        for candidate in routed.candidates:
            by_id[candidate.id] = candidate
    envelope = tuple(by_id[key] for key in sorted(by_id, key=str))
    digest = _ids_hash([_candidate_key(item) for item in envelope])
    return envelope, digest


def _gold_parents_in_envelope(
    case: CascadeCase,
    node_text: str,
    envelope: Sequence[CandidateFact],
) -> dict[str, Any]:
    """Scoring-side only: how many gold parents sit inside H^max."""

    def entities_of(text: str) -> set[str]:
        from integrations.memebench.ingest import _norm

        tn = _norm(text)
        out: set[str] = set()
        for name, ent in case.entities.items():
            before = _norm(ent.before)
            if before and before in tn:
                out.add(name)
        return out

    node_ents = entities_of(node_text)
    envelope_ents: set[str] = set()
    for candidate in envelope:
        envelope_ents.update(entities_of(candidate.text))
    gold_parents = {edge.source for edge in case.edges if edge.target in node_ents}
    hit = gold_parents & envelope_ents
    return {
        "n_gold_parents": len(gold_parents),
        "n_gold_parents_in_envelope": len(hit),
        "gold_parent_recall": (
            len(hit) / len(gold_parents) if gold_parents else 1.0
        ),
    }


def _entity_pool_from_snapshot(snapshot: Sequence[CandidateFact]) -> list[str]:
    pool: list[str] = []
    seen: set[str] = set()
    for candidate in snapshot:
        for name in _proper_names(candidate.text):
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                pool.append(name)
    return pool


class _PendingQueue:
    def __init__(self) -> None:
        self._items: deque[PendingFact] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def enqueue(self, fact: PendingFact) -> None:
        self._items.append(fact)

    def pop_ready(self, n: int | None = None) -> list[PendingFact]:
        if n is None or n >= len(self._items):
            ready = list(self._items)
            self._items.clear()
            return ready
        ready = [self._items.popleft() for _ in range(n)]
        return ready

    def empty(self) -> bool:
        return not self._items


def _should_flush(
    schedule: ConsolidationSchedule,
    *,
    sessions_since_flush: int,
    at_end: bool,
) -> bool:
    if at_end:
        return True
    if schedule.mode in {"sync-inline", "async-each-session"}:
        return True
    if schedule.mode == "async-microbatch":
        k = schedule.batch_size if schedule.batch_size > 0 else 5
        return sessions_since_flush >= k
    if schedule.mode == "backfill":
        return False
    raise ValueError(f"unknown consolidation mode {schedule.mode!r}")


async def _consolidate_one(
    db,
    pending: PendingFact,
    *,
    plan: BuildPlan,
    disamb_cheap: DependencyDiscoveryService,
    disamb_strong: DependencyDiscoveryService,
    edge_cheap: DependencyDiscoveryService,
    edge_strong: DependencyDiscoveryService,
    case: CascadeCase,
    graph: IngestedGraph,
    tiers: dict[str, Counter],
    envelope_audit: list[dict],
    node_audit: list[dict],
    audit_consolidations: list[dict] | None,
    clock: Clock,
) -> dict[str, Any]:
    started = clock()
    snapshot = pending.candidate_snapshot
    envelope, envelope_hash = envelope_for_snapshot(
        pending.text, pending.embedding, snapshot
    )
    entity_pool = _entity_pool_from_snapshot(snapshot)
    mention = _PRONOUN.search(pending.text)
    if mention and entity_pool:
        dis = await route_disambiguation(
            mention.group(0),
            pending.text,
            entity_pool,
            plan.tau_disamb,
            llm=disamb_cheap,
            strong=disamb_strong,
        )
        tiers["disamb"][dis.tier] += 1
        judge_text = (
            f"{pending.text} ({mention.group(0)} = {dis.resolution})"
            if dis.resolution
            else pending.text
        )
        disamb_tier = dis.tier
    else:
        tiers["disamb"]["none"] += 1
        judge_text = pending.text
        disamb_tier = "none"

    sources: list[UUID] = []
    routed_candidates: list[CandidateFact] = []
    cand_tier = "block"
    edge_tier = "cheap_none"
    if snapshot:
        cand = route_candidate_selection(
            judge_text,
            pending.embedding,
            list(snapshot),
            plan.tau_cand,
            k=plan.k,
            recency=plan.recency,
        )
        tiers["cand"][cand.tier] += 1
        cand_tier = cand.tier
        routed_candidates = list(cand.candidates)
        tau, lam = edge_discovery_args(plan)
        edge = await route_edge_discovery(
            judge_text,
            cand.candidates,
            tau,
            cheap=edge_cheap,
            strong=edge_strong,
            lam=lam,
        )
        tiers["edge"][edge.tier] += 1
        edge_tier = edge.tier
        sources = list(edge.sources)
    else:
        tiers["cand"]["empty"] += 1
        tiers["edge"]["cheap_none"] += 1

    # H^max is the union of candidates any frozen plan could inspect. The
    # registered-menu calculation is LLM-free and uses the raw extracted text.
    # A policy's already-computed disambiguation may change lexical blocking, so
    # include its actual routed set as part of that same union without making an
    # extra model call.
    by_envelope_id = {item.id: item for item in envelope}
    augmented = False
    for item in routed_candidates:
        if item.id not in by_envelope_id:
            by_envelope_id[item.id] = item
            augmented = True
    if augmented:
        envelope = tuple(
            by_envelope_id[key] for key in sorted(by_envelope_id, key=str)
        )
        envelope_hash = _ids_hash([_candidate_key(item) for item in envelope])

    gold = _gold_parents_in_envelope(case, pending.text, envelope)
    envelope_audit.append(
        {
            "node_id": str(pending.node_id),
            "session_index": pending.session_index,
            "envelope_size": len(envelope),
            "envelope_hash": envelope_hash,
            "snapshot_size": len(snapshot),
            **gold,
        }
    )

    persisted_sources: list[UUID] = []
    for src_id in sources:
        await db.execute(
            """
            INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
            VALUES ($1, $2, 'derived_from')
            ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
            """,
            pending.node_id,
            src_id,
        )
        graph.persisted_edges.add((src_id, pending.node_id))
        persisted_sources.append(src_id)

    elapsed = clock() - started
    wait = max(0.0, started - pending.enqueued_at)
    record = {
        "node_id": str(pending.node_id),
        "session_index": pending.session_index,
        "snapshot_ids": [str(item.id) for item in snapshot],
        "n_sources": len(sources),
        "disamb_tier": disamb_tier,
        "cand_tier": cand_tier,
        "edge_tier": edge_tier,
        "pending_wait_seconds": wait,
        "consolidation_seconds": elapsed,
    }
    node_audit.append(record)
    if audit_consolidations is not None:
        snapshot_ids = [str(item.id) for item in snapshot]
        hmax_ids = [str(item.id) for item in envelope]
        audit_consolidations.append(
            {
                "node_id": str(pending.node_id),
                "session_index": pending.session_index,
                "candidate_snapshot_ids": snapshot_ids,
                "candidate_snapshot_hash": _ids_hash(snapshot_ids),
                "candidate_snapshot_size": len(snapshot_ids),
                "hmax_candidate_ids": hmax_ids,
                "hmax_hash": envelope_hash,
                "hmax_size": len(hmax_ids),
                "hmax_augmented_with_actual_route": augmented,
                "routed_candidate_ids": [
                    str(item.id) for item in routed_candidates
                ],
                "candidate_tier": cand_tier,
                "edge_tier": edge_tier,
                "final_selected_source_ids": [str(item) for item in sources],
                "persisted_source_ids": [
                    str(item) for item in persisted_sources
                ],
            }
        )
    return record


async def ingest_case_chronological(
    db,
    case: CascadeCase,
    account_id: str,
    embed_batch,
    *,
    extractor: ConversationExtractionService,
    disamb_cheap: DependencyDiscoveryService,
    disamb_strong: DependencyDiscoveryService,
    edge_cheap: DependencyDiscoveryService,
    edge_strong: DependencyDiscoveryService,
    plan: BuildPlan,
    schedule: ConsolidationSchedule,
    audit_trace: bool = False,
    clock: Clock = time.perf_counter,
    consolidation_hook: Callable[[list[PendingFact]], None] | None = None,
) -> ChronologicalIngestResult:
    """Replay pre-change sessions in original order and consolidate by schedule."""

    graph = IngestedGraph(account_id=account_id, root_id=None)
    pre_sessions, _ = _split_evidence(case)
    pending = _PendingQueue()
    published_pool: list[CandidateFact] = []
    tiers = {"disamb": Counter(), "cand": Counter(), "edge": Counter()}
    pending_audit: list[dict] = []
    node_audit: list[dict] = []
    envelope_audit: list[dict] = []
    extracted_node_trace: list[dict] | None = [] if audit_trace else None
    consolidation_trace: list[dict] | None = [] if audit_trace else None
    job_sizes: list[int] = []
    session_fact_counts: list[int] = []
    max_pending = 0
    n_jobs = 0
    fast_path_seconds = 0.0
    consolidation_seconds = 0.0
    pending_wait_seconds = 0.0
    sessions_since_flush = 0

    async def flush(*, count_on_critical_path: bool) -> None:
        nonlocal n_jobs, consolidation_seconds, pending_wait_seconds, max_pending
        nonlocal fast_path_seconds
        if pending.empty():
            return
        ready = pending.pop_ready()
        started = clock()
        if consolidation_hook is not None:
            consolidation_hook(ready)
        n_jobs += 1
        job_sizes.append(len(ready))
        for item in ready:
            record = await _consolidate_one(
                db,
                item,
                plan=plan,
                disamb_cheap=disamb_cheap,
                disamb_strong=disamb_strong,
                edge_cheap=edge_cheap,
                edge_strong=edge_strong,
                case=case,
                graph=graph,
                tiers=tiers,
                envelope_audit=envelope_audit,
                node_audit=node_audit,
                audit_consolidations=consolidation_trace,
                clock=clock,
            )
            pending_wait_seconds += float(record["pending_wait_seconds"])
        elapsed = clock() - started
        consolidation_seconds += elapsed
        if count_on_critical_path:
            fast_path_seconds += elapsed

    for session_index, sess in enumerate(pre_sessions):
        snapshot = tuple(published_pool)
        t0 = clock()
        facts = await extractor.extract(_session_text(sess))
        texts = [fact.text for fact in facts if fact.text and fact.text.strip()]
        embeddings = await embed_all(embed_batch, texts) if texts else []
        session_ids: list[UUID] = []
        for text, emb in zip(texts, embeddings):
            node_id = await insert_memory(db, account_id, "fact", text, emb)
            graph.inserted_nodes.append((node_id, text))
            session_ids.append(node_id)
            if extracted_node_trace is not None:
                extracted_node_trace.append(
                    {
                        "node_id": str(node_id),
                        "text": text,
                        "session_index": session_index,
                        "embedding_present": emb is not None,
                        **_source_evidence(sess, text),
                    }
                )
            item = PendingFact(
                node_id=node_id,
                text=text,
                embedding=list(emb) if emb is not None else None,
                session_index=session_index,
                candidate_snapshot=snapshot,
                enqueued_at=clock(),
            )
            pending.enqueue(item)
            pending_audit.append(
                {
                    "node_id": str(node_id),
                    "session_index": session_index,
                    "snapshot_ids": [str(c.id) for c in snapshot],
                    "snapshot_size": len(snapshot),
                    "enqueued_at": item.enqueued_at,
                }
            )
        max_pending = max(max_pending, len(pending))
        session_fact_counts.append(len(texts))
        for text, emb, node_id in zip(texts, embeddings, session_ids):
            published_pool.append(
                CandidateFact(id=node_id, text=text, embedding=emb)
            )
        fast_path_seconds += clock() - t0
        sessions_since_flush += 1
        if _should_flush(
            schedule, sessions_since_flush=sessions_since_flush, at_end=False
        ):
            await flush(count_on_critical_path=schedule.mode == "sync-inline")
            sessions_since_flush = 0

    await flush(count_on_critical_path=schedule.mode == "sync-inline")

    timings = {
        "fast_path_seconds": fast_path_seconds,
        "consolidation_seconds": consolidation_seconds,
        "pending_wait_seconds": pending_wait_seconds,
        "max_pending_nodes": max_pending,
        "n_consolidation_jobs": n_jobs,
        "job_node_counts": job_sizes,
        "session_fact_counts": session_fact_counts,
        "n_sessions": len(pre_sessions),
        "pending_remaining": len(pending),
    }
    return ChronologicalIngestResult(
        graph=graph,
        schedule=schedule,
        plan=plan,
        tier_mix={key: dict(value) for key, value in tiers.items()},
        timings=timings,
        pending_audit=pending_audit,
        node_audit=node_audit,
        envelope_audit=envelope_audit,
        audit_trace=(
            {
                "nodes": extracted_node_trace,
                "consolidations": consolidation_trace,
            }
            if audit_trace
            else None
        ),
    )


def chronological_edge_scores(
    case: CascadeCase, result: ChronologicalIngestResult
) -> dict[str, float | int]:
    return edge_pr_raw(case, result.graph)
