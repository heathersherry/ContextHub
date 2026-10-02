"""Create a new preparation receipt after all edits and offline checks."""

import ast
import re
import offline_guard  # noqa: F401
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
from integrations.memebench.patha.run_gates import FileIdentity
from integrations.memebench.patha.reporting import dependency_identity

root = Path(__file__).resolve().parents[2]
out = root / "docs/patha-preparation-2026-09-25"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(name, value):
    with (out / name).open("x") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")


rounds = [
    "patha-prep-check-01",
    "patha-prep-check-02",
    "patha-prep-check-03",
    "patha-prep-regression-01",
    "patha-prep-additional-01",
    "patha-prep-additional-02",
    "patha-prep-final-regression",
]
for name in rounds:
    src = Path("/private/tmp") / name
    dest = out / "evidence" / name
    dest.mkdir(parents=True, exist_ok=True)
    for file in ["command.json", "pytest.log", "results.xml", "result.json"]:
        if (dest / file).exists():
            assert sha(src / file) == sha(dest / file)
        else:
            shutil.copy2(src / file, dest / file)
xml = ET.parse(out / "evidence/patha-prep-final-regression/results.xml")
suites = list(xml.getroot().iter("testsuite"))
counts = {
    k: sum(int(s.attrib.get(k, 0)) for s in suites)
    for k in ["tests", "failures", "errors", "skipped"]
}
assert counts == dict(tests=312, failures=0, errors=0, skipped=0), counts
tracked = subprocess.check_output(
    ["git", "diff", "--name-only", "HEAD"], cwd=root, text=True
).splitlines()
new = [
    "src/contexthub/llm/request_parameters.py",
    "integrations/memebench/patha/answer_prompt.py",
    "integrations/memebench/patha/episode_deadline.py",
    "tests/test_patha_preparation.py",
]
# Limit formatting/lint to changed/new implementation and preparation files.
# chat_client/adjudication have historical formatting; still check their syntax.
py = [p for p in tracked + new if p.endswith(".py")]
for p in py:
    ast.parse((root / p).read_text())
cmd = [
    "/opt/anaconda3/bin/ruff",
    "check",
    *[
        p
        for p in py
        if p
        not in [
            "src/contexthub/llm/chat_client.py",
            "src/contexthub/discovery/adjudication.py",
        ]
    ],
    "scripts/patha_preparation",
]
result = subprocess.run(cmd, cwd=root, text=True, capture_output=True)
assert result.returncode == 0, result.stdout + result.stderr
check = subprocess.run(
    ["git", "diff", "--check"], cwd=root, text=True, capture_output=True
)
assert check.returncode == 0, check.stdout + check.stderr
save(
    "verification.json",
    dict(
        final_counts=counts,
        rounds=rounds,
        failed_round="additional-01: guard rejected explicitly disabled env_file=None; corrected guard and rechecked without changing test assertion",
        static=dict(
            argv=cmd,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            syntax_files=py,
            diff_check_exit_code=check.returncode,
        ),
        database_checks=False,
        network_requests=False,
        model_calls=False,
    ),
)
baseline = json.loads((out / "baseline.json").read_text())
s1 = (
    root.parent / "ContextHub-research-plan/research/proposal/experiments/pathA-rq1-rq2"
)
s1_now = {p: sha(p) for p in baseline["s1_code"]}
assert s1_now == baseline["s1_code"]
close = (
    root.parent
    / "ContextHub-research-plan/research/proposal/implementation-handoffs/S1-closeout.md"
).read_text()
s1_artifacts = {
    p: sha(s1 / p) == h
    for p, h in re.findall(
        r"\| `((?:answers|inputs|labels|manifests)/[^`]+)` \| `([a-f0-9]{64})`", close
    )
}
assert len(s1_artifacts) == 11 and all(s1_artifacts.values())
assert (
    sha(root.parent / "public/MEME/meme_filler32k.json")
    == baseline["dataset"]["sha256"]
)
repos = {}
for name, p in [
    ("code", root),
    ("research", root.parent / "ContextHub-research-plan"),
    ("official", root.parent / "public/MEME-public"),
]:
    repos[name] = {
        k: subprocess.check_output(["git", "-C", str(p), *args], text=True)
        for k, args in {
            "head": ["rev-parse", "HEAD"],
            "branch": ["branch", "--show-current"],
            "status": ["status", "--short"],
        }.items()
    }
    assert repos[name]["head"] == baseline["repos"][name]["head"]
    assert repos[name]["branch"] == baseline["repos"][name]["branch"]
    if name != "code":
        assert repos[name]["status"] == baseline["repos"][name]["status"]
save(
    "end-baseline-check.json",
    dict(s1_code=s1_now, s1_artifacts=s1_artifacts, repos=repos, dataset_matches=True),
)
parent = s1 / "manifests/scorer-contract-v2.json"
identity = dependency_identity(
    FileIdentity(path=str(parent), sha256=sha(parent)), output=out / "scorer-identity"
)
save(
    "DELIVERY.json",
    dict(
        version="patha-first-run-preparation-v1",
        code_commit=identity["code_commit"],
        dirty_diff_sha256=identity["dirty_diff_sha256"],
        code_dependency_count=len(identity["code_files"]),
        scorer_identity_sha256=sha(out / "scorer-identity/identity.json"),
        tracked_changes=tracked,
        new_code_and_tests=new,
        preparation_scripts=[
            str(p.relative_to(root))
            for p in (root / "scripts/patha_preparation").glob("*.py")
        ],
        tests=counts,
        real_execution_authorized=False,
        independent_review_passed=False,
        identity_rule="Hash manifest excludes itself only; tracked patch excludes untracked files, whose exact bytes are separately listed. Any edit or commit requires new identity.",
        remaining_conditions=json.loads(
            (out / "draft-v1/draft-status.json").read_text()
        )["blocking"],
    ),
)
files = {root / p for p in tracked + new}
files.update((root / "scripts/patha_preparation").glob("*.py"))
files.update(p for p in out.rglob("*") if p.is_file())
manifest = {str(p.relative_to(root)): sha(p) for p in sorted(files)}
save("DELIVERY-SHA256SUMS.json", manifest)
print(
    json.dumps(
        dict(
            delivery_sha256=sha(out / "DELIVERY.json"),
            file_manifest_sha256=sha(out / "DELIVERY-SHA256SUMS.json"),
            files=len(manifest),
            tests=counts,
            scorer_code_dependencies=len(identity["code_files"]),
        )
    )
)
