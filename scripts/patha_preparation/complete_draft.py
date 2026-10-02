"""Offline schema/knowledge templates, exact official prompt identities and scope counts."""

import sys
import offline_guard  # noqa: F401
import hashlib
import json
from pathlib import Path
from integrations.memebench.patha.run_gates import FileIdentity, RunManifest
from integrations.memebench.patha.official_adapter import load_official_class
from integrations.memebench.patha.cli import StagePlan
from integrations.memebench.patha.judge_runner import JudgePlan
from integrations.memebench.patha.reporting import ScorePlan
from contexthub.knowledge_config import KnowledgeConfig

root = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1]).resolve()
s1 = (
    root.parent / "ContextHub-research-plan/research/proposal/experiments/pathA-rq1-rq2"
)


def read(name):
    return json.loads((out / name).read_text())


def write(name, value):
    path = out / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


p = root.parent / "public/MEME-public/code/eval/judge.py"
source = FileIdentity(path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
cls = load_official_class(source)
write(
    "official-judge-prompts.json",
    dict(
        source=source.model_dump(mode="json"),
        prompts={
            k: dict(
                text=getattr(cls, k),
                sha256=hashlib.sha256(getattr(cls, k).encode()).hexdigest(),
            )
            for k in ("BEFORE_COMMON_PROMPT", "CASCADE_D_PROMPT", "CASCADE_U_PROMPT")
        },
        wrapper_hash_is_not_judge_prompt_hash=True,
    ),
)
a = read("algorithm.draft.json")
t = read("timeouts.draft.json")
for strategy in ("selective", "full-history"):
    config = dict(
        id="patha-prep-v1-" + strategy,
        code_version=None,
        extraction=read("models/extraction.json"),
        extraction_price=None,
        discovery=dict(
            search={
                k: a["search"][k]
                for k in (
                    "initial_sources",
                    "expansion_factor",
                    "page_size",
                    "ordering",
                )
            },
            adjudication=dict(
                id="patha-prep-v1-adjudication",
                weak=read("models/p1_weak.json"),
                strong=read("models/p1_strong.json"),
                stage0=a["stage0"],
                plans=a["p1"]["plans"],
                proxy_version="optional-review-preference-0-1-v1",
                cost_formula_version="fixed-262144-input-upper-bound-v1",
                lambda_p1=0,
                weak_price=None,
                strong_price=None,
            ),
            timeout_seconds=t["discovery_timeout_seconds"],
        ),
        maintenance=dict(
            id="patha-prep-v1-maintenance",
            page_size=64,
            chunk_size=8192,
            content_level="l2_content",
            timeout_seconds=t["maintenance_target_timeout_seconds"],
            retry=t["retry"],
            retry_read_codes=["connection", "timeout", "serialization"],
        ),
        profile=dict(
            profile="adaptive",
            planner=dict(
                lambda_p2="0",
                lambda_unit="targets/currency",
                currency=None,
                u_max_algorithm="single-target-count-v1",
                estimates=[
                    dict(
                        action=action,
                        amount=None,
                        currency=None,
                        algorithm_version="fixed-262144-input-upper-bound-v1",
                        price_sha256=None,
                    )
                    for action in ["M2", "M3"]
                ],
            ),
            action_timeout_seconds=t["action_timeout_seconds"],
            rule_timeout_seconds=t["rule_timeout_seconds"],
        ),
        rules=[],
        execution_owner="manual",
        ingest_timeout_seconds=t["ingest_timeout_seconds"],
    )
    config["discovery"]["search"].update(
        id="patha-prep-v1-" + strategy, strategy=strategy, lambda_p1=0
    )
    for role in ("base", "strong", "verifier", "change_location"):
        config[role] = read("models/" + role + ".json")
    write("knowledge/" + strategy + ".incomplete.json", config)
for cls in (StagePlan, JudgePlan, ScorePlan, RunManifest, KnowledgeConfig):
    write("schemas/" + cls.__name__ + ".json", cls.model_json_schema())
selected = {r["episode_id"] for r in read("episodes.json")["episodes"]}
counts = {e: dict(records=0, Cas=0, Abs=0, questions=0) for e in selected}
for line in (s1 / "labels/scoreable-records-v2.jsonl").read_text().split("\n"):
    if not line:
        continue
    row = json.loads(line)
    if row["episode_id"] in selected:
        c = counts[row["episode_id"]]
        c["records"] += 1
        c[row["task_type"]] += 1
for line in (s1 / "answers/answer-bundles-v1.jsonl").read_text().split("\n"):
    if not line:
        continue
    row = json.loads(line)
    if row["episode_id"] in selected:
        counts[row["episode_id"]]["questions"] += len(row["questions"])
write(
    "scope-counts.json",
    dict(
        episodes=counts,
        path_review_minimum=sum(c["records"] for c in counts.values()) * 2,
        case_references=sum(c["records"] for c in counts.values()),
        p2_reviews=sum(c["records"] for c in counts.values()) * 3,
        answer_slots=sum(c["questions"] for c in counts.values()) * 3,
        judge_slots=sum(c["records"] for c in counts.values()) * 6,
        counting_only_no_quality_inspection=True,
    ),
)
print(json.dumps(counts))
