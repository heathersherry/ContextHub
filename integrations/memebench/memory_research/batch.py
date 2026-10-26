"""Portable MEME-32k batch entry point; preflight is offline and free of model calls."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .cli import DATA_SHA, TASKS, argument_parser
from .official import load_official

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "experiments/meme32k/config.json"


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def command(case, args):
    return [
        sys.executable, "-m", "integrations.memebench.memory_research.cli",
        "--config", str(CONFIG), "--data", str(args.data),
        "--official-judge", str(args.official_judge), "--mode", "check",
        "--episode", case, "--output", str(args.output / case),
        "--memory-provider", "deepseek", "--memory-model", "deepseek-flash",
        "--embedding-provider", "aliyun", "--embedding-model", "qwen3.7-text-embedding",
        "--embedding-dimensions", "1536", "--p1-backend", "jev",
        "--p1-jev-threshold", "0.5", "--topic-similarity-threshold", "0.8",
        "--p2-direct-backend", "jev", "--rule-gate-backend", "jev",
    ]


def successful(folder, case):
    summary = folder / "summary.json"
    scored = folder / (case + ".scored.json")
    if not summary.exists() or not scored.exists():
        return False
    s, result = read(summary), read(scored)
    return (
        s["completed"] == 1 and s["unjudged"] == 0
        and all(not q.get("error") for phase in ("before", "after")
                for q in result[phase + "_answers"])
    )


def aggregate(output, cases, chosen):
    totals = {task: {"passed": 0, "total": 0} for task in TASKS}
    for folder in chosen.values():
        for task, row in read(output / folder / "summary.json")["tasks"].items():
            for field in ("passed", "total"):
                totals[task][field] += row[field]
    return {
        "expected_cases": len(cases), "completed_cases": len(chosen),
        "complete": len(chosen) == len(cases),
        "missing_cases": [c for c in cases if c not in chosen],
        "selected_runs": chosen, "tasks": totals,
        "passed": sum(t["passed"] for t in totals.values()),
        "total_scored": sum(t["total"] for t in totals.values()),
        "score_scope": "Completed cases only; not a full benchmark score unless complete=true.",
    }


def preflight(args):
    data = args.data.read_bytes()
    if hashlib.sha256(data).hexdigest() != DATA_SHA:
        raise ValueError("Expected the pinned MEME filler32k JSON; dataset hash differs")
    episodes = json.loads(data)
    cases = [e["episode_id"] for e in episodes]
    counts = Counter(q["task_type"].split(" (")[0]
                     for e in episodes for q in e["after_questions"]["questions"])
    if len(set(cases)) != 100 or len(cases) != 100 or counts != dict(Cas=164, Abs=130, Del=100, ER=100, Agg=100, Tr=100):
        raise ValueError("Unexpected episode/task counts")
    load_official(args.official_judge)
    if not args.case:
        metadata = read(ROOT / "experiments/meme32k/baseline-source.json")
        selected = read(ROOT / "experiments/meme32k/historical-cases.json")
        cases = [r["case"] for r in selected["cases"] if r["source_tag"] == metadata["tag"]]
    if args.case:
        if args.case not in cases:
            raise ValueError("Unknown case")
        cases = [args.case]
    # Validate the actual underlying CLI contract without invoking it.
    argument_parser().parse_args(command(cases[0], args)[3:])
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT):
        raise ValueError("Commit tracked changes before running a reproducible batch")
    return {
        "commit": commit, "dataset_sha256": DATA_SHA,
        "judge_sha256": hashlib.sha256(args.official_judge.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
        "cases": cases, "execution": "serial; fresh run per attempted case",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--official-judge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", help="Smoke-test one case instead of all 100")
    parser.add_argument("--preflight", action="store_true", help="Validate inputs only; no keys, database or APIs")
    parser.add_argument("--resume", action="store_true", help="Skip successful cases in an existing batch")
    parser.add_argument("--retry-failed", action="store_true", help="With --resume, rerun failed cases from session 1")
    args = parser.parse_args()
    args.data, args.official_judge, args.output = (p.resolve() for p in (args.data, args.official_judge, args.output))
    manifest = preflight(args)
    if args.preflight:
        print(json.dumps(manifest, indent=2))
        return
    if args.retry_failed and not args.resume:
        parser.error("--retry-failed requires --resume")
    if not os.environ.get("MEMORY_DATABASE_URL"):
        parser.error("Set MEMORY_DATABASE_URL to an initialized dedicated database")
    providers = read(ROOT / "model_providers.local.json")["targets"]
    selected = {p["label"]: p for p in providers if p["label"] in ("deepseek", "aliyun", "openlux")}
    if set(selected) != {"deepseek", "aliyun", "openlux"} or any(
        not p.get("api_key") or p["api_key"].startswith("REPLACE_") for p in selected.values()
    ):
        parser.error("Fill all three providers in model_providers.local.json")
    from dotenv import dotenv_values
    if not (os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env").get("TYPESAFE_API_KEY")):
        parser.error("Set TYPESAFE_API_KEY")
    manifest["providers"] = {k: p["base_url"] for k, p in selected.items()}
    if args.resume:
        if read(args.output / "batch.json") != manifest:
            parser.error("Resume requires the same commit, cases, dataset, judge, config and endpoints")
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        save(args.output / "batch.json", manifest)
    chosen = {}
    for case in manifest["cases"]:
        attempts = sorted((args.output / case).glob("memory-check-*"))
        good = [p for p in attempts if successful(p, case)]
        if good:
            chosen[case] = str(good[-1].relative_to(args.output))
        elif attempts and not args.retry_failed:
            print(f"{case}: incomplete attempt preserved; use --resume --retry-failed", flush=True)
        else:
            print(f"{case}: starting", flush=True)
            with (args.output / (case + ".log")).open("a") as log:
                subprocess.run(command(case, args), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
            good = [p for p in sorted((args.output / case).glob("memory-check-*")) if successful(p, case)]
            if good:
                chosen[case] = str(good[-1].relative_to(args.output))
        save(args.output / "summary.json", aggregate(args.output, manifest["cases"], chosen))
    summary = aggregate(args.output, manifest["cases"], chosen)
    print(json.dumps(summary, indent=2))
    if not summary["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
