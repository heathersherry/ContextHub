"""Load pinned official scoring methods without importing its CLI or SDK."""

import ast
import hashlib
import json
from pathlib import Path
from contexthub.memory import contracts


def load_official(path):
    source = Path(path).read_text()
    pinned = json.loads(
        (contracts.PROMPTS / "evaluation" / "official_manifest.json").read_text()
    )
    if hashlib.sha256(source.encode()).hexdigest() != pinned["judge_sha256"]:
        raise ValueError("official judge source differs from frozen version")
    tree = ast.parse(source)
    nodes = [
        n
        for n in tree.body
        if isinstance(n, (ast.ClassDef, ast.FunctionDef))
        and n.name in ("LLMJudge", "judge_episode")
    ]
    namespace = {"json": json, "Dict": dict, "List": list}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    cls = namespace["LLMJudge"]
    for name, filename in pinned["prompt_files"].items():
        if (
            getattr(cls, name)
            != (contracts.PROMPTS / "evaluation" / filename).read_text()
        ):
            raise ValueError("official prompt was modified")
    return cls, namespace["judge_episode"]
