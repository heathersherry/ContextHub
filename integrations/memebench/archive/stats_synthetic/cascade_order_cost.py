"""Cheap-first vs strong-first soundness-cascade cost, over the full pos+neg set.

Both orders short-circuit on stale and escalate on fresh, so their final verdict
is cheap_stale OR strong_stale either way -- identical false-fresh, identical
false-stale, identical recompute set. Only the call pattern differs, so the
comparison reduces to judge cost. Escalation rate for a given head tier is that
tier's Pr(fresh) over all edges it sees, positives and negatives alike.

Reports the break-even per-token price ratio, so the conclusion does not depend
on assuming a price for either model. Input/output token split is not available
in judge_routing artifacts (only a single total per call), so dollar figures are
deliberately not produced here.

Zero API: reads only judge_routing_hop{1,2}.json.

Usage:
    python3 -m integrations.memebench.cascade_order_cost --runs-dir <dir> --out <file>
"""

import argparse
import json
from pathlib import Path


def analyse(routing):
    ok = [v for v in routing["verdicts"] if not v.get("error")]
    n = len(ok)
    cheap_fresh = sum(1 for v in ok if not v["j3_cheap"])
    strong_fresh = sum(1 for v in ok if not v["j4_costly"])
    mean_cheap_tok = sum(v["j3_tokens"] for v in ok) / n
    mean_strong_tok = sum(v["j4_tokens"] for v in ok) / n

    p_cheap_fresh = cheap_fresh / n
    p_strong_fresh = strong_fresh / n

    # cheap-first: always cheap, escalate to strong when cheap says fresh
    cf = {"cheap_tok_per_edge": mean_cheap_tok,
          "strong_tok_per_edge": mean_strong_tok * p_cheap_fresh}
    # strong-first: always strong, escalate to cheap when strong says fresh
    sf = {"cheap_tok_per_edge": mean_cheap_tok * p_strong_fresh,
          "strong_tok_per_edge": mean_strong_tok}

    # cost(order) = price_cheap*cheap_tok + price_strong*strong_tok.
    # cheap-first cheaper  <=>  rho > mean_cheap*(1-p_strong_fresh)
    #                                 / (mean_strong*(1-p_cheap_fresh))
    # where rho = price_strong/price_cheap per token.
    breakeven = (mean_cheap_tok * (1 - p_strong_fresh)) / (mean_strong_tok * (1 - p_cheap_fresh))

    return {
        "n_edges": n,
        "cheap_model": routing["cheap_model"],
        "strong_model": routing["costly_model"],
        "pr_cheap_fresh_escalation_rate_cheap_first": p_cheap_fresh,
        "pr_strong_fresh_escalation_rate_strong_first": p_strong_fresh,
        "mean_tokens_cheap_call": mean_cheap_tok,
        "mean_tokens_strong_call": mean_strong_tok,
        "strong_over_cheap_token_ratio": mean_strong_tok / mean_cheap_tok,
        "cheap_first": cf,
        "strong_first": sf,
        "breakeven_price_ratio_strong_over_cheap_per_token": breakeven,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    runs_dir = Path(args.runs_dir)
    report = {}
    for hop in (1, 2):
        routing = json.loads((runs_dir / f"judge_routing_hop{hop}.json").read_text())
        r = analyse(routing)
        report[f"hop{hop}"] = r
        print(f"=== hop{hop}: n={r['n_edges']} (all pos+neg), "
              f"{r['cheap_model']} vs {r['strong_model']} ===")
        print(f"  escalation rate  cheap-first={r['pr_cheap_fresh_escalation_rate_cheap_first']:.4f}"
              f"   strong-first={r['pr_strong_fresh_escalation_rate_strong_first']:.4f}")
        print(f"  mean tokens/call cheap={r['mean_tokens_cheap_call']:.1f}"
              f"  strong={r['mean_tokens_strong_call']:.1f}"
              f"  ({r['strong_over_cheap_token_ratio']:.1f}x)")
        print(f"  cheap-first  cheap_tok/edge={r['cheap_first']['cheap_tok_per_edge']:.1f}"
              f"  strong_tok/edge={r['cheap_first']['strong_tok_per_edge']:.1f}")
        print(f"  strong-first cheap_tok/edge={r['strong_first']['cheap_tok_per_edge']:.1f}"
              f"  strong_tok/edge={r['strong_first']['strong_tok_per_edge']:.1f}")
        print("  cheap-first cheaper iff price_strong/price_cheap > "
              f"{r['breakeven_price_ratio_strong_over_cheap_per_token']:.4f} per token")

    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
