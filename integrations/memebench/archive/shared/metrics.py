"""Aggregate CascadeCase results into the comparison table + cost, write artifacts.

Reports (per plan):
- Internal ablation: acc(ON) - acc(OFF) = delta, cleanly attributed to the
  propagation layer (everything else held constant).
- By-hop breakdown (1-hop / 2-hop).
- Cost: oracle calls + est. tokens.
- Static MEME baseline row (6 systems avg 3% on Cascade; MD-flat x Opus4.7
  0.32 @ ~70x) — cited from the paper, NOT rerun.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

# Static figures cited from MEME (arXiv:2605.12477); not rerun here.
MEME_BASELINE = {
    "six_systems_cascade_avg_acc": 0.03,     # 6 systems, gpt-4.1-mini, 100 episodes
    "mdflat_opus47_cascade_acc": 0.32,       # MD-flat x Claude Opus 4.7, 20-ep subset
    "mdflat_opus47_cost_multiple": 70,       # ~70x baseline cost
}

# Public per-token rates, USD per 1M tokens (in, out). Verified 2026-08; see
# ContextHub-research-plan/research/proposal/model-pricing-reference.md.
# Cascade runs mix backbones whose rates differ ~40x, so every bucket MUST be
# priced at its own model's rate — a single global price is wrong for them.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),   # MEME p12's backbone rate
    "gpt-4o": (2.50, 10.00),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6": (5.00, 30.00),
}
# Fallback for an unlisted model: gpt-4.1-mini's rate (MEME's own backbone).
PRICE_IN_PER_1M = 0.40
PRICE_OUT_PER_1M = 1.60


def _rate(model: str | None) -> tuple[float, float]:
    return PRICES.get(model or "", (PRICE_IN_PER_1M, PRICE_OUT_PER_1M))


def _usd(prompt_tokens: int, completion_tokens: int,
         price_in: float, price_out: float) -> float:
    return prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out


def _bucket_usd(snap: dict | None) -> float:
    """USD for one bucket, priced at ITS OWN model's public rate."""
    if not snap:
        return 0.0
    pin, pout = _rate(snap.get("model"))
    return _usd(snap.get("prompt_tokens", 0), snap.get("completion_tokens", 0), pin, pout)


# Bucket -> MEME-style stage. Retrieve is omitted (embedding only, no LLM call).
# Propagation is OUR stage: MEME has no failure-propagation layer, so it can
# never be folded into Ingest/Answer without breaking comparability.
STAGE_OF_BUCKET = {
    "extract_llm": "ingest",          # raw dialogue -> facts (write path)
    "ingest_llm": "ingest",           # dependency discovery (non-cascade path)
    "cascade_cheap_llm": "ingest",    # 做法甲 build-side weak tier
    "cascade_strong_llm": "ingest",   # 做法甲 build-side strong tier
    "inference_llm": "answer",        # final answer
    "p2_cheap_llm": "propagation",    # 做法乙 cheap staleness gate
    "oracle_llm": "propagation",      # staleness oracle / P2 verify tier
}
# Excluded from all stages, matching MEME: embedding + the GPT-4o grader.
EXCLUDED_BUCKETS = ("judge_llm",)


