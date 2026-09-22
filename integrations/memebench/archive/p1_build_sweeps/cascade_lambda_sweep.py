"""P1 — build-side pricing cascade λ-frontier (variable 3, edge discovery).

Companion to cascade_sweep.py. That script sweeps the fixed floor threshold τ
(escalate iff confidence < τ) — cost-BLIND. This script sweeps the escalation
PRICE λ: escalate iff conf < p_min (floor) OR (1 − conf) / c ≥ λ (worthwhile),
where c is the token cost of the strong call. Folding c into the decision lets
the router prefer cheap-to-escalate low-confidence edges first.

Purpose: test empirically whether pricing by (1 − p)/c beats routing by (1 − p)
alone (the τ sweep). λ = +inf recovers the pure-floor rule, so the λ=inf point
should coincide with the τ = p_min endpoint of cascade_sweep.py (self-check).

Per edge we record (q = 1 − conf, c_est, c_actual, tier, escalated) so the offline
fractional-knapsack frontier can be reconstructed and the integer-vs-fractional
gap (appendix F.2) measured. c_actual = real strong-tier tokens for that edge,
via a CountingChatClient snapshot around each edge-routing call.

Usage (smoke first with --limit 5):
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.cascade_lambda_sweep \
        --hop 1 --limit 5 --provider openlux \
        --cheap-model gpt-4o-mini --strong-model gpt-5.6-sol \
        --lambdas inf 5e-4 2e-4 1e-4 5e-5 2e-5 0 --p-min 0.0 \
        --data /Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json \
        --out integrations/memebench/runs/cascade_lambda_hop1.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.cascade_router import (
    _edge_cost_estimate,
    route_candidate_selection,
    route_edge_discovery,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from integrations.memebench.candidate_selection_sweep import (
    _filler_texts,
    _gold_specs,
)
from integrations.memebench.cost import CountingChatClient
from integrations.memebench.ingest import (
    IngestedGraph,
    _embed_all,
    _insert_memory,
    edge_pr,
)
from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    CascadeCase,
    extract_cascade_cases,
    load_episodes,
)
from integrations.memebench.systems import (
    DEFAULT_PROVIDERS_PATH,
    build_system,
    load_provider,
)


@dataclass
class EdgeRecord:
    """One edge-discovery decision (only nodes that reached the cheap tier)."""
    episode_id: str
    conf: float          # p — cheap-tier structural confidence
    q: float             # 1 − p — proxy quality bought by escalating
    c_est: float         # decision-time token-cost estimate
    c_actual: int        # real strong-tier tokens (0 if not escalated)
    tier: str
    escalated: bool


async def ingest_case_lambda(
    db,
    case: CascadeCase,
    account: str,
    embed_batch,
    cheap: DependencyDiscoveryService,
    strong: DependencyDiscoveryService,
    strong_chat: CountingChatClient,
    p_min: float,
    lam: float,
    max_tokens: int,
    max_filler: int,
    k: int,
) -> tuple[IngestedGraph, Counter, list[EdgeRecord]]:
    """Like cascade_sweep.ingest_case_cascade but pricing-aware + per-edge records.

    tau is passed as p_min (the floor half of the rule); lam turns on the pricing
    half inside route_edge_discovery. Each edge-routing call is bracketed by a
    strong-token snapshot to attribute c_actual to that single edge.
    """
    graph = IngestedGraph(account_id=account, root_id=None)
    node_by_entity: dict[str, uuid.UUID] = {}

    filler_texts = _filler_texts(case, max_filler)
    gold_specs = _gold_specs(case)

    all_texts = filler_texts + [s[1] for s in gold_specs]
    all_emb = await _embed_all(embed_batch, all_texts)
    filler_emb = all_emb[: len(filler_texts)]
    gold_emb = all_emb[len(filler_texts):]

    pool: list[CandidateFact] = []
    for text, emb in zip(filler_texts, filler_emb):
        fid = await _insert_memory(db, account, "filler", text, emb)
        pool.append(CandidateFact(id=fid, text=text, embedding=emb))

    edge_tiers: Counter = Counter()
    records: list[EdgeRecord] = []
    for (slug, text, role, entity), emb in zip(gold_specs, gold_emb):
        node_id = await _insert_memory(db, account, slug, text, emb)
        graph.node_meta[node_id] = (entity or "", role)
        if role == "root":
            graph.root_id = node_id
            node_by_entity[case.root] = node_id
        elif role == "cur":
            graph.materialized[entity] = node_id
            node_by_entity[entity] = node_id
        elif role == "pre":
            graph.predeclarations[entity] = node_id

        if pool:
            cand = route_candidate_selection(text, emb, pool, p_min, k=k)
            s_before = strong_chat.total_tokens
            edge = await route_edge_discovery(
                text, cand.candidates, p_min,
                cheap=cheap, strong=strong, lam=lam, max_tokens=max_tokens,
            )
            c_actual = strong_chat.total_tokens - s_before
            edge_tiers[edge.tier] += 1
            # Only cheap/strong tiers are genuine escalation candidates (regex /
            # cheap_none decline to link with confidence 1.0 and never cost strong).
            if edge.tier in ("cheap", "strong"):
                c_est = _edge_cost_estimate(text, cand.candidates, max_tokens)
                records.append(EdgeRecord(
                    episode_id=case.episode_id,
                    conf=edge.confidence, q=1.0 - edge.confidence,
                    c_est=c_est, c_actual=c_actual,
                    tier=edge.tier, escalated=(edge.tier == "strong"),
                ))
            for src_id in edge.sources:
                await db.execute(
                    """
                    INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
                    VALUES ($1, $2, 'derived_from')
                    ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
                    """,
                    node_id, src_id,
                )
                graph.persisted_edges.add((src_id, node_id))
        pool.append(CandidateFact(id=node_id, text=text, embedding=emb))

    return graph, edge_tiers, records


@dataclass
class CaseResult:
    episode_id: str
    n_gold: int
    n_pred: int
    n_tp: int
    precision: float
    recall: float
    cheap_tokens: int
    strong_tokens: int


@dataclass
class LambdaResult:
    lam: float
    p_min: float
    n_cases: int = 0
    n_error: int = 0
    micro_precision: float = 0.0
    micro_recall: float = 0.0
    macro_precision: float = 0.0
    macro_recall: float = 0.0
    cheap_tokens: int = 0
    strong_tokens: int = 0
    total_tokens: int = 0
    tokens_per_case: float = 0.0
    n_escalated: int = 0        # edges sent to strong
    sum_q_escalated: float = 0.0  # Σ(1−p) over escalated edges — proxy quality bought
    edge_tier_mix: dict = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)
    edges: list[EdgeRecord] = field(default_factory=list)


async def run_lambda(system, prov, cheap_model, strong_model, lam, p_min,
                     max_tokens, cases, max_filler, k, ckpt: Path,
                     done_cases: dict[str, dict]) -> LambdaResult:
    """Ingest cases at a fixed λ, skipping ones already in the checkpoint.

    Each case is independent: on success it is appended to the checkpoint (JSONL)
    immediately, so a disconnect loses at most the case in flight. done_cases holds
    {episode_id: {case, edges, tier_mix}} already completed for THIS λ."""
    cheap_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=cheap_model)
    )
    strong_chat = CountingChatClient(
        OpenAIChatClient(api_key=prov["api_key"], base_url=prov["base_url"], model=strong_model)
    )
    cheap = DependencyDiscoveryService(cheap_chat)
    strong = DependencyDiscoveryService(strong_chat)
    embed_batch = system.embedding.embed_batch

    async with system.pool.acquire() as conn:
        await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    res = LambdaResult(lam=lam, p_min=p_min)
    edge_mix: Counter = Counter()
    tag = _lam_tag(lam)
    # Seed with cases already done for this λ (from a prior interrupted run).
    for rec in done_cases.values():
        res.cases.append(rec["case"])
        res.edges.extend(rec["edges"])
        edge_mix.update(rec["tier_mix"])
    n_resumed = len(done_cases)
    if n_resumed:
        print(f"  [λ={tag}] resuming: {n_resumed} cases already done, skipping them",
              flush=True)

    for i, case in enumerate(cases, 1):
        if case.episode_id in done_cases:
            continue
        account = f"lam-{tag}-{case.episode_id}-{case.target_entity}"[:60]
        try:
            c0, s0 = cheap_chat.total_tokens, strong_chat.total_tokens
            async with system.repo.session(account) as db:
                graph, et, recs = await ingest_case_lambda(
                    db, case, account, embed_batch, cheap, strong, strong_chat,
                    p_min, lam, max_tokens, max_filler, k,
                )
                pr = edge_pr(case, graph)
            ctok = cheap_chat.total_tokens - c0
            stok = strong_chat.total_tokens - s0
            case_res = CaseResult(
                episode_id=case.episode_id,
                n_gold=pr["n_gold"], n_pred=pr["n_pred"], n_tp=pr["n_tp"],
                precision=pr["precision"], recall=pr["recall"],
                cheap_tokens=ctok, strong_tokens=stok,
            )
            edge_mix.update(et)
            res.edges.extend(recs)
            res.cases.append(case_res)
            _append_case(ckpt, tag, case_res, recs, dict(et))  # persist immediately
            print(f"  [λ={tag}] [{i}/{len(cases)}] {case.episode_id}: "
                  f"P={pr['precision']:.2f} R={pr['recall']:.2f} "
                  f"cheap={ctok} strong={stok}", flush=True)
        except Exception as exc:
            res.n_error += 1
            print(f"  [λ={tag}] [{i}/{len(cases)}] {case.episode_id}: "
                  f"ERROR {type(exc).__name__}: {exc}", flush=True)

    await cheap_chat.close()
    await strong_chat.close()
    res.edge_tier_mix = dict(edge_mix)
    return _finalize(res)


def _lam_tag(lam: float) -> str:
    return "inf" if math.isinf(lam) else f"{lam:.0e}"


def _tag_to_lam(tag: str) -> float:
    return float("inf") if tag == "inf" else float(tag)


def _ckpt_path(out: Path) -> Path:
    """Per-case checkpoint (JSONL) sitting beside the final json output."""
    return out.with_suffix(".cases.jsonl")


def _append_case(ckpt: Path, lam_tag: str, case: CaseResult,
                 edges: list[EdgeRecord], tier_mix: dict) -> None:
    """Append one completed (λ, case) to the checkpoint. Cases are independent,
    so a disconnect loses at most the case in flight — everything before persists."""
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "lam_tag": lam_tag,
        "case": asdict(case),
        "edges": [asdict(e) for e in edges],
        "tier_mix": tier_mix,
    }, ensure_ascii=False)
    with ckpt.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _load_ckpt(ckpt: Path) -> dict[str, dict[str, dict]]:
    """Read the checkpoint into {lam_tag: {episode_id: {case, edges, tier_mix}}}.

    Only fully-written lines survive (a torn final line is skipped), so resume is
    safe even if the process died mid-write."""
    done: dict[str, dict[str, dict]] = {}
    if not ckpt.exists():
        return done
    for raw in ckpt.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue  # torn last line from a hard kill — ignore
        tag = rec["lam_tag"]
        case = CaseResult(**rec["case"])
        edges = [EdgeRecord(**e) for e in rec["edges"]]
        done.setdefault(tag, {})[case.episode_id] = {
            "case": case, "edges": edges, "tier_mix": rec.get("tier_mix", {}),
        }
    return done


def _finalize(res: LambdaResult) -> LambdaResult:
    """Compute all derived aggregate fields from res.cases + res.edges."""
    ok = res.cases
    sum_tp = sum(c.n_tp for c in ok)
    sum_pred = sum(c.n_pred for c in ok)
    sum_gold = sum(c.n_gold for c in ok)
    res.n_cases = len(ok)
    res.micro_precision = (sum_tp / sum_pred) if sum_pred else 0.0
    res.micro_recall = (sum_tp / sum_gold) if sum_gold else 0.0
    res.macro_precision = (sum(c.precision for c in ok) / len(ok)) if ok else 0.0
    res.macro_recall = (sum(c.recall for c in ok) / len(ok)) if ok else 0.0
    res.cheap_tokens = sum(c.cheap_tokens for c in ok)
    res.strong_tokens = sum(c.strong_tokens for c in ok)
    res.total_tokens = res.cheap_tokens + res.strong_tokens
    res.tokens_per_case = (res.total_tokens / len(ok)) if ok else 0.0
    res.n_escalated = sum(1 for e in res.edges if e.escalated)
    res.sum_q_escalated = sum(e.q for e in res.edges if e.escalated)
    return res


def _results_from_ckpt(done: dict[str, dict[str, dict]], p_min: float) -> list[LambdaResult]:
    """Aggregate the per-case checkpoint into one finalized LambdaResult per λ."""
    results = []
    for tag, cases_map in done.items():
        res = LambdaResult(lam=_tag_to_lam(tag), p_min=p_min)
        mix: Counter = Counter()
        for rec in cases_map.values():
            res.cases.append(rec["case"])
            res.edges.extend(rec["edges"])
            mix.update(rec["tier_mix"])
        res.edge_tier_mix = dict(mix)
        results.append(_finalize(res))
    results.sort(key=lambda r: (math.inf if math.isinf(r.lam) else r.lam), reverse=True)
    return results


def _write_output(out: Path, args, cases, results: list[LambdaResult]) -> None:
    """Rebuild the final json from finalized per-λ results + offline frontier."""
    escalate_all = min(results, key=lambda r: r.lam) if results else None
    frontier = []
    if escalate_all and escalate_all.edges:
        budgets = sorted({r.strong_tokens for r in results if r.strong_tokens > 0})
        frontier = fractional_knapsack_frontier(escalate_all.edges, budgets)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"provider": args.provider, "cheap_model": args.cheap_model,
         "strong_model": args.strong_model, "hop": args.hop,
         "n_cases": len(cases), "p_min": args.p_min, "max_tokens": args.max_tokens,
         "max_filler": args.max_filler, "k": args.k,
         "results": [asdict(r) for r in results],
         "fractional_frontier": frontier},
        ensure_ascii=False, indent=2), encoding="utf-8")


def fractional_knapsack_frontier(edges: list[EdgeRecord], budgets: list[float]) -> list[dict]:
    """Offline fractional-knapsack OPT: max Σq s.t. Σc ≤ B, per budget B.

    Uses c_actual harvested from the escalate-all run (edges with c_actual > 0).
    Sort by q/c descending, fill greedily, split the last item fractionally.
    """
    items = [(e.q, e.c_actual) for e in edges if e.c_actual > 0 and e.q > 0]
    items.sort(key=lambda qc: qc[0] / qc[1], reverse=True)
    out = []
    for B in budgets:
        spent = 0.0
        gained = 0.0
        for q, c in items:
            if spent + c <= B:
                spent += c
                gained += q
            else:
                frac = max(0.0, (B - spent) / c)
                gained += frac * q
                spent = B
                break
        out.append({"budget": B, "opt_q": gained})
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=None)
    ap.add_argument("--limit", type=int, default=40, help="max cases (0 = all)")
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH))
    ap.add_argument("--provider", default="openlux")
    ap.add_argument("--cheap-model", required=True, help="weak tier; no default: name the model at each run (see run_eval.py)")
    ap.add_argument("--strong-model", required=True, help="strong tier; no default: name the model at each run (see run_eval.py)")
    ap.add_argument("--lambdas", nargs="+", type=float,
                    default=[float("inf"), 5e-4, 2e-4, 1e-4, 5e-5, 2e-5, 0.0],
                    help="escalation prices to sweep (inf = pure floor = E.6 endpoint)")
    ap.add_argument("--p-min", type=float, default=0.0,
                    help="confidence floor (0 isolates the pricing rule's net effect)")
    ap.add_argument("--max-tokens", type=int, default=100,
                    help="output cap used in the c_est estimate")
    ap.add_argument("--k", type=int, default=10, help="embed_topk width (variable 4)")
    ap.add_argument("--max-filler", type=int, default=60)
    ap.add_argument("--out", default="integrations/memebench/runs/cascade_lambda.json")
    args = ap.parse_args()

    system = await build_system(provider_label=args.provider, chat_model=args.cheap_model)
    prov = load_provider(args.provider, DEFAULT_PROVIDERS_PATH)
    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.limit:
        cases = cases[: args.limit]

    out = Path(args.out)
    ckpt = _ckpt_path(out)
    # Resume at CASE granularity: cases are independent, so a disconnect loses at
    # most the case in flight. The checkpoint (JSONL) holds every completed case.
    done = _load_ckpt(ckpt)
    if done:
        tot = sum(len(v) for v in done.values())
        print(f"[resume] checkpoint {ckpt}: {tot} cases done across "
              f"λ={sorted(done)} — skipping them", flush=True)
    pending = 0
    try:
        for lam in args.lambdas:
            tag = _lam_tag(lam)
            done_cases = done.get(tag, {})
            if len(done_cases) >= len(cases):
                print(f"=== λ: {tag}  all {len(cases)} cases done, skip ===", flush=True)
                continue
            print(f"=== λ: {tag}  p_min={args.p_min}  ({len(cases)} cases, "
                  f"cheap={args.cheap_model} strong={args.strong_model}) ===", flush=True)
            r = await run_lambda(
                system, prov, args.cheap_model, args.strong_model,
                lam, args.p_min, args.max_tokens, cases, args.max_filler, args.k,
                ckpt, done_cases,
            )
            if r.n_error:
                pending += r.n_error
                print(f"  [λ={tag}] {r.n_error} cases errored (likely network) — "
                      f"NOT checkpointed; re-run to retry just those.", flush=True)
            # Rebuild final json from the checkpoint after each λ.
            _write_output(out, args, cases, _results_from_ckpt(_load_ckpt(ckpt), args.p_min))
            print(f"[saved] λ={tag}: {r.n_cases} cases -> {out}", flush=True)
    finally:
        await system.close()

    results = _results_from_ckpt(_load_ckpt(ckpt), args.p_min)
    _write_output(out, args, cases, results)
    if pending:
        print(f"\n[!] {pending} case-runs errored and were skipped — re-run the same "
              f"command to retry only the missing (λ, case) pairs.", flush=True)

    print("\n" + "=" * 92)
    print(f"Build-side pricing λ-frontier  (hop={args.hop or 'all'}, {len(cases)} cases, "
          f"cheap={args.cheap_model} strong={args.strong_model}, p_min={args.p_min})")
    print("=" * 92)
    print(f"{'λ':>8} {'micro-P':>8} {'micro-R':>8} {'macro-P':>8} {'macro-R':>8} "
          f"{'strong-tok':>11} {'tok/case':>9} {'#esc':>5} {'Σq-esc':>8}")
    for r in results:
        tag = "inf" if math.isinf(r.lam) else f"{r.lam:.0e}"
        print(f"{tag:>8} {r.micro_precision:>8.3f} {r.micro_recall:>8.3f} "
              f"{r.macro_precision:>8.3f} {r.macro_recall:>8.3f} "
              f"{r.strong_tokens:>11} {r.tokens_per_case:>9.0f} {r.n_escalated:>5} "
              f"{r.sum_q_escalated:>8.2f}"
              + (f"  ({r.n_error} err)" if r.n_error else ""))
    print(f"\nArtifacts -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
