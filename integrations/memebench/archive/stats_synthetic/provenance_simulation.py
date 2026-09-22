"""Synthetic provenance capture/pruning simulation.

This experiment is independent of MEME.  It generates a true dependency DAG,
simulates runtime read-set capture, and compares:

* keep-all: retain every captured read edge;
* prune: remove irrelevant reads, while risking false-negative removal of true
  captured dependencies.

MEME gold edges are not provenance and are not used by this simulation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Iterable, Sequence

Edge = tuple[int, int]


@dataclass(frozen=True)
class SimulationPoint:
    policy: str
    capture_probability: float
    irrelevant_read_inflation: float
    pruning_false_negative_risk: float
    n_trials: int
    true_edges: int
    captured_true_edges: int
    predicted_edges: int
    true_positive_edges: int
    graph_miss_count: int
    capture_coverage: float
    graph_miss_rate: float
    precision: float
    edge_inflation: float


def _probability(value: float, name: str) -> float:
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return value


def _rng(seed: str, *parts: object) -> random.Random:
    material = "\0".join([seed, *(str(part) for part in parts)])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest, "big"))


def generate_true_dag(
    *,
    n_nodes: int,
    dependency_probability: float,
    rng: random.Random,
) -> set[Edge]:
    """Generate an ordered DAG: every possible edge points low -> high."""

    if n_nodes < 2:
        raise ValueError("n_nodes must be at least 2")
    probability = _probability(dependency_probability, "dependency_probability")
    edges = {
        (source, dependent)
        for dependent in range(1, n_nodes)
        for source in range(dependent)
        if rng.random() < probability
    }
    # Avoid degenerate no-dependency trials, where graph-miss is undefined as a
    # capture target and precision/inflation direction becomes uninformative.
    if not edges:
        dependent = rng.randrange(1, n_nodes)
        edges.add((rng.randrange(dependent), dependent))
    return edges


def simulate_read_set(
    true_edges: set[Edge],
    *,
    n_nodes: int,
    capture_probability: float,
    irrelevant_read_inflation: float,
    rng: random.Random,
) -> tuple[set[Edge], set[Edge]]:
    """Return (captured true edges, full recorded read set).

    ``irrelevant_read_inflation`` is the probability that each causally
    irrelevant earlier-node read is nevertheless present in the recorded set.
    """

    capture = _probability(capture_probability, "capture_probability")
    inflation = _probability(
        irrelevant_read_inflation, "irrelevant_read_inflation"
    )
    captured = {edge for edge in true_edges if rng.random() < capture}
    irrelevant_candidates = {
        (source, dependent)
        for dependent in range(1, n_nodes)
        for source in range(dependent)
        if (source, dependent) not in true_edges
    }
    irrelevant = {
        edge for edge in irrelevant_candidates if rng.random() < inflation
    }
    return captured, captured | irrelevant


def prune_read_set(
    reads: set[Edge],
    true_edges: set[Edge],
    *,
    false_negative_risk: float,
    irrelevant_keep_probability: float,
    rng: random.Random,
) -> set[Edge]:
    """Prune reads, dropping true edges at the requested FN risk.

    The separate irrelevant keep probability models imperfect but useful
    pruning precision.  It defaults to 0.1 in the sweep CLI.
    """

    false_negative = _probability(
        false_negative_risk, "false_negative_risk"
    )
    irrelevant_keep = _probability(
        irrelevant_keep_probability, "irrelevant_keep_probability"
    )
    kept: set[Edge] = set()
    for edge in reads:
        keep_probability = (
            1.0 - false_negative if edge in true_edges else irrelevant_keep
        )
        if rng.random() < keep_probability:
            kept.add(edge)
    return kept


def _summarize(
    *,
    policy: str,
    capture_probability: float,
    inflation: float,
    false_negative_risk: float,
    trials: Sequence[tuple[set[Edge], set[Edge], set[Edge]]],
) -> SimulationPoint:
    true_total = sum(len(true) for true, _, _ in trials)
    captured_total = sum(len(captured) for _, captured, _ in trials)
    pred_total = sum(len(predicted) for _, _, predicted in trials)
    tp_total = sum(len(true & predicted) for true, _, predicted in trials)
    misses = sum(not true.issubset(predicted) for true, _, predicted in trials)
    return SimulationPoint(
        policy=policy,
        capture_probability=capture_probability,
        irrelevant_read_inflation=inflation,
        pruning_false_negative_risk=false_negative_risk,
        n_trials=len(trials),
        true_edges=true_total,
        captured_true_edges=captured_total,
        predicted_edges=pred_total,
        true_positive_edges=tp_total,
        graph_miss_count=misses,
        capture_coverage=captured_total / true_total,
        graph_miss_rate=misses / len(trials),
        precision=tp_total / pred_total if pred_total else 1.0,
        edge_inflation=pred_total / true_total,
    )


def run_simulation(
    *,
    n_trials: int,
    n_nodes: int,
    dependency_probability: float,
    capture_probabilities: Iterable[float],
    irrelevant_read_inflations: Iterable[float],
    pruning_false_negative_risks: Iterable[float],
    irrelevant_keep_probability: float = 0.1,
    seed: str,
) -> dict:
    """Run a paired sweep over capture, read inflation, and pruning FN risk."""

    if n_trials <= 0:
        raise ValueError("n_trials must be positive")
    if not seed:
        raise ValueError("seed must be explicit and non-empty")
    dependency_probability = _probability(
        dependency_probability, "dependency_probability"
    )
    irrelevant_keep_probability = _probability(
        irrelevant_keep_probability, "irrelevant_keep_probability"
    )
    captures = [
        _probability(value, "capture_probability")
        for value in capture_probabilities
    ]
    inflations = [
        _probability(value, "irrelevant_read_inflation")
        for value in irrelevant_read_inflations
    ]
    false_negatives = [
        _probability(value, "pruning_false_negative_risk")
        for value in pruning_false_negative_risks
    ]
    if not captures or not inflations or not false_negatives:
        raise ValueError("all three sweep dimensions must be non-empty")

    points: list[SimulationPoint] = []
    for capture, inflation in product(captures, inflations):
        bases: list[tuple[set[Edge], set[Edge], set[Edge]]] = []
        for trial_index in range(n_trials):
            true = generate_true_dag(
                n_nodes=n_nodes,
                dependency_probability=dependency_probability,
                rng=_rng(seed, "dag", trial_index),
            )
            captured, reads = simulate_read_set(
                true,
                n_nodes=n_nodes,
                capture_probability=capture,
                irrelevant_read_inflation=inflation,
                rng=_rng(seed, "reads", capture, inflation, trial_index),
            )
            bases.append((true, captured, reads))

        points.append(
            _summarize(
                policy="keep-all",
                capture_probability=capture,
                inflation=inflation,
                false_negative_risk=0.0,
                trials=bases,
            )
        )
        for false_negative in false_negatives:
            pruned_trials = []
            for trial_index, (true, captured, reads) in enumerate(bases):
                predicted = prune_read_set(
                    reads,
                    true,
                    false_negative_risk=false_negative,
                    irrelevant_keep_probability=irrelevant_keep_probability,
                    rng=_rng(
                        seed,
                        "prune",
                        capture,
                        inflation,
                        false_negative,
                        trial_index,
                    ),
                )
                pruned_trials.append((true, captured, predicted))
            points.append(
                _summarize(
                    policy="prune",
                    capture_probability=capture,
                    inflation=inflation,
                    false_negative_risk=false_negative,
                    trials=pruned_trials,
                )
            )

    return {
        "method": {
            "data_source": "synthetic DAG/read-set simulation; no MEME gold",
            "true_dependency_graph": "ordered random DAG",
            "capture": "each true dependency independently recorded with probability p",
            "irrelevant_reads": (
                "each causally irrelevant earlier-node edge independently recorded "
                "with the inflation probability"
            ),
            "keep_all": "retain every recorded read edge",
            "prune": (
                "drop a captured true dependency with pruning false-negative risk; "
                "retain an irrelevant read with irrelevant_keep_probability"
            ),
            "graph_miss": "at least one true dependency absent from predicted edges",
            "edge_inflation": "predicted edge count / true dependency edge count",
        },
        "seed": seed,
        "n_trials": n_trials,
        "n_nodes": n_nodes,
        "dependency_probability": dependency_probability,
        "irrelevant_keep_probability": irrelevant_keep_probability,
        "points": [asdict(point) for point in points],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--nodes", type=int, default=20)
    parser.add_argument("--dependency-probability", type=float, default=0.08)
    parser.add_argument(
        "--capture-probabilities", nargs="+", type=float, default=[0.9, 0.95, 0.99]
    )
    parser.add_argument(
        "--irrelevant-read-inflations", nargs="+", type=float, default=[0.0, 0.05, 0.2]
    )
    parser.add_argument(
        "--pruning-false-negative-risks", nargs="+", type=float, default=[0.0, 0.01, 0.05]
    )
    parser.add_argument("--irrelevant-keep-probability", type=float, default=0.1)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run_simulation(
        n_trials=args.trials,
        n_nodes=args.nodes,
        dependency_probability=args.dependency_probability,
        capture_probabilities=args.capture_probabilities,
        irrelevant_read_inflations=args.irrelevant_read_inflations,
        pruning_false_negative_risks=args.pruning_false_negative_risks,
        irrelevant_keep_probability=args.irrelevant_keep_probability,
        seed=args.seed,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out is None:
        print(rendered, end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