def cost_per_episode(buckets: dict, n_ok: int) -> dict:
    """Per-episode token & USD cost under MEME's accounting, three stages.

    Each bucket is priced at ITS OWN model's public per-token rate (runs mix
    gpt-4o-mini / gpt-4.1-mini / gpt-5.5, ~40x apart), then summed into stages:

      - ``ingest``      = extract + dependency discovery (incl. both 做法甲
                          cascade tiers, which ARE the build cost under --cascade)
      - ``answer``      = final answer
      - ``propagation`` = P2 cheap gate + staleness oracle. **Our stage; MEME
                          has no analogue**, so it is reported separately and
                          never mixed into the MEME-comparable number.
      - retrieve        = 0 (embedding only, no LLM call)

    Two scopes, so nothing is hidden:
      - ``meme_aligned``: Ingest + Answer. Strictly MEME-comparable.
      - ``full``: + Propagation. ContextHub's true total.
    """
    def money_tokens(names) -> tuple[int, int, float]:
        pin = pout = 0
        usd = 0.0
        for k in names:
            snap = buckets.get(k)
            if not snap:
                continue
            pin += snap.get("prompt_tokens", 0)
            pout += snap.get("completion_tokens", 0)
            usd += _bucket_usd(snap)
        return pin, pout, usd

    def scope(names) -> dict:
        pin, pout, usd = money_tokens(names)
        tot = pin + pout
        return {
            "prompt_tokens": pin,
            "completion_tokens": pout,
            "total_tokens": tot,
            "tokens_per_episode": (tot / n_ok) if n_ok else None,
            "usd_total": usd,
            "usd_per_episode": (usd / n_ok) if n_ok else None,
        }

    by_stage: dict[str, list[str]] = {"ingest": [], "answer": [], "propagation": []}
    for bucket, st in STAGE_OF_BUCKET.items():
        by_stage[st].append(bucket)

    stages = {st: scope(names) for st, names in by_stage.items()}
    # Per-bucket detail with the rate actually applied, so any number in the
    # paper's cost table can be traced back to (model, tokens, rate).
    detail = {}
    for bucket, st in STAGE_OF_BUCKET.items():
        snap = buckets.get(bucket)
        if not snap or not snap.get("calls"):
            continue
        rin, rout = _rate(snap.get("model"))
        detail[bucket] = {
            "stage": st,
            "model": snap.get("model"),
            "price_in_per_1m": rin,
            "price_out_per_1m": rout,
            "calls": snap.get("calls"),
            "prompt_tokens": snap.get("prompt_tokens", 0),
            "completion_tokens": snap.get("completion_tokens", 0),
            "usd_total": _bucket_usd(snap),
            "usd_per_episode": (_bucket_usd(snap) / n_ok) if n_ok else None,
        }

    return {
        "priced_per_model": True,
        "prices_used": {m: {"in": p[0], "out": p[1]} for m, p in PRICES.items()},
        "stage_map": ("Ingest=extract+discovery(+cascade weak/strong), Answer=inference, "
                      "Propagation=p2_cheap_gate+oracle (ours; no MEME analogue), "
                      "Retrieve=0 (embedding only)"),
        "excluded": ["embedding", "judge"],
        "stages": stages,
        "by_bucket": detail,
        "meme_aligned": scope(by_stage["ingest"] + by_stage["answer"]),
        "full": scope(by_stage["ingest"] + by_stage["answer"] + by_stage["propagation"]),
    }


def _acc(results, key, hop=None):
    rs = [r for r in results if r.error is None and (hop is None or r.hop == hop)]
    if not rs:
        return None, 0
    n = len(rs)
    hits = sum(1 for r in rs if getattr(r, key))
    return hits / n, n


def _edge_pr(results, hop=None) -> dict:
    """Micro precision/recall of discovered edges vs gold (Step-1 metric).

    Micro = pool tp/pred/gold across cases, so per-case counts weight naturally.
    Also report the macro (mean of per-case P/R) for reference.
    """
    rs = [r for r in results if r.error is None and (hop is None or r.hop == hop)]
    if not rs:
        return {"n": 0}
    tp = sum(r.edge_n_tp for r in rs)
    pred = sum(r.edge_n_pred for r in rs)
    gold = sum(r.edge_n_gold for r in rs)
    micro_p = tp / pred if pred else None
    micro_r = tp / gold if gold else None
    macro_p = sum(r.edge_precision for r in rs if r.edge_precision is not None) / len(rs)
    macro_r = sum(r.edge_recall for r in rs if r.edge_recall is not None) / len(rs)
    return {
        "n": len(rs),
        "n_tp": tp, "n_pred": pred, "n_gold": gold,
        "micro_precision": micro_p, "micro_recall": micro_r,
        "macro_precision": macro_p, "macro_recall": macro_r,
    }


