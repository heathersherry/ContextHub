"""MEME Cascade external-control eval: paired OFF/ON over cascade cases.

Design (per plan):
- Internal ablation: same ContextHub, single variable = failure-propagation layer
  ON vs OFF. Retrieval/answer/judge held constant.
- Paired within-case: ingest once -> before-Q -> apply root_change -> measure OFF
  (before drain) -> drain -> measure ON. This ordering requires the root_change
  event to exist but not yet be drained when OFF is measured, so cases run
  SERIALLY (a global concurrent engine would drain a case's event early and
  break the OFF-before-drain invariant). LLM latency dominates anyway.
- Isolation: a unique account_id per case (RLS scopes contexts/retrieval), so no
  truncate-between-cases races and per-case state is inspectable.

Metrics: accuracy (trivial-pass + raw after) by arm and by hop; oracle/answer
call counts + est. tokens.

Run: CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.run_eval \
        [--data PATH] [--limit N] [--hop {1,2}] [--chat-model M] [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from integrations.memebench.answer import answer_question
from integrations.memebench.ingest import (
    apply_root_change,
    apply_root_change_raw,
    edge_pr,
    edge_pr_raw,
    ingest_case,
    ingest_case_raw,
    ingest_case_raw_cascade_e2e,
    ingest_filler,
)
from integrations.memebench.judge import CaseVerdict, judge_case_async
from integrations.memebench.loader import CascadeCase, extract_cascade_cases, load_episodes
from integrations.memebench.systems import EvalSystem, build_system

# Moved to integrations/memebench/common.py so this finished experiment could be
# archived without breaking the live pipeline that borrowed these four symbols.
from integrations.memebench.common import (  # noqa: E402
    DEFAULT_DATA,
    _token_delta,
    _token_snap,
)


@dataclass
class CaseResult:
    episode_id: str
    domain: str
    hop: int
    target_entity: str
    gold_answer: str
    before_answer: str
    off_answer: str
    on_answer: str
    before_ok: bool
    off_after_ok: bool
    on_after_ok: bool
    off_trivial_pass: bool
    on_trivial_pass: bool
    oracle_calls: int
    n_filler: int
    edge_mode: str = "gold"
    # edge discovery P/R vs gold (only meaningful for edge_mode="discovered")
    edge_n_gold: int = 0
    edge_n_pred: int = 0
    edge_n_tp: int = 0
    edge_precision: float | None = None
    edge_recall: float | None = None
    # raw-dialogue (mode B) only: how many stored nodes the change detector
    # flagged as superseded (0 => no cascade could fire this case).
    raw_dialogue: bool = False
    n_superseded: int | None = None
    # 做法甲 build-side cascade (only set when --cascade): per-variable tier mix
    # for this case + weak/strong ingest token deltas.
    cascade: bool = False
    cascade_disamb_mix: dict | None = None
    cascade_cand_mix: dict | None = None
    cascade_edge_mix: dict | None = None
    cascade_cheap_tokens: int = 0
    cascade_strong_tokens: int = 0
    # Per-case token deltas for EVERY cost bucket, as
    #   {bucket: {"model", "calls", "prompt_tokens", "completion_tokens"}}.
    # Process-lifetime snapshots cannot survive checkpoint resume (a rerun only
    # re-executes the errored cases, so its snapshots cover those cases only), so
    # cost is reconstructed by summing these per-case deltas across the whole
    # checkpoint. Empty for pre-existing checkpoints written before this field.
    tokens: dict | None = None
    # Per-edge staleness-gate log for THIS case (做法乙). One entry per edge the
    # oracle rule judged: {dependent_id, change_type, cheap, escalated, final}.
    # oracle_calls counts only the strong tier, so without this an oracle_calls=0
    # failure cannot be told apart from "propagation never reached the edge".
    gate_events: list | None = None
    # Wall-clock seconds per stage. --case-timeout turns a slow case into an
    # `error`, and without timings there is no way to see how close a run sat to
    # that ceiling or which stage was slow.
    timings: dict | None = None
    # Retrieved node URIs behind each of the three answers (include_stale=False,
    # so the ON list is what survived propagation). Needed to check whether a
    # stale value leaked in via some OTHER node quoting it.
    retrieved_before: list | None = None
    retrieved_off: list | None = None
    retrieved_on: list | None = None
    # ON-arm notes that still contain the pre-change value, as {uri, text[:300]}.
    on_leak_notes: list | None = None
    # Gold-edge identities, not just P/R counts: which gold pairs were missed.
    edge_missed: list | None = None
    # Judge inputs, so any judge can be re-run offline over cases.json alone.
    before_question: str = ""
    after_question: str = ""
    before_gold: str | None = None
    error: str | None = None


def _account_for(case: CascadeCase) -> str:
    return f"meme-{case.episode_id}-{case.target_entity}"[:60]


# Cost buckets, in the same naming metrics.STAGE_OF_BUCKET uses.


async def run_one(
    system: EvalSystem, case: CascadeCase, *, filler_granularity: str, edge_mode: str,
    llm_judge: bool = False, raw_dialogue: bool = False, cascade: bool = False,
    tau_disamb: float = 0.5, tau_cand: float = 0.4, tau_edge: float = 0.4, k: int = 5,
) -> CaseResult:
    account = _account_for(case)
    embed = system.embedding.embed
    embed_batch = system.embedding.embed_batch
    oracle_calls_before = system.oracle_chat.call_count
    tok_before = _token_snap(system)
    system.gate_events.clear()   # per-case scope; rule appends into this list
    timings: dict[str, float] = {}
    t_case = time.perf_counter()

    def _lap(stage: str, t0: float) -> float:
        timings[stage] = round(time.perf_counter() - t0, 3)
        return time.perf_counter()
    n_superseded: int | None = None
    casc_mix = {"disamb": None, "cand": None, "edge": None}
    casc_cheap_tok = casc_strong_tok = 0

    try:
        discovery_svc = {
            "discovered": system.discovery,
            "discovered_tiered": system.discovery_tiered,
            "discovered_hard": system.discovery_hard,
        }.get(edge_mode)
        async with system.repo.session(account) as db:
            if raw_dialogue and cascade:
                c0 = system.cascade_cheap_chat.total_tokens
                s0 = system.cascade_strong_chat.total_tokens
                graph, tiers = await ingest_case_raw_cascade_e2e(
                    db, case, account, embed_batch,
                    extractor=system.extractor,
                    disamb_cheap=system.cascade_cheap_svc,
                    disamb_strong=system.cascade_strong_svc,
                    edge_cheap=system.cascade_cheap_svc,
                    edge_strong=system.cascade_strong_svc,
                    tau_disamb=tau_disamb, tau_cand=tau_cand, tau_edge=tau_edge, k=k,
                )
                pr = edge_pr_raw(case, graph)
                casc_mix = {kk: dict(vv) for kk, vv in tiers.items()}
                casc_cheap_tok = system.cascade_cheap_chat.total_tokens - c0
                casc_strong_tok = system.cascade_strong_chat.total_tokens - s0
            elif raw_dialogue:
                graph = await ingest_case_raw(
                    db, case, account, embed_batch, system.extractor, discovery_svc,
                )
                pr = edge_pr_raw(case, graph)
            else:
                graph = await ingest_case(
                    db, case, account, embed_batch,
                    edge_mode=edge_mode,
                    discovery=discovery_svc,
                )
                pr = edge_pr(case, graph)
            n_filler = await ingest_filler(db, case, account, embed_batch, granularity=filler_granularity)
        t = _lap("ingest", t_case)

        before_answer = ""
        before_res = None
        before_gold = case.before_question.expected_answer if case.before_question else None
        if case.before_question:
            async with system.repo.session(account) as db:
                before_res = await answer_question(system, db, account, case.before_question.question)
                before_answer = before_res.answer
        t = _lap("before_q", t)

        async with system.repo.session(account) as db:
            if raw_dialogue:
                superseded = await apply_root_change_raw(
                    db, case, account, graph, embed_batch,
                    system.extractor, system.discovery_chat,
                )
                n_superseded = len(superseded)
            else:
                await apply_root_change(db, case, graph, embed)
        t = _lap("root_change", t)

        # OFF: event exists, not yet drained.
        async with system.repo.session(account) as db:
            off_res = await answer_question(system, db, account, case.after_question.question)
            off_answer = off_res.answer
        t = _lap("off_answer", t)

        # ON: drain (this case's events; serial run => no other case's events pending).
        engine = system.build_engine(cascade_on_stale=True)
        engine._running = True
        for _ in range(8):
            await engine._drain_ready_events(context_id=None)
        t = _lap("drain", t)

        async with system.repo.session(account) as db:
            on_res = await answer_question(system, db, account, case.after_question.question)
            on_answer = on_res.answer
        t = _lap("on_answer", t)

        # ON-arm notes still carrying the pre-change value: the leak path where a
        # correctly-stale answer node is excluded but a DOWNSTREAM node quotes its
        # old value verbatim in a justification clause.
        leak = []
        if before_gold:
            from integrations.memebench.judge import matches
            for uri, txt in zip(on_res.retrieved_uris, on_res.retrieved_l2):
                if matches(txt, before_gold):
                    leak.append({"uri": uri, "text": txt[:300]})

        judge_chat = system.judge_chat if llm_judge else None
        bq = case.before_question.question if case.before_question else ""
        aq = case.after_question.question
        off_v: CaseVerdict = await judge_case_async(
            before_answer, before_gold, off_answer, case.after_question.expected_answer,
            before_question=bq, after_question=aq, chat=judge_chat,
        )
        on_v: CaseVerdict = await judge_case_async(
            before_answer, before_gold, on_answer, case.after_question.expected_answer,
            before_question=bq, after_question=aq, chat=judge_chat,
        )

        return CaseResult(
            episode_id=case.episode_id, domain=case.domain, hop=case.hop,
            target_entity=case.target_entity, gold_answer=case.gold_answer,
            before_answer=before_answer, off_answer=off_answer, on_answer=on_answer,
            before_ok=on_v.before_ok,
            off_after_ok=off_v.after_ok, on_after_ok=on_v.after_ok,
            off_trivial_pass=off_v.trivial_pass, on_trivial_pass=on_v.trivial_pass,
            oracle_calls=system.oracle_chat.call_count - oracle_calls_before, n_filler=n_filler,
            edge_mode=edge_mode,
            edge_n_gold=int(pr["n_gold"]), edge_n_pred=int(pr["n_pred"]), edge_n_tp=int(pr["n_tp"]),
            edge_precision=float(pr["precision"]), edge_recall=float(pr["recall"]),
            raw_dialogue=raw_dialogue, n_superseded=n_superseded,
            cascade=cascade,
            cascade_disamb_mix=casc_mix["disamb"], cascade_cand_mix=casc_mix["cand"],
            cascade_edge_mix=casc_mix["edge"],
            cascade_cheap_tokens=casc_cheap_tok, cascade_strong_tokens=casc_strong_tok,
            tokens=_token_delta(tok_before, _token_snap(system)),
            gate_events=list(system.gate_events),
            timings={**timings, "total": round(time.perf_counter() - t_case, 3)},
            retrieved_before=(before_res.retrieved_uris if before_res else []),
            retrieved_off=off_res.retrieved_uris,
            retrieved_on=on_res.retrieved_uris,
            on_leak_notes=leak,
            edge_missed=list(pr.get("missed") or []),
            before_question=bq, after_question=aq, before_gold=before_gold,
        )
    except Exception as exc:  # fail-soft per case
        return CaseResult(
            episode_id=case.episode_id, domain=case.domain, hop=case.hop,
            target_entity=case.target_entity, gold_answer=case.gold_answer,
            before_answer="", off_answer="", on_answer="",
            before_ok=False, off_after_ok=False, on_after_ok=False,
            off_trivial_pass=False, on_trivial_pass=False,
            oracle_calls=0, n_filler=0, edge_mode=edge_mode,
            # An errored case still burned tokens (and time). Recording them keeps
            # the run's TOTAL spend honest; $/ep is unaffected (n_ok denominator).
            tokens=_token_delta(tok_before, _token_snap(system)),
            gate_events=list(system.gate_events),
            timings={**timings, "total": round(time.perf_counter() - t_case, 3)},
            error=f"{type(exc).__name__}: {exc}",
        )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--hop", type=int, choices=[1, 2], default=None)
    # MODEL FLAGS CARRY NO DEFAULTS (2026-08-10). --extract-model once defaulted to
    # claude-opus-4-8 and silently sent a 100-case P2 run through opus while the
    # recorded decision was gpt-4.1-mini; the numbers were mis-attributed for a
    # month. Name every model in play at each run. Flags whose feature is off may
    # be omitted; _require_models() rejects an enabled feature with no model named.
    ap.add_argument("--chat-model", required=True,
                    help="answer-generation model (always required)")
    ap.add_argument("--oracle-model", default=None,
                    help="oracle (P2 verify tier) model; falls back to --chat-model. "
                         "Set this to keep the oracle stronger than the answer model, "
                         "e.g. --p2-cascade --p2-cheap-model gpt-4o-mini --oracle-model gpt-5.5")
    ap.add_argument("--provider", default="openlux",
                    help="chat/oracle provider label from model_providers.local.json "
                         "(--cascade needs one carrying the gpt-* models, e.g. openlux)")
    ap.add_argument("--filler-granularity", default="session", choices=["session", "turn"])
    ap.add_argument("--edge-mode", default="gold",
                    choices=["gold", "discovered", "discovered_tiered", "discovered_hard"],
                    help="gold=MEME dependency_edges_used (oracle upper bound); "
                         "discovered=naive LLM discovery; "
                         "discovered_tiered=conditional-aware syntactic routing + LLM; "
                         "discovered_hard=syntactic hard-exclude of conditional facts")
    ap.add_argument("--case-timeout", type=float, default=900.0,
                    help="per-case wall-clock timeout in seconds (stuck call -> error, skip). "
                         "Was 120 until 2026-08-10, which no longer fits: measured on openlux, "
                         "a strong-tier call at realistic prompt length (~2.6k in) takes ~29s "
                         "median and embed_batch(10) ~14s, so a median case needs ~220s and the "
                         "heaviest (20 strong calls) >700s. At 120 every case timed out.")
    ap.add_argument("--llm-judge", action="store_true",
                    help="grade answers with an LLM judge (MEME §4.1 parity) instead of "
                         "string containment; uses system.judge_chat")
    ap.add_argument("--judge-model", default=None,
                    help="model for the LLM judge (MEME uses gpt-4o); REQUIRED with --llm-judge")
    ap.add_argument("--raw-dialogue", action="store_true",
                    help="mode B: ingest from raw evidence dialogue (per-session extraction, "
                         "no gold_facts / no entity schema); root change is detected zero-oracle. "
                         "Requires --edge-mode discovered*.")
    ap.add_argument("--extract-model", default=None,
                    help="extractor model for --raw-dialogue (mode B); REQUIRED with --raw-dialogue")
    ap.add_argument("--cascade", action="store_true",
                    help="做法甲: route the raw-dialogue build side through the four-variable "
                         "cascade (var1 fixed extractor; var2/3 weak->strong; var4 recency/embed_topk). "
                         "Requires --raw-dialogue. P2 propagation path unchanged.")
    ap.add_argument("--cascade-cheap-model", default=None,
                    help="weak-tier model for --cascade (var2/3); REQUIRED with --cascade")
    ap.add_argument("--cascade-strong-model", default=None,
                    help="strong-tier model for --cascade (var2/3); REQUIRED with --cascade")
    ap.add_argument("--tau-disamb", type=float, default=0.5,
                    help="design-time tau for variable 2 (disambiguation) under --cascade")
    ap.add_argument("--tau-cand", type=float, default=0.4,
                    help="design-time tau for variable 4 (candidate selection) under --cascade")
    ap.add_argument("--tau-edge", type=float, default=0.4,
                    help="design-time tau for variable 3 (edge discovery) under --cascade")
    ap.add_argument("--cascade-k", type=int, default=5,
                    help="variable-4 recency/embed_topk width under --cascade")
    ap.add_argument("--p2-cascade", action="store_true",
                    help="做法乙: enable the propagation-side soundness cascade — "
                         "cheap gate before the oracle (cheap-stale short-circuits, "
                         "cheap-fresh escalates to oracle). Off = single-tier oracle "
                         "(byte-for-byte the pre-做法乙 P2). Independent of --cascade.")
    ap.add_argument("--p2-cheap-model", default=None,
                    help="cheap-gate model for --p2-cascade (oracle set via --oracle-model); "
                         "REQUIRED with --p2-cascade")
    ap.add_argument("--sample", type=int, default=None,
                    help="randomly sample this many cases (reproducible via --seed) "
                         "instead of taking the first --limit")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for --sample")
    ap.add_argument("--out", default="integrations/memebench/runs")
    args = ap.parse_args()

    if args.raw_dialogue and args.edge_mode == "gold":
        ap.error("--raw-dialogue has no entity labels at insert time; use "
                 "--edge-mode discovered / discovered_tiered / discovered_hard")
    if args.cascade and not args.raw_dialogue:
        ap.error("--cascade routes the raw-dialogue build side; add --raw-dialogue")

    # No model may be implied. Each enabled feature must name its own model.
    for flag, needed, dest in (
        ("--llm-judge", args.llm_judge, "judge_model"),
        ("--raw-dialogue", args.raw_dialogue, "extract_model"),
        ("--cascade", args.cascade, "cascade_cheap_model"),
        ("--cascade", args.cascade, "cascade_strong_model"),
        ("--p2-cascade", args.p2_cascade, "p2_cheap_model"),
    ):
        if needed and not getattr(args, dest):
            ap.error(f"{flag} requires --{dest.replace('_', '-')} to be named explicitly "
                     f"(model defaults were removed on purpose)")

    episodes = load_episodes(args.data)
    cases = extract_cascade_cases(episodes, hop=args.hop)
    if args.sample:
        import random
        cases = random.Random(args.seed).sample(cases, min(args.sample, len(cases)))
    elif args.limit:
        cases = cases[: args.limit]
    # Log EVERY model actually in play, so the run's own log is the audit record.
    roles = [f"answer={args.chat_model}", f"oracle={args.oracle_model or args.chat_model}"]
    if args.raw_dialogue:
        roles.append(f"extract={args.extract_model}")
    if args.llm_judge:
        roles.append(f"judge={args.judge_model}")
    if args.cascade:
        roles.append(f"cascade_cheap={args.cascade_cheap_model}")
        roles.append(f"cascade_strong={args.cascade_strong_model}")
    if args.p2_cascade:
        roles.append(f"p2_cheap={args.p2_cheap_model}")
    print(f"Running {len(cases)} cascade cases (hop={args.hop or 'all'}) "
          f"[edge_mode={args.edge_mode}] models: {', '.join(roles)}", flush=True)

    system = await build_system(
        chat_model=args.chat_model, oracle_model=args.oracle_model,
        judge_model=args.judge_model, extract_model=args.extract_model,
        provider_label=args.provider,
        cascade=args.cascade,
        cascade_cheap_model=args.cascade_cheap_model,
        cascade_strong_model=args.cascade_strong_model,
        p2_cascade=args.p2_cascade,
        p2_cheap_model=args.p2_cheap_model,
    )
    # Resume support: replay per-case results from checkpoint.jsonl (one asdict/line).
    # Each case is independent (own account, own graph+propagation), so a completed
    # case never needs rerunning. Skipped cases don't rebuild their DB state — their
    # result is already persisted — so TRUNCATE only fires on a fresh (no-checkpoint) run.
    ckpt_dir = Path(args.out)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = ckpt_dir / "checkpoint.jsonl"
    done: dict[str, CaseResult] = {}
    if ckpt_path.exists():
        import json as _json
        for line in ckpt_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            d = _json.loads(line)
            # only treat error-free cases as done; errored cases (e.g. ReadTimeout)
            # are retried on rerun. Last non-error line for an id wins.
            if not d.get("error"):
                done[d["episode_id"]] = CaseResult(**d)
        print(f"  [resume] {len(done)} ok cases loaded from {ckpt_path}, "
              f"will skip them (errored cases will be retried)", flush=True)

    # start clean ONLY on a fresh run (no checkpoint) — resuming must keep prior state
    if not done:
        async with system.pool.acquire() as conn:
            await conn.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    results: list[CaseResult] = []
    try:
        for i, case in enumerate(cases, 1):
            if case.episode_id in done:
                results.append(done[case.episode_id])
                print(f"  [{i}/{len(cases)}] SKIP {case.episode_id} (checkpointed)", flush=True)
                continue
            # Timeout cancels run_one, and CancelledError is not an Exception, so
            # run_one's own except never runs — snapshot here to keep the timed-out
            # case's tokens/time.
            tok_before_case = _token_snap(system)
            t_before_case = time.perf_counter()
            try:
                r = await asyncio.wait_for(
                    run_one(system, case, filler_granularity=args.filler_granularity,
                            edge_mode=args.edge_mode, llm_judge=args.llm_judge,
                            raw_dialogue=args.raw_dialogue, cascade=args.cascade,
                            tau_disamb=args.tau_disamb, tau_cand=args.tau_cand,
                            tau_edge=args.tau_edge, k=args.cascade_k),
                    timeout=args.case_timeout,
                )
            except asyncio.TimeoutError:
                r = CaseResult(
                    episode_id=case.episode_id, domain=case.domain, hop=case.hop,
                    target_entity=case.target_entity, gold_answer=case.gold_answer,
                    before_answer="", off_answer="", on_answer="",
                    before_ok=False, off_after_ok=False, on_after_ok=False,
                    off_trivial_pass=False, on_trivial_pass=False,
                    oracle_calls=0, n_filler=0, edge_mode=args.edge_mode,
                    raw_dialogue=args.raw_dialogue,
                    tokens=_token_delta(tok_before_case, _token_snap(system)),
                    gate_events=list(system.gate_events),
                    timings={"total": round(time.perf_counter() - t_before_case, 3)},
                    error=f"case_timeout>{args.case_timeout}s",
                )
            results.append(r)
            # append to checkpoint immediately so a mid-run crash loses at most this case
            with ckpt_path.open("a") as f:
                f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
            flag = "ERR" if r.error else ("OK" if (not r.off_after_ok and r.on_after_ok) else "..")
            print(f"  [{i}/{len(cases)}] {flag} {case.episode_id}/{case.target_entity} "
                  f"hop{r.hop} OFF={r.off_after_ok} ON={r.on_after_ok} "
                  f"{'<'+r.error+'>' if r.error else ''}", flush=True)
    finally:
        answer_snap = system.answer_chat.snapshot()
        oracle_snap = system.oracle_chat.snapshot()
        discovery_snap = system.discovery_chat.snapshot()
        # Both are None when their feature is off (model not named) — the old
        # unguarded .snapshot() crashed any run without --llm-judge, losing the
        # summary (checkpoint survived).
        extract_snap = system.extract_chat.snapshot() if system.extract_chat else None
        judge_snap = system.judge_chat.snapshot() if system.judge_chat else None
        casc_cheap_snap = system.cascade_cheap_chat.snapshot() if system.cascade_cheap_chat else None
        casc_strong_snap = system.cascade_strong_chat.snapshot() if system.cascade_strong_chat else None
        p2_cheap_snap = system.p2_cheap_chat.snapshot() if system.p2_cheap_chat else None
        await system.close()

    _report(results, args, answer_snap, oracle_snap, discovery_snap,
            extract_snap, judge_snap, casc_cheap_snap, casc_strong_snap, p2_cheap_snap)
    if args.cascade:
        _report_cascade(results, casc_cheap_snap, casc_strong_snap,
                        args.cascade_cheap_model, args.cascade_strong_model)
    if args.p2_cascade:
        _report_p2_cascade(results, p2_cheap_snap, oracle_snap,
                           args.p2_cheap_model, args.oracle_model or args.chat_model)


def _report_cascade(results, cheap_snap, strong_snap, cheap_model, strong_model):
    """做法甲 build-side cascade summary: per-variable tier mix + weak/strong ingest cost.

    Three-way cost split (per plan): report the build-side weak(gpt-4o-mini) +
    strong(gpt-5.6-sol) ingest tokens SEPARATELY from the oracle/answer tokens
    (gpt-4.1-mini, reported by the main _report). Never sum across models.
    """
    from collections import Counter

    ok = [r for r in results if not r.error and r.cascade]
    mix = {"disamb": Counter(), "cand": Counter(), "edge": Counter()}
    for r in ok:
        for key, attr in (("disamb", r.cascade_disamb_mix),
                          ("cand", r.cascade_cand_mix), ("edge", r.cascade_edge_mix)):
            if attr:
                mix[key].update(attr)
    cheap_tok = sum(r.cascade_cheap_tokens for r in ok)
    strong_tok = sum(r.cascade_strong_tokens for r in ok)

    print("\n" + "=" * 72)
    print(f"做法甲 build-side cascade  ({len(ok)} cases, var1 fixed extractor)")
    print("=" * 72)
    print(f"  variable 2 disamb : {dict(mix['disamb'])}")
    print(f"  variable 4 cand   : {dict(mix['cand'])}")
    print(f"  variable 3 edge   : {dict(mix['edge'])}")
    print(f"  ingest weak ({cheap_model})  tokens : {cheap_tok}")
    print(f"  ingest strong ({strong_model}) tokens: {strong_tok}")
    if cheap_snap is not None:
        print(f"  weak  calls={cheap_snap.get('calls')} total_tokens={cheap_snap.get('total_tokens')}")
    if strong_snap is not None:
        print(f"  strong calls={strong_snap.get('calls')} total_tokens={strong_snap.get('total_tokens')}")


def _report_p2_cascade(results, cheap_snap, strong_snap, cheap_model, strong_model):
    """做法乙 propagation-side soundness cascade summary.

    oracle_calls (per case) counts ONLY the strong/verify tier (oracle_chat) —
    the cheap gate is a separate CountingChatClient. So:
      ON-arm strong calls  = Σ oracle_calls          (edges that reached strong)
      cheap-gate calls      = cheap_snap['calls']      (every edge is gated once)
      short-circuits (saved strong) = cheap calls − strong calls
    Compare this run's Σoracle_calls against the OFF-arm run (no --p2-cascade,
    where oracle_calls == edge count) to read the strong-call savings.
    """
    n_err = sum(1 for r in results if r.error)
    ok = [r for r in results if not r.error]
    strong_calls = sum(r.oracle_calls for r in ok)
    # Gate counts from the per-case log, so cheap and strong share ONE denominator
    # (the OK cases). The withdrawn E.9 read cheap from a whole-run process snapshot
    # and strong from OK cases only, making the short-circuit rate unauditable.
    ev = [e for r in ok for e in (r.gate_events or [])]
    cheap_calls = sum(1 for e in ev if e.get("cheap") is not None) if ev else None
    short_circuits = sum(1 for e in ev if e.get("cheap") == "stale")
    escalations = sum(1 for e in ev if e.get("escalated"))
    false_fresh = sum(1 for e in ev if e.get("cheap") == "fresh" and e.get("final") == "stale")

    print("\n" + "=" * 72)
    print(f"做法乙 P2 propagation-side cascade  ({len(ok)} ok cases)")
    print("=" * 72)
    if n_err:
        print(f"  ⚠️ calls below are OK-case only ({len(ok)}); "
              f"total_tokens are whole-run (incl. {n_err} errored) — different denominators")
    print(f"  cheap gate ({cheap_model}) : calls={cheap_calls} "
          f"total_tokens={cheap_snap.get('total_tokens') if cheap_snap else None}")
    print(f"  strong verify ({strong_model}) : calls={strong_calls} "
          f"total_tokens={strong_snap.get('total_tokens') if strong_snap else None}")
    if cheap_calls:
        rate = short_circuits / cheap_calls
        print(f"  gated edges (per-case log, OK cases)       : {cheap_calls}")
        print(f"  short-circuits (cheap-stale, saved strong) : {short_circuits} "
              f"({rate:.1%} of gated edges)")
        print(f"  escalations (cheap-fresh → strong)         : {escalations}")
        # The cheap tier's only fatal error mode is false-FRESH; when the oracle
        # then says stale, the cascade caught what the cheap tier missed.
        print(f"  cheap false-fresh caught by oracle         : {false_fresh}")
        if escalations != strong_calls:
            print(f"  ⚠️ escalations({escalations}) != Σoracle_calls({strong_calls}) — "
                  f"strong tier also reached by a non-gated path; investigate")
    else:
        print("  ⚠️ no gate events logged — cheap tier never ran (single-tier?)")
    print(f"  → compare Σstrong={strong_calls} vs OFF-arm oracle_calls "
          f"(=edge count) for the savings.")


def _report(results, args, answer_snap, oracle_snap, discovery_snap,
            extract_snap, judge_snap, casc_cheap_snap=None,
            casc_strong_snap=None, p2_cheap_snap=None):
    from integrations.memebench.metrics import summarize, print_summary, write_artifacts
    summary = summarize(results, answer_snap, oracle_snap, discovery_snap,
                        model=args.chat_model, edge_mode=args.edge_mode,
                        extract_snap=extract_snap, judge_snap=judge_snap,
                        casc_cheap_snap=casc_cheap_snap,
                        casc_strong_snap=casc_strong_snap,
                        p2_cheap_snap=p2_cheap_snap)
    print_summary(summary)
    out_dir = Path(args.out)
    write_artifacts(out_dir, results, summary)
    print(f"\nArtifacts written to {out_dir}/")


if __name__ == "__main__":
    asyncio.run(main())
