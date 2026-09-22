"""Select and independently certify one frozen MEME graph-building policy.

The certification event is deliberately episode-level: an episode is a graph
miss when ``n_tp < n_gold``.  On MEME's deduplicated tree workload this means
that the complete gold path/edge set was not retained.  It is a conservative
workload-specific event target, not a claim about arbitrary provenance graphs.

Selection sees only the deterministic selection split.  It chooses at most one
policy, freezes it, and evaluates that policy exactly once on certification.
A certification failure is therefore a No-Go; this module never falls back to
another policy after looking at certification data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# Moved to integrations/memebench/common.py so this finished experiment could be archived.
from integrations.memebench.common import (
    EpisodeResult,
    PolicyCandidate,
    clopper_pearson_upper,
    stable_episode_split,
)












def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _episode_from_case(case: Mapping[str, Any]) -> EpisodeResult:
    episode_id = str(case.get("episode_id", ""))
    if not episode_id:
        raise ValueError("case is missing episode_id")
    n_gold = int(_finite_number(case.get("n_gold"), field="n_gold"))
    n_pred = int(_finite_number(case.get("n_pred"), field="n_pred"))
    n_tp = int(_finite_number(case.get("n_tp"), field="n_tp"))
    if min(n_gold, n_pred, n_tp) < 0 or n_tp > min(n_gold, n_pred):
        raise ValueError(f"invalid edge counts for episode {episode_id}")

    expected_precision = n_tp / n_pred if n_pred else 0.0
    expected_recall = n_tp / n_gold if n_gold else 1.0
    precision = _finite_number(
        case.get("precision", expected_precision), field="precision"
    )
    recall = _finite_number(case.get("recall", expected_recall), field="recall")
    if not math.isclose(precision, expected_precision, abs_tol=1e-8):
        raise ValueError(f"inconsistent precision for episode {episode_id}")
    if not math.isclose(recall, expected_recall, abs_tol=1e-8):
        raise ValueError(f"inconsistent recall for episode {episode_id}")

    if "cheap_tokens" in case or "strong_tokens" in case:
        cheap = _finite_number(case.get("cheap_tokens", 0), field="cheap_tokens")
        strong = _finite_number(case.get("strong_tokens", 0), field="strong_tokens")
    else:
        token_fields = (
            "discovery_tokens",
            "total_discovery_tokens",
            "total_tokens",
            "tokens",
        )
        present = next((name for name in token_fields if name in case), None)
        if present is None:
            raise ValueError(f"case {episode_id} has no supported token-cost field")
        cheap = _finite_number(case[present], field=present)
        strong = 0.0
    if cheap < 0 or strong < 0:
        raise ValueError(f"negative token cost for episode {episode_id}")
    return EpisodeResult(
        episode_id=episode_id,
        n_gold=n_gold,
        n_pred=n_pred,
        n_tp=n_tp,
        precision=precision,
        recall=recall,
        cheap_tokens=cheap,
        strong_tokens=strong,
    )


def _policy_label(result: Mapping[str, Any], index: int) -> tuple[str, dict[str, Any]]:
    preferred = ("lam", "tau", "model", "filter", "policy", "threshold")
    parameters: dict[str, Any] = {}
    for key in preferred:
        if key in result:
            parameters[key] = result[key]
    if not parameters:
        parameters["result_index"] = index
    label = ",".join(f"{key}={_display_value(value)}" for key, value in parameters.items())
    return label, parameters


def _display_value(value: Any) -> str:
    if isinstance(value, float) and math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return str(value)


def load_policy_menu(paths: Sequence[str | Path]) -> list[PolicyCandidate]:
    """Load generic MEME sweep JSON and require exact episode-set alignment."""

    if not paths:
        raise ValueError("at least one sweep JSON is required")
    policies: list[PolicyCandidate] = []
    expected_ids: set[str] | None = None
    for raw_path in paths:
        path = Path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise ValueError(f"{path}: expected a non-empty results list")
        for index, result in enumerate(results):
            if not isinstance(result, dict) or not isinstance(result.get("cases"), list):
                raise ValueError(f"{path}: result {index} has no cases list")
            episodes = tuple(_episode_from_case(case) for case in result["cases"])
            ids = [episode.episode_id for episode in episodes]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{path}: duplicate episode ID in result {index}")
            id_set = set(ids)
            if expected_ids is None:
                expected_ids = id_set
            elif id_set != expected_ids:
                missing = sorted(expected_ids - id_set)
                extra = sorted(id_set - expected_ids)
                raise ValueError(
                    f"{path}: episode set mismatch in result {index}; "
                    f"missing={missing[:5]}, extra={extra[:5]}"
                )
            label, parameters = _policy_label(result, index)
            policy_id = f"{path.name}:{label}"
            edge_mix = result.get("edge_tier_mix")
            cheap_none = None
            if isinstance(edge_mix, dict) and "cheap_none" in edge_mix:
                cheap_none = int(edge_mix["cheap_none"])
            policies.append(
                PolicyCandidate(
                    policy_id=policy_id,
                    source=str(path),
                    parameters=parameters,
                    episodes=tuple(sorted(episodes, key=lambda item: item.episode_id)),
                    cheap_none=cheap_none,
                )
            )
    ids = [policy.policy_id for policy in policies]
    if len(ids) != len(set(ids)):
        raise ValueError("policy IDs collide; use sweep files with distinct names")
    return policies


def _metrics(policy: PolicyCandidate, episode_ids: Iterable[str]) -> dict[str, Any]:
    selected = set(episode_ids)
    episodes = [episode for episode in policy.episodes if episode.episode_id in selected]
    if len(episodes) != len(selected):
        raise ValueError(f"{policy.policy_id}: requested unknown episode ID")
    misses = sum(episode.graph_miss for episode in episodes)
    n_gold = sum(episode.n_gold for episode in episodes)
    n_pred = sum(episode.n_pred for episode in episodes)
    n_tp = sum(episode.n_tp for episode in episodes)
    total_tokens = sum(episode.total_tokens for episode in episodes)
    return {
        "n_episodes": len(episodes),
        "graph_miss_count": misses,
        "graph_miss_rate": misses / len(episodes) if episodes else None,
        "edge_precision": n_tp / n_pred if n_pred else 0.0,
        "edge_recall": n_tp / n_gold if n_gold else 1.0,
        "cheap_tokens": sum(episode.cheap_tokens for episode in episodes),
        "strong_tokens": sum(episode.strong_tokens for episode in episodes),
        "total_tokens": total_tokens,
        "tokens_per_episode": total_tokens / len(episodes) if episodes else None,
    }


def run_certification(
    policies: Sequence[PolicyCandidate],
    *,
    seed: str,
    selection_fraction: float,
    epsilon: float,
    alpha: float,
    selection_risk_limit: float | None = None,
    max_tokens_per_episode: float | None = None,
    cp_upper: Callable[[int, int, float], float] = clopper_pearson_upper,
) -> dict[str, Any]:
    """Select one policy on selection data, then certify it once."""

    if not policies:
        raise ValueError("policy menu is empty")
    if not 0.0 <= epsilon <= 1.0:
        raise ValueError("epsilon must be in [0, 1]")
    if selection_risk_limit is None:
        selection_risk_limit = epsilon
    if not 0.0 <= selection_risk_limit <= 1.0:
        raise ValueError("selection_risk_limit must be in [0, 1]")
    episode_ids = [episode.episode_id for episode in policies[0].episodes]
    expected_ids = set(episode_ids)
    for policy in policies[1:]:
        if {episode.episode_id for episode in policy.episodes} != expected_ids:
            raise ValueError("all policies must have exactly aligned episode sets")
    split = stable_episode_split(
        episode_ids, seed=seed, selection_fraction=selection_fraction
    )

    menu: list[dict[str, Any]] = []
    eligible: list[tuple[float, float, str, PolicyCandidate]] = []
    for policy in policies:
        selection = _metrics(policy, split.selection_ids)
        risk_ok = selection["graph_miss_rate"] <= selection_risk_limit
        cost_ok = (
            max_tokens_per_episode is None
            or selection["tokens_per_episode"] <= max_tokens_per_episode
        )
        entry = {
            "policy_id": policy.policy_id,
            "source": policy.source,
            "parameters": dict(policy.parameters),
            "selection": selection,
            "selection_risk_ok": risk_ok,
            "selection_cost_ok": cost_ok,
            "cheap_none_full_sweep": policy.cheap_none,
        }
        menu.append(entry)
        if risk_ok and cost_ok:
            eligible.append(
                (
                    selection["tokens_per_episode"],
                    selection["graph_miss_rate"],
                    policy.policy_id,
                    policy,
                )
            )

    selected_policy = min(eligible)[3] if eligible else None
    certification = None
    decision = "No-Go"
    if selected_policy is not None:
        cert_metrics = _metrics(selected_policy, split.certification_ids)
        upper = cp_upper(
            cert_metrics["graph_miss_count"], cert_metrics["n_episodes"], alpha
        )
        certification = {
            **cert_metrics,
            "U_graph": upper,
            "passes": upper <= epsilon,
            "evaluations_after_freeze": 1,
        }
        decision = "Go" if certification["passes"] else "No-Go"

    return {
        "method": {
            "event_target": "episode graph miss iff n_tp < n_gold",
            "scope": (
                "On the MEME tree gold workload this conservatively requires the "
                "complete gold path/edge set. It does not establish provenance "
                "capture or general-DAG correctness."
            ),
            "selection_rule": (
                "Among policies passing selection empirical graph-miss risk and "
                "the optional cost cap, minimize selection tokens/episode; tie-break "
                "by miss rate then policy ID."
            ),
            "freeze_rule": (
                "Certification evaluates only the frozen selected policy once. "
                "Failure is No-Go; no certification-driven fallback is allowed."
            ),
            "confidence_bound": "one-sided exact Clopper-Pearson",
        },
        "epsilon": epsilon,
        "alpha": alpha,
        "selection_risk_limit": selection_risk_limit,
        "max_tokens_per_episode": max_tokens_per_episode,
        "split": asdict(split),
        "policy_menu": menu,
        "selected_policy_id": (
            selected_policy.policy_id if selected_policy is not None else None
        ),
        "certification": certification,
        "decision": decision,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweeps", nargs="+", help="one or more MEME sweep JSON files")
    parser.add_argument("--seed", required=True, help="explicit stable split seed")
    parser.add_argument("--selection-fraction", type=float, required=True)
    parser.add_argument("--epsilon", type=float, required=True)
    parser.add_argument("--alpha", type=float, required=True)
    parser.add_argument(
        "--selection-risk-limit",
        type=float,
        default=None,
        help="empirical selection graph-miss cap (default: epsilon)",
    )
    parser.add_argument(
        "--max-tokens-per-episode",
        type=float,
        default=None,
        help="optional selection cost target/cap",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    report = run_certification(
        load_policy_menu(args.sweeps),
        seed=args.seed,
        selection_fraction=args.selection_fraction,
        epsilon=args.epsilon,
        alpha=args.alpha,
        selection_risk_limit=args.selection_risk_limit,
        max_tokens_per_episode=args.max_tokens_per_episode,
    )
    rendered = json.dumps(_json_safe(report), ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