def _buckets_from_cases(results) -> dict | None:
    """Sum per-case token deltas (r.tokens) into snapshot-shaped buckets.

    Returns None when no ok case carries token data (old checkpoint), so the
    caller can fall back to process-lifetime snapshots.
    """
    ok = [r for r in results if r.error is None and getattr(r, "tokens", None)]
    if not ok:
        return None
    out: dict[str, dict] = {}
    for r in ok:
        for bucket, d in (r.tokens or {}).items():
            b = out.setdefault(bucket, {
                "model": d.get("model"), "calls": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "tokens_are_real": True, "estimated_calls": 0,
            })
            b["calls"] += d.get("calls", 0)
            b["prompt_tokens"] += d.get("prompt_tokens", 0)
            b["completion_tokens"] += d.get("completion_tokens", 0)
    for b in out.values():
        b["total_tokens"] = b["prompt_tokens"] + b["completion_tokens"]
    return out


def summarize(results, answer_snap, oracle_snap, discovery_snap, *,
              model: str, edge_mode: str = "gold",
              extract_snap: dict | None = None, judge_snap: dict | None = None,
              casc_cheap_snap: dict | None = None,
              casc_strong_snap: dict | None = None,
              p2_cheap_snap: dict | None = None) -> dict:
    n_total = len(results)
    n_err = sum(1 for r in results if r.error)
    n_ok = n_total - n_err

    def block(hop):
        off_tp, n = _acc(results, "off_trivial_pass", hop)
        on_tp, _ = _acc(results, "on_trivial_pass", hop)
        off_raw, _ = _acc(results, "off_after_ok", hop)
        on_raw, _ = _acc(results, "on_after_ok", hop)
        return {
            "n": n,
            "off_trivial_pass": off_tp,
            "on_trivial_pass": on_tp,
            "delta_trivial_pass": (on_tp - off_tp) if (on_tp is not None and off_tp is not None) else None,
            "off_after_raw": off_raw,
            "on_after_raw": on_raw,
            "delta_after_raw": (on_raw - off_raw) if (on_raw is not None and off_raw is not None) else None,
        }

    total_oracle_calls = sum(r.oracle_calls for r in results if r.error is None)
    snap_buckets = {
        # token buckets: ingest (discovery) / inference (answer) / oracle /
        # extract (raw-dialogue fact extraction, mode B only) / judge, plus the
        # 做法甲 build-side cascade tiers and the 做法乙 cheap gate. Under
        # --cascade the real build cost lives in cascade_*, NOT in ingest_llm.
        "ingest_llm": discovery_snap,
        "inference_llm": answer_snap,
        "oracle_llm": oracle_snap,
        "extract_llm": extract_snap,
        "judge_llm": judge_snap,
        "cascade_cheap_llm": casc_cheap_snap,
        "cascade_strong_llm": casc_strong_snap,
        "p2_cheap_llm": p2_cheap_snap,
    }
    # Prefer per-case token deltas: process-lifetime snapshots only cover the
    # cases THIS process ran, so after a checkpoint resume-retry they undercount.
    # Summing r.tokens over all ok cases is resume-safe. Falls back to snapshots
    # for old checkpoints (r.tokens is None) or non-token-carrying runs.
    buckets = _buckets_from_cases(results) or snap_buckets
    tokens_source = "per_case_sum" if buckets is not snap_buckets else "process_snapshot"
    return {
        "model": model,
        "edge_mode": edge_mode,
        "n_total": n_total,
        "n_ok": n_ok,
        "n_error": n_err,
        "overall": block(None),
        "hop1": block(1),
        "hop2": block(2),
        # Step-1: dependency-discovery quality (only meaningful when discovered).
        "edge_discovery": {
            "overall": _edge_pr(results, None),
            "hop1": _edge_pr(results, 1),
            "hop2": _edge_pr(results, 2),
        },
        "cost": {
            "total_oracle_calls": total_oracle_calls,
            "oracle_calls_per_case": (total_oracle_calls / n_ok) if n_ok else None,
            "tokens_source": tokens_source,
            **buckets,
            # MEME-aligned per-episode token & USD (two scopes: aligned / full).
            "per_episode": cost_per_episode(buckets, n_ok),
        },
        "meme_baseline_static": MEME_BASELINE,
    }


def _fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


