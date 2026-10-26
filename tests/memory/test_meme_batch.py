import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from integrations.memebench.memory_research import batch


def test_fixed_protocol_matches_cli(tmp_path):
    args = SimpleNamespace(data=Path("data.json"), official_judge=Path("judge.py"), output=tmp_path)
    parsed = batch.argument_parser().parse_args(batch.command("pl_001", args)[3:])
    assert parsed.memory_model == "deepseek-flash"
    assert parsed.memory_provider == "deepseek"
    assert parsed.embedding_model == "qwen3.7-text-embedding"
    assert parsed.embedding_dimensions == 1536
    assert parsed.p1_backend == parsed.p2_direct_backend == parsed.candidate_gate_backend == "jev"
    assert parsed.p1_jev_threshold == 0.5
    assert parsed.topic_similarity_threshold == 0.8


def test_incomplete_or_answer_failure_is_not_resumable_success(tmp_path):
    assert not batch.successful(tmp_path, "pl_001")
    summary = {"completed": 1, "unjudged": 0}
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    result = {"before_answers": [{"error": None}], "after_answers": [{"error": {"type": "Timeout"}}]}
    scored = tmp_path / "pl_001.scored.json"
    scored.write_text(json.dumps(result))
    assert not batch.successful(tmp_path, "pl_001")
    result["after_answers"][0]["error"] = None
    scored.write_text(json.dumps(result))
    assert batch.successful(tmp_path, "pl_001")
    summary["unjudged"] = 1
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    assert not batch.successful(tmp_path, "pl_001")


def test_partial_summary_never_claims_full_score(tmp_path):
    folder = tmp_path / "pl_001" / "run"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(json.dumps({"tasks": {t: {"passed": 1, "total": 2} for t in batch.TASKS}}))
    result = batch.aggregate(tmp_path, ["pl_001", "sw_001"], {"pl_001": "pl_001/run"})
    assert result["complete"] is False
    assert result["missing_cases"] == ["sw_001"]
    assert result["passed"] == 6 and result["total_scored"] == 12


def test_resume_retains_success_and_requires_explicit_failed_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "ROOT", tmp_path)
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql://fixture/unused")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture")
    (tmp_path / "model_providers.local.json").write_text(json.dumps({"targets": [
        {"label": label, "base_url": "https://fixture.invalid", "api_key": "fixture-secret"}
        for label in ("deepseek", "aliyun", "openlux")
    ]}))
    monkeypatch.setattr(batch, "preflight", lambda args: {"commit": "fixed", "cases": ["pl_001", "sw_001"]})
    calls = []

    def run(command, **kwargs):
        case = command[command.index("--episode") + 1]
        calls.append(case)
        folder = Path(command[command.index("--output") + 1]) / f"memory-check-{len(calls)}"
        folder.mkdir(parents=True)
        if calls == ["pl_001", "sw_001"]:
            return SimpleNamespace(returncode=1)
        (folder / "summary.json").write_text(json.dumps({
            "completed": 1, "unjudged": 0,
            "tasks": {t: {"passed": 1, "total": 1} for t in batch.TASKS},
        }))
        (folder / (case + ".scored.json")).write_text(json.dumps({"before_answers": [], "after_answers": []}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(batch.subprocess, "run", run)
    argv = ["batch", "--data", "unused", "--official-judge", "unused", "--output", str(tmp_path / "batch")]
    monkeypatch.setattr(batch.sys, "argv", argv)
    with pytest.raises(SystemExit):
        batch.main()
    monkeypatch.setattr(batch.sys, "argv", argv + ["--resume"])
    with pytest.raises(SystemExit):
        batch.main()
    assert calls == ["pl_001", "sw_001"]
    monkeypatch.setattr(batch.sys, "argv", argv + ["--resume", "--retry-failed"])
    batch.main()
    assert calls == ["pl_001", "sw_001", "sw_001"]
    assert batch.read(tmp_path / "batch/summary.json")["complete"]
    assert "fixture-secret" not in (tmp_path / "batch/batch.json").read_text()
