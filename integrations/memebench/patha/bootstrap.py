"""Paired, whole-episode cluster bootstrap with explicit frozen protocol."""

import hashlib
import math
import random
from .scorer import metrics
from contexthub.models.knowledge import ContractError


def percentile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def episode_draws(episode_ids, *, seed, resamples):
    if type(seed) is not int or seed < 0 or resamples != 10000:
        raise ContractError("explicit_10000_bootstrap_protocol_required")
    if not episode_ids or len(set(episode_ids)) != len(episode_ids):
        raise ContractError("bootstrap_episode_universe_invalid")
    rng = random.Random(seed)
    for _ in range(resamples):
        yield tuple(rng.randrange(len(episode_ids)) for _ in episode_ids)


def paired_cluster_bootstrap(rows, *, episode_ids, statistic, seed, resamples):
    """statistic receives a tuple of entire clusters, including repeated draws.

    No individual case, strategy or replicate is sampled separately. Undefined
    samples are counted and invalidate the CI; they are never silently dropped.
    """
    clusters = {episode: [] for episode in episode_ids}
    for row in rows:
        if row["episode_id"] not in clusters:
            raise ContractError("bootstrap_record_outside_frozen_episodes")
        clusters[row["episode_id"]].append(row)
    original = tuple(tuple(clusters[e]) for e in episode_ids)
    point = statistic(original)
    values, unknown = [], 0
    digest = hashlib.sha256()
    for draw in episode_draws(episode_ids, seed=seed, resamples=resamples):
        digest.update((",".join(map(str, draw)) + "\n").encode())
        value = statistic(tuple(original[i] for i in draw))
        if value is None or not math.isfinite(value):
            unknown += 1
        else:
            values.append(value)
    valid = unknown == 0 and point is not None and math.isfinite(point)
    return dict(
        point=point if point is not None and math.isfinite(point) else None,
        lower_one_sided_95=percentile(values, 0.05) if valid else None,
        upper_one_sided_95=percentile(values, 0.95) if valid else None,
        two_sided_95=[percentile(values, 0.025), percentile(values, 0.975)]
        if valid
        else None,
        resamples=resamples,
        seed=seed,
        undefined_resamples=unknown,
        defined=valid,
        draw_indices_sha256=digest.hexdigest(),
        episode_order=list(episode_ids),
        method="paired whole-episode percentile bootstrap; linear quantiles",
        retained_records=len(rows),
    )


def conclusion(interval, rule):
    if not interval["defined"]:
        return None
    return {
        "cost_lower": lambda: interval["upper_one_sided_95"] < 0,
        "recovery_more": lambda: interval["lower_one_sided_95"] > 0,
        "quality_noninferior": lambda: interval["lower_one_sided_95"] >= -0.05,
        "false_fresh_not_worse": lambda: interval["upper_one_sided_95"] <= 0,
    }[rule]()


def paired_rate_statistic(candidate, comparator, *, aggregate):
    """Rows carry arm,numerator,denominator,unknown. All repeats stay in-cluster."""
    if aggregate not in ("pooled_counts", "mean_episode_rates"):
        raise ContractError("aggregation_not_frozen")

    def calculate(clusters):
        rates = []
        for arm in (candidate, comparator):
            counts = []
            for cluster in clusters:
                subset = [r for r in cluster if r["arm"] == arm]
                if not subset or any(r.get("unknown", False) for r in subset):
                    return None
                counts.append(
                    (
                        sum(r["numerator"] for r in subset),
                        sum(r["denominator"] for r in subset),
                    )
                )
            if aggregate == "pooled_counts":
                n, d = map(sum, zip(*counts))
                if not d:
                    return None
                rates.append(n / d)
            else:
                if any(d == 0 for _, d in counts):
                    return None
                rates.append(sum(n / d for n, d in counts) / len(counts))
        return rates[0] - rates[1]

    return calculate


