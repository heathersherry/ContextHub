"""Episode-cluster-aware inference for the cheap/strong judge 2x2 table.

Plain Fisher/McNemar treat each change-edge as IID, but edges are nested in
episodes. This script re-derives the same effect sizes with episode-level
cluster bootstrap (percentile CI) and cluster permutation (sign-flip at the
episode level), so significance does not rely on the independence assumption.

Zero API: reads only judge_routing_hop{1,2}.json + neg_edge_set_hop{1,2}.json.

Usage:
    python3 -m integrations.memebench.cluster_inference --runs-dir <dir> --iters 20000
"""

import argparse
import json
import math
import random
from pathlib import Path


def load(runs_dir: Path, hop: int):
    """Return per-positive-edge records: (episode_id, cheap_miss, strong_miss)."""
    routing = json.loads((runs_dir / f"judge_routing_hop{hop}.json").read_text())
    edges = json.loads((runs_dir / f"neg_edge_set_hop{hop}.json").read_text())["edges"]
    verdicts = routing["verdicts"]
    if len(verdicts) != len(edges):
        raise SystemExit(f"hop{hop}: length mismatch {len(verdicts)} vs {len(edges)}")

    recs = []
    for v, e in zip(verdicts, edges):
        if v["should_stale"] != e["should_stale"] or v.get("neg_class") != e.get("neg_class"):
            raise SystemExit(f"hop{hop}: positional join broken")
        if not v["should_stale"] or v.get("error"):
            continue
        # miss == judged fresh on an edge that should have gone stale
        recs.append((e["episode_id"], not v["j3_cheap"], not v["j4_costly"]))
    return recs


def table(recs):
    """2x2 over (cheap_miss, strong_miss): a=both, b=cheap only, c=strong only, d=neither."""
    a = b = c = d = 0
    for _, cm, sm in recs:
        if cm and sm:
            a += 1
        elif cm:
            b += 1
        elif sm:
            c += 1
        else:
            d += 1
    return a, b, c, d


def stats(recs):
    n = len(recs)
    a, b, c, d = table(recs)
    cheap = (a + b) / n
    strong = (a + c) / n
    return {
        "n": n,
        "a_both_miss": a,
        "b_cheap_only": b,
        "c_strong_only": c,
        "d_neither": d,
        "cheap_miss": cheap,
        "strong_miss": strong,
        "cascade_miss": a / n,
        "independent_product": cheap * strong,
        "ratio_vs_independent": (a / n) / (cheap * strong) if cheap * strong else float("nan"),
        "log_or": math.log((a + 0.5) * (d + 0.5) / ((b + 0.5) * (c + 0.5))),
        "miss_diff": strong - cheap,
    }


def by_episode(recs):
    groups = {}
    for ep, cm, sm in recs:
        groups.setdefault(ep, []).append((ep, cm, sm))
    return list(groups.values())


def icc_design_effect(recs, idx):
    """One-way ANOVA estimator of ICC and the resulting design effect.

    idx selects the outcome: 0 = cheap miss, 1 = strong miss (in the (cm, sm)
    pair after stripping the episode id). design_effect = 1 + (mbar-1)*ICC is
    the factor by which clustering inflates the variance of a proportion, so
    n/design_effect is the effective sample size.
    """
    clusters = by_episode(recs)
    m, n = len(clusters), len(recs)
    mbar = n / m
    vals = [r[idx + 1] for r in recs]
    p = sum(vals) / n
    msb = sum(len(c) * ((sum(x[idx + 1] for x in c) / len(c)) - p) ** 2 for c in clusters) / (m - 1)
    msw = sum(
        sum((x[idx + 1] - sum(y[idx + 1] for y in c) / len(c)) ** 2 for x in c)
        for c in clusters
    ) / (n - m)
    icc = (msb - msw) / (msb + (mbar - 1) * msw)
    deff = 1 + (mbar - 1) * icc
    return {
        "p": p,
        "n_clusters": m,
        "mean_cluster_size": mbar,
        "icc": icc,
        "design_effect": deff,
        "effective_n": n / deff,
    }