def _tok(snap: dict | None) -> str:
    # Buckets summed from per-case deltas only exist when that role was called,
    # so an absent bucket means "never used", not an error.
    if not snap:
        return "0 calls, 0 tok"
    tag = "real" if snap.get("tokens_are_real") else "est"
    return f"{snap['calls']} calls, {snap['total_tokens']} tok ({tag})"


def print_summary(s: dict) -> None:
    print("\n" + "=" * 62)
    print(f"MEME Cascade External-Control Results  (model={s['model']}, "
          f"edge_mode={s.get('edge_mode', 'gold')})")
    print(f"cases: {s['n_ok']} ok / {s['n_error']} error / {s['n_total']} total")
    print("=" * 62)
    print(f"{'stratum':10s} {'n':>4s} {'OFF(tp)':>9s} {'ON(tp)':>9s} {'delta':>8s}  {'OFF(raw)':>9s} {'ON(raw)':>9s}")
    for name in ("overall", "hop1", "hop2"):
        b = s[name]
        print(f"{name:10s} {b['n']:>4d} {_fmt(b['off_trivial_pass']):>9s} {_fmt(b['on_trivial_pass']):>9s} "
              f"{_fmt(b['delta_trivial_pass']):>8s}  {_fmt(b['off_after_raw']):>9s} {_fmt(b['on_after_raw']):>9s}")
    if str(s.get("edge_mode", "")).startswith("discovered"):
        print("-" * 62)
        print("dependency discovery (Step-1) vs gold edges:")
        for name in ("overall", "hop1", "hop2"):
            e = s["edge_discovery"][name]
            if not e.get("n"):
                continue
            print(f"  {name:8s} P={_fmt(e['micro_precision'])} R={_fmt(e['micro_recall'])} "
                  f"(micro; tp={e['n_tp']}/pred={e['n_pred']}/gold={e['n_gold']}) "
                  f"macroP={_fmt(e['macro_precision'])} macroR={_fmt(e['macro_recall'])}")
    c = s["cost"]
    print("-" * 62)
    print(f"oracle calls: {c['total_oracle_calls']} total, {_fmt(c['oracle_calls_per_case'])}/case")
    print(f"tokens from: {c.get('tokens_source', 'process_snapshot')}")
    print(f"ingest    LLM (discovery): {_tok(c.get('ingest_llm'))}")
    print(f"inference LLM (answer):    {_tok(c.get('inference_llm'))}")
    print(f"oracle    LLM (staleness): {_tok(c.get('oracle_llm'))}")
    if c.get("extract_llm"):
        print(f"extract   LLM (raw-B):     {_tok(c['extract_llm'])}")
    if c.get("judge_llm"):
        print(f"judge     LLM (grading):   {_tok(c['judge_llm'])}")
    pe = c.get("per_episode")
    if pe:
        def _pe(d):  # n/a when no episode succeeded (tokens_per_episode is None)
            t, u = d["tokens_per_episode"], d["usd_per_episode"]
            return "n/a tok, $n/a" if t is None else f"{t:,.0f} tok, ${u:.5f}"
        st = pe.get("stages") or {}
        for name in ("ingest", "answer", "propagation"):
            if st.get(name):
                print(f"  stage {name:12s}: {_pe(st[name])}")
        for bucket, d in (pe.get("by_bucket") or {}).items():
            print(f"    {bucket:20s} {str(d['model']):14s} "
                  f"${d['price_in_per_1m']}/${d['price_out_per_1m']} per 1M  "
                  f"{d['calls']} calls  ${d['usd_total']:.4f}")
        a, f = pe["meme_aligned"], pe["full"]
        print(f"per-episode (MEME-aligned, ingest+answer): {_pe(a)}")
        print(f"per-episode (full, +propagation):          {_pe(f)}")
    m = s["meme_baseline_static"]
    print("-" * 62)
    print(f"MEME baseline (cited): 6-sys Cascade avg={m['six_systems_cascade_avg_acc']}, "
          f"MD-flat×Opus4.7={m['mdflat_opus47_cascade_acc']} @ ~{m['mdflat_opus47_cost_multiple']}×")
    print("=" * 62)


def write_artifacts(out_dir: Path, results, summary: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    rows = [asdict(r) for r in results]
    with (out_dir / "cases.json").open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    if rows:
        with (out_dir / "cases.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