def comparison_report(report, costs, *, episode_ids, protocol):
    """Quality and recovery statistics; cost rows are from ledger exports only.

    cost rows: episode_id, replicate_id, stage(P1/P2), arm, cost(export_cost).
    Missing costs, auxiliary tolerances or statistical support cannot pass.
    """
    result = {}

    def compare(name, rows, candidate, control, rule):
        interval = paired_cluster_bootstrap(
            rows,
            episode_ids=episode_ids,
            statistic=paired_rate_statistic(
                candidate, control, aggregate=protocol.aggregate
            ),
            seed=protocol.bootstrap_seed,
            resamples=protocol.bootstrap_resamples,
        )
        result[name] = dict(
            candidate=candidate,
            comparator=control,
            sign="candidate minus comparator",
            rule=rule,
            passed=conclusion(interval, rule),
            interval=interval,
        )

    for kind in ("Cas", "Abs"):
        for metric in ("internal", "official"):
            data = []
            for ep in episode_ids:
                for arm in ("adaptive", "full/strong-only"):
                    selected = [
                        r
                        for r in report["rows"]
                        if r["episode_id"] == ep
                        and r["profile"] == arm
                        and r["task_type"] == kind
                    ]
                    data.append(
                        dict(
                            episode_id=ep,
                            arm=arm,
                            numerator=sum(
                                r["p2_correct"]
                                if metric == "internal"
                                else r["official_class"] == "real"
                                for r in selected
                            ),
                            denominator=len(selected),
                        )
                    )
            compare(
                metric + "_" + kind,
                data,
                "adaptive",
                "full/strong-only",
                "quality_noninferior",
            )
    # Absolute number of correctly recovered recoverable Cas targets per episode
    # (averaged over the explicitly frozen repeats), not a ratio whose changing
    # denominator can reward abstention. P1 misses remain failures.
    recovery = []
    for ep in episode_ids:
        for arm in ("adaptive", "local/base-only"):
            selected = [
                r
                for r in report["rows"]
                if r["episode_id"] == ep
                and r["profile"] == arm
                and r["task_type"] == "Cas"
            ]
            recovery.append(
                dict(
                    episode_id=ep,
                    arm=arm,
                    denominator=protocol.repeats,
                    numerator=sum(
                        r["recoverable_cas"] is True
                        and r["p1_correct"]
                        and r["p2_correct"]
                        for r in selected
                    ),
                    unknown=any(r["recoverable_cas"] is None for r in selected),
                )
            )
    compare("recovery", recovery, "adaptive", "local/base-only", "recovery_more")
    for kind in ("overall", "Cas", "Abs"):
        data = []
        for ep in episode_ids:
            for arm in ("adaptive", "full/strong-only"):
                selected = [
                    r
                    for r in report["rows"]
                    if r["episode_id"] == ep
                    and r["profile"] == arm
                    and (kind == "overall" or r["task_type"] == kind)
                ]
                data.append(
                    dict(
                        episode_id=ep,
                        arm=arm,
                        numerator=sum(r["false_fresh"] for r in selected),
                        denominator=sum(r["fresh"] is True for r in selected),
                        unknown=any(
                            r["fresh"] is None
                            or (r["fresh"] is True and r["p2_verdict"] == "UNKNOWN")
                            for r in selected
                        ),
                    )
                )
        name = "false_fresh_" + kind
        compare(name, data, "adaptive", "full/strong-only", "false_fresh_not_worse")
        if not result[name]["interval"]["defined"]:
            a = metrics(
                [
                    r
                    for r in report["rows"]
                    if r["profile"] == "adaptive"
                    and (kind == "overall" or r["task_type"] == kind)
                ]
            )
            b = metrics(
                [
                    r
                    for r in report["rows"]
                    if r["profile"] == "full/strong-only"
                    and (kind == "overall" or r["task_type"] == kind)
                ]
            )
            result[name]["na_raw_state_check"] = (
                None
                if any(m["unknown_fresh"] or m["unreviewed_fresh"] for m in (a, b))
                else a["false_fresh_count"] <= b["false_fresh_count"]
                and a["tau_ff_pass"] is True
                and (
                    a["abs_correct_unresolved"] == a["abs_total"]
                    if kind == "Abs"
                    else a["rho_p2_pass"] is True
                )
            )
            # A partially undefined resampling distribution with defined point
            # rates is not the frozen zero-fresh N/A exception.
            if a["false_fresh_main"] is not None and b["false_fresh_main"] is not None:
                result[name]["na_raw_state_check"] = None
    for stage, candidate, control in (
        ("P1", "selective", "full-history"),
        ("P2", "adaptive", "full/strong-only"),
    ):
        selected = [
            c for c in costs if c["stage"] == stage and c["arm"] in (candidate, control)
        ]
        keys = [(r["episode_id"], r["replicate_id"], r["arm"]) for r in selected]
        if len(set(keys)) != len(keys):
            raise ContractError("duplicate_cost_allocation")
        for dimension in ("C_billed", "T_wall"):
            currencies = {r["cost"]["summary"]["currency"] for r in selected}
            data = []
            for ep in episode_ids:
                for arm in (candidate, control):
                    vals = [
                        c for c in selected if c["episode_id"] == ep and c["arm"] == arm
                    ]
                    valid = {v["replicate_id"] for v in vals} == set(
                        range(protocol.repeats)
                    ) and all(v["cost"][dimension] is not None for v in vals)
                    if dimension == "C_billed" and len(currencies) > 1:
                        valid = False
                    data.append(
                        dict(
                            episode_id=ep,
                            arm=arm,
                            numerator=sum(float(v["cost"][dimension]) for v in vals)
                            if valid
                            else 0,
                            denominator=protocol.repeats,
                            unknown=not valid,
                        )
                    )
            compare(stage + "_" + dimension, data, candidate, control, "cost_lower")
        # Auxiliary workload tolerances are explicit. Missing/unmeasured local
        # dimensions and wall measurements prevent a complete-cost claim.
        auxiliaries = []
        for ep in episode_ids:
            for rep in range(protocol.repeats):
                pair = {
                    r["arm"]: r["cost"]
                    for r in selected
                    if r["episode_id"] == ep and r["replicate_id"] == rep
                }
                if (
                    set(pair) != {candidate, control}
                    or protocol.wall_relative_tolerance is None
                    or protocol.local_relative_tolerances is None
                ):
                    auxiliaries.append(None)
                    continue
                a, b = pair[candidate], pair[control]
                if (
                    a["T_wall"] is None
                    or b["T_wall"] is None
                    or any(x["summary"]["incomplete_attempts"] for x in (a, b))
                ):
                    auxiliaries.append(None)
                    continue
                observed = set(a["W_local"]) | set(b["W_local"])
                if not observed <= set(protocol.local_relative_tolerances):
                    auxiliaries.append(None)
                    continue
                auxiliaries.append(
                    a["T_wall"] <= b["T_wall"] * (1 + protocol.wall_relative_tolerance)
                    and all(
                        a["W_local"].get(k, 0)
                        <= b["W_local"].get(k, 0)
                        * (1 + protocol.local_relative_tolerances[k])
                        for k in observed
                    )
                )
        result[stage + "_complete_cost_lower"] = (
            None
            if None in auxiliaries or result[stage + "_C_billed"]["passed"] is None
            else all(auxiliaries) and result[stage + "_C_billed"]["passed"]
        )
    return dict(
        protocol=protocol.model_dump(mode="json"),
        comparisons=result,
        cost_basis="core usage and frozen simple tariffs, not provider invoices; complete-cost means this monetary axis plus explicit wall/local-work tolerances",
        note="Undefined intervals and unfrozen auxiliary tolerances never certify success.",
    )