def bootstrap(recs, iters, rng):
    """Resample episodes with replacement; percentile CI on each statistic."""
    clusters = by_episode(recs)
    keys = ("log_or", "ratio_vs_independent", "miss_diff", "cascade_miss")
    draws = {k: [] for k in keys}
    for _ in range(iters):
        sample = []
        for _ in range(len(clusters)):
            sample.extend(rng.choice(clusters))
        s = stats(sample)
        for k in keys:
            draws[k].append(s[k])
    out = {}
    for k, vals in draws.items():
        vals.sort()
        lo = vals[int(0.025 * (len(vals) - 1))]
        hi = vals[int(0.975 * (len(vals) - 1))]
        out[k] = (lo, hi)
    return out


def permute_association(recs, iters, rng):
    """H0: cheap/strong misses are independent within an episode.

    Permuting the strong-miss vector across edges *within* each episode destroys
    the pairing while preserving both marginals and the episode clustering.
    """
    clusters = by_episode(recs)
    obs = stats(recs)["log_or"]
    ge = 0
    for _ in range(iters):
        shuffled = []
        for cl in clusters:
            strong = [sm for _, _, sm in cl]
            rng.shuffle(strong)
            shuffled.extend((ep, cm, sm) for (ep, cm, _), sm in zip(cl, strong))
        if stats(shuffled)["log_or"] >= obs:
            ge += 1
    return obs, (ge + 1) / (iters + 1)


def permute_marginals(recs, iters, rng):
    """H0: cheap and strong have equal miss rates (cluster-level sign flip).

    Swapping the two tiers' labels for a whole episode is the cluster analogue
    of McNemar's within-pair exchange.
    """
    clusters = by_episode(recs)
    obs = stats(recs)["miss_diff"]
    ge = 0
    for _ in range(iters):
        flipped = []
        for cl in clusters:
            if rng.random() < 0.5:
                flipped.extend((ep, sm, cm) for ep, cm, sm in cl)
            else:
                flipped.extend(cl)
        if abs(stats(flipped)["miss_diff"]) >= abs(obs):
            ge += 1
    return obs, (ge + 1) / (iters + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", required=True)
    ap.add_argument("--iters", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    runs_dir = Path(args.runs_dir)
    report = {"iters": args.iters, "seed": args.seed, "hops": {}}

    for hop in (1, 2):
        recs = load(runs_dir, hop)
        rng = random.Random(args.seed + hop)
        s = stats(recs)
        s["n_episodes"] = len(by_episode(recs))
        ci = bootstrap(recs, args.iters, rng)
        or_obs, or_p = permute_association(recs, args.iters, rng)
        md_obs, md_p = permute_marginals(recs, args.iters, rng)

        s["cluster_bootstrap_ci95"] = {k: list(v) for k, v in ci.items()}
        s["design_effect"] = {
            "cheap_miss": icc_design_effect(recs, 0),
            "strong_miss": icc_design_effect(recs, 1),
        }
        s["cluster_permutation"] = {
            "log_or": {"observed": or_obs, "p_one_sided": or_p},
            "miss_diff": {"observed": md_obs, "p_two_sided": md_p},
        }
        report["hops"][f"hop{hop}"] = s

        print(f"=== hop{hop}: n={s['n']} edges in {s['n_episodes']} episodes ===")
        print(f"  2x2 a/b/c/d = {s['a_both_miss']}/{s['b_cheap_only']}/{s['c_strong_only']}/{s['d_neither']}")
        print(f"  cheap miss {s['cheap_miss']:.4f}  strong miss {s['strong_miss']:.4f}")
        print(f"  cascade {s['cascade_miss']:.4f} vs independent {s['independent_product']:.4f}"
              f"  ratio {s['ratio_vs_independent']:.2f}x  CI95 {ci['ratio_vs_independent'][0]:.2f}-{ci['ratio_vs_independent'][1]:.2f}")
        print(f"  log OR {s['log_or']:.3f} (OR {math.exp(s['log_or']):.1f})"
              f"  CI95 log {ci['log_or'][0]:.3f}-{ci['log_or'][1]:.3f}"
              f"  (OR {math.exp(ci['log_or'][0]):.1f}-{math.exp(ci['log_or'][1]):.1f})"
              f"  perm p={or_p:.5f}")
        print(f"  strong-cheap miss diff {md_obs:+.4f}"
              f"  CI95 {ci['miss_diff'][0]:+.4f}-{ci['miss_diff'][1]:+.4f}  perm p={md_p:.5f}")
        for lab, de in s["design_effect"].items():
            print(f"  {lab:11s} ICC={de['icc']:.3f} design_effect={de['design_effect']:.2f}"
                  f" effective_n={de['effective_n']:.0f} (raw {s['n']})")

    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
