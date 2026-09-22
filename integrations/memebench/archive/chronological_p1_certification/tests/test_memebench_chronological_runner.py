from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from integrations.memebench.run_chronological_p1p2 import (
    bind_run_config,
    build_case_record,
    build_parser,
    case_key,
    chronological_account,
    cmd_summarize,
    drain_case_events,
    filter_rows_to_episode_ids,
    frozen_artifact_content_hash,
    git_identity,
    graph_miss_summary,
    hop_dir,
    load_frozen_plan,
    load_frozen_p2_contract,
    load_p2_contract,
    maybe_merge_retry_tokens,
    merge_tokens,
    planned_all_direct_stale,
    run_config_payload,
    scored_records,
    sha256_file,
    write_json,
)


class _Case:
    def __init__(self, episode_id: str, target_entity: str) -> None:
        self.episode_id = episode_id
        self.target_entity = target_entity


def test_filter_rows_drops_evaluation_episodes() -> None:
    rows = [
        {"episode_id": "sel-1", "edge": 1},
        {"episode_id": "eval-9", "edge": 2},
        {"episode_id": "sel-2", "edge": 3},
    ]
    kept = filter_rows_to_episode_ids(rows, ["sel-1", "sel-2"])
    assert [row["episode_id"] for row in kept] == ["sel-1", "sel-2"]


def test_load_p2_contract_never_calibrates_on_eval(monkeypatch) -> None:
    rows = [
        {"episode_id": "sel-1", "should_stale": True},
        {"episode_id": "eval-1", "should_stale": True},
        {"episode_id": "sel-1", "should_stale": False},
    ]
    captured: dict[str, list[str]] = {}

    def fake_joined(edges, verdicts):
        return rows

    def fake_calibrate(cal_rows, method=None, alpha=None, recompute_cost=None):
        captured["ids"] = [str(row["episode_id"]) for row in cal_rows]
        return {"__global__": {"J1": {"expected_cost": 0, "delta": 0.1}}}, {"n": len(cal_rows)}

    monkeypatch.setattr(
        "integrations.memebench.run_chronological_p1p2.load_joined",
        fake_joined,
    )
    monkeypatch.setattr(
        "integrations.memebench.run_chronological_p1p2.calibrate_contracts",
        fake_calibrate,
    )
    doc = load_p2_contract(
        hop=1,
        method="cp-upper",
        selection_ids=["sel-1"],
        evaluation_ids=["eval-1"],
        split_hash="hash",
    )
    assert captured["ids"] == ["sel-1", "sel-1"]
    assert doc["evaluation_episodes_in_calibration"] is False
    assert doc["held_out_certification"] is False
    assert doc["contract_distribution_mismatch"] is True
    assert doc["n_excluded_evaluation_rows"] == 1
    assert "eval-1" not in doc["calibration_episode_ids"]


def test_e2e_refuses_to_recalibrate() -> None:
    source = inspect.getsource(
        __import__(
            "integrations.memebench.run_chronological_p1p2",
            fromlist=["cmd_run_e2e"],
        ).cmd_run_e2e
    )
    assert "load_frozen_p2_contract" in source
    assert "load_p2_contract(hop" not in source
    case_src = inspect.getsource(
        __import__(
            "integrations.memebench.run_chronological_p1p2",
            fromlist=["run_one_group_case"],
        ).run_one_group_case
    )
    assert "answer_question" in case_src
    assert "judge_case_async" in case_src
    assert "context_id=None" not in case_src
    assert "judge_chat" in case_src
    drain_src = inspect.getsource(drain_case_events)
    assert "pending_event_context_ids" in drain_src
    assert "unfinished_propagation_events" in drain_src
    assert "range(32)" in drain_src
    e2e_src = inspect.getsource(
        __import__(
            "integrations.memebench.run_chronological_p1p2",
            fromlist=["cmd_run_e2e"],
        ).cmd_run_e2e
    )
    assert "judge_model" in e2e_src or "judge-model" in inspect.getsource(
        __import__(
            "integrations.memebench.run_chronological_p1p2",
            fromlist=["build_parser"],
        ).build_parser
    )


def test_load_frozen_p2_contract_missing_and_leaked(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="will not recalibrate"):
        load_frozen_p2_contract(tmp_path, "cp-upper")
    leaked = tmp_path / "p2_contract_cp-upper.json"
    leaked.write_text(
        '{"evaluation_episodes_in_calibration": true, "contracts": {}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="evaluation episodes"):
        load_frozen_p2_contract(tmp_path, "cp-upper")
    leaked.write_text(
        json.dumps(
            {
                "evaluation_episodes_in_calibration": False,
                "method": "point",
                "split_hash": "old",
                "calibration_episode_ids": ["eval-1", "sel-1"],
                "contracts": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match"):
        load_frozen_p2_contract(tmp_path, "cp-upper")
    leaked.write_text(
        json.dumps(
            {
                "evaluation_episodes_in_calibration": False,
                "method": "cp-upper",
                "split_hash": "old",
                "calibration_episode_ids": ["eval-1", "sel-1"],
                "contracts": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="split_hash"):
        load_frozen_p2_contract(tmp_path, "cp-upper", split_hash="new")
    with pytest.raises(ValueError, match="evaluation episodes"):
        load_frozen_p2_contract(
            tmp_path,
            "cp-upper",
            split_hash="old",
            selection_ids=["sel-1"],
            evaluation_ids=["eval-1"],
        )


def test_case_key_includes_hop_and_target() -> None:
    key1 = case_key(
        {
            "episode_id": "ep",
            "hop": 1,
            "group": "G0",
            "target_entity": "Alice",
            "p1_policy": "T_current_tau",
            "consolidation_mode": "sync-inline",
        }
    )
    key2 = case_key(
        {
            "episode_id": "ep",
            "hop": 2,
            "group": "G0",
            "target_entity": "Alice",
            "p1_policy": "T_current_tau",
            "consolidation_mode": "sync-inline",
        }
    )
    key3 = case_key(
        {
            "episode_id": "ep",
            "hop": 1,
            "group": "G0",
            "target_entity": "Bob",
            "p1_policy": "T_current_tau",
            "consolidation_mode": "sync-inline",
        }
    )
    assert key1 != key2
    assert key1 != key3
    assert "1" in key1 and "2" in key2


def test_accounts_differ_by_group() -> None:
    case = _Case("ep-1", "Alice")
    g0 = chronological_account(
        case, group="G0", hop=1, policy="T_current_tau", schedule="sync-inline"
    )
    g1 = chronological_account(
        case, group="G1", hop=1, policy="T_current_tau", schedule="async-each-session"
    )
    hop2 = chronological_account(
        case, group="G0", hop=2, policy="T_current_tau", schedule="sync-inline"
    )
    assert g0 != g1
    assert g0 != hop2


def test_error_record_does_not_count_as_graph_hit() -> None:
    failed = build_case_record(
        episode_id="ep",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        errors=["Timeout"],
    )
    ok_miss = build_case_record(
        episode_id="ep2",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        p1={"n_gold": 2, "n_pred": 0, "n_tp": 0, "scored": True},
    )
    ok_hit = build_case_record(
        episode_id="ep3",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        p1={"n_gold": 1, "n_pred": 1, "n_tp": 1, "scored": True},
    )
    assert failed["p1"]["graph_miss"] is None
    assert failed["p1"]["scored"] is False
    rows = [failed, ok_miss, ok_hit]
    scored = scored_records(rows)
    assert [row["episode_id"] for row in scored] == ["ep2", "ep3"]
    stats = graph_miss_summary(rows)
    assert stats["n"] == 2
    assert stats["n_failed"] == 1
    assert stats["graph_miss_rate"] == 0.5
    assert stats["event"] == "episode"
    assert stats["p1_go"] is False
    assert stats["evaluation_complete"] is False


def test_graph_miss_summary_is_episode_level_not_case_level() -> None:
    hit = build_case_record(
        episode_id="ep-shared",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        target_entity="Alice",
        p1={"n_gold": 1, "n_pred": 1, "n_tp": 1, "scored": True},
    )
    miss = build_case_record(
        episode_id="ep-shared",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        target_entity="Bob",
        p1={"n_gold": 1, "n_pred": 1, "n_tp": 0, "scored": True},
    )
    stats = graph_miss_summary([hit, miss])
    assert stats["n"] == 1
    assert stats["n_cases"] == 2
    assert stats["graph_miss_rate"] == 1.0
    assert stats["n_gold_edges"] == 2
    assert stats["n_missed_edges"] == 1
    assert stats["edge_miss_rate"] == 0.5
    assert planned_all_direct_stale([hit, miss]) is None


def test_edge_miss_rate_separates_severity_when_episode_miss_ties() -> None:
    light = [
        build_case_record(
            episode_id=f"light-{i}",
            hop=1,
            group="p1-selection",
            p1_policy="T_current_tau",
            consolidation_mode="async-each-session",
            p1={"n_gold": 100, "n_pred": 99, "n_tp": 99, "scored": True},
        )
        for i in range(2)
    ]
    heavy = [
        build_case_record(
            episode_id=f"heavy-{i}",
            hop=1,
            group="p1-selection",
            p1_policy="E_economy",
            consolidation_mode="async-each-session",
            p1={"n_gold": 100, "n_pred": 20, "n_tp": 20, "scored": True},
        )
        for i in range(2)
    ]
    light_stats = graph_miss_summary(light)
    heavy_stats = graph_miss_summary(heavy)
    assert light_stats["graph_miss_rate"] == 1.0
    assert heavy_stats["graph_miss_rate"] == 1.0
    assert light_stats["edge_miss_rate"] == 0.01
    assert heavy_stats["edge_miss_rate"] == 0.80
    assert light_stats["n_missed_edges"] == 2
    assert heavy_stats["n_missed_edges"] == 160


def test_retry_merges_failed_tokens() -> None:
    previous = {
        "errors": ["boom"],
        "tokens": {
            "extract_llm": {
                "model": "gpt-4.1-mini",
                "calls": 2,
                "prompt_tokens": 10,
                "completion_tokens": 4,
            }
        },
    }
    current = build_case_record(
        episode_id="ep",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        p1={"n_gold": 1, "n_pred": 1, "n_tp": 1, "scored": True},
        tokens={
            "extract_llm": {
                "model": "gpt-4.1-mini",
                "calls": 1,
                "prompt_tokens": 3,
                "completion_tokens": 1,
            }
        },
    )
    merged = maybe_merge_retry_tokens(previous, current)
    bucket = merged["tokens"]["extract_llm"]
    assert bucket["calls"] == 3
    assert bucket["prompt_tokens"] == 13
    assert merge_tokens({}, {"x": {"calls": 1, "prompt_tokens": 2, "completion_tokens": 0}})[
        "x"
    ]["calls"] == 1


def test_summarize_excludes_failures_and_uses_hop_dir(tmp_path) -> None:
    out = hop_dir(tmp_path, 1)
    write_json(
        out / "split.json",
        {"split": {"certification_ids": ["ep-ok"], "split_hash": "abc"}},
    )
    failed = build_case_record(
        episode_id="ep-fail",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        errors=["boom"],
    )
    ok = build_case_record(
        episode_id="ep-ok",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        p1={"n_gold": 1, "n_pred": 0, "n_tp": 0, "scored": True},
        outcome={"false_fresh": True, "on_trivial_pass": False},
    )
    write_json(out / "e2e" / "cp-upper" / "G0" / "cases.json", {"group": "G0", "cases": [failed, ok]})
    args = SimpleNamespace(out=tmp_path, hop=1)
    assert cmd_summarize(args) == 0
    summary = (out / "summary.json").read_text(encoding="utf-8")
    assert '"n": 1' in summary
    assert '"n_failed": 1' in summary
    assert '"graph_miss_rate": 1.0' in summary
    assert '"p1_go": false' in summary
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["summarize", "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        parser.parse_args(["select-p1", "--out", str(tmp_path)])


def test_p1_go_requires_complete_successful_evaluation() -> None:
    hits = [
        build_case_record(
            episode_id=f"ep-{i:03d}",
            hop=1,
            group="G0",
            p1_policy="T_current_tau",
            consolidation_mode="sync-inline",
            p1={"n_gold": 1, "n_pred": 1, "n_tp": 1, "scored": True},
        )
        for i in range(80)
    ]
    clean = graph_miss_summary(hits, expected_episode_ids=[f"ep-{i:03d}" for i in range(80)])
    assert clean["n_failed"] == 0
    assert clean["p1_go"] is True
    failed = build_case_record(
        episode_id="ep-fail",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        errors=["oracle timeout"],
    )
    with_fail = graph_miss_summary(hits + [failed])
    assert with_fail["U_graph"] <= clean["U_graph"] or with_fail["n"] == clean["n"]
    assert with_fail["p1_go"] is False
    missing = graph_miss_summary(
        hits, expected_episode_ids=[f"ep-{i:03d}" for i in range(80)] + ["ep-missing"]
    )
    assert missing["n_missing_episodes"] == 1
    assert missing["p1_go"] is False


def test_bind_run_config_rejects_model_or_split_change(tmp_path) -> None:
    dest = tmp_path / "e2e" / "G0"
    payload = {"models": {"chat_model": "gpt-4.1-mini"}, "split_hash": "aaa"}
    digest = bind_run_config(dest, payload)
    assert digest == bind_run_config(dest, payload)
    with pytest.raises(ValueError, match="new --out"):
        bind_run_config(dest, {"models": {"chat_model": "other"}, "split_hash": "aaa"})
    other = tmp_path / "legacy"
    other.mkdir()
    (other / "checkpoint.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unbound checkpoint"):
        bind_run_config(other, payload)


def _model_args(**extra) -> SimpleNamespace:
    payload = {
        "chat_model": "gpt-4.1-mini",
        "extract_model": "gpt-4.1-mini",
        "p1_cheap_model": "gpt-4o-mini",
        "p1_strong_model": "gpt-4.1-mini",
        "p2_cheap_model": "gpt-4o-mini",
        "p2_strong_model": "gpt-4.1-mini",
        "embedding_model": "text-embedding-v4",
        "embedding_provider": "aliyun",
        "provider": "openlux",
        "data": None,
        "hop": 1,
        "judge_model": "gpt-4o",
    }
    payload.update(extra)
    return SimpleNamespace(**payload)


def test_frozen_artifact_hash_is_content_not_path_or_mtime(tmp_path) -> None:
    left = tmp_path / "left" / "frozen_p1_policy.json"
    right = tmp_path / "right" / "frozen_p1_policy.json"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_bytes(b'{"policy":"T_current_tau"}\n')
    right.write_bytes(b'{"policy":"T_current_tau"}\n')
    digest = frozen_artifact_content_hash(left)
    assert digest == frozen_artifact_content_hash(right)
    assert digest == sha256_file(left)
    left.touch()
    right.touch()
    assert frozen_artifact_content_hash(left) == digest
    dumped = json.dumps(
        run_config_payload(
            _model_args(),
            split_hash="split",
            frozen_p1_policy_hash=digest,
        )
    )
    assert str(left) not in dumped
    assert "mtime" not in dumped


def test_bind_run_config_rejects_frozen_p1_content_change(tmp_path) -> None:
    dest = tmp_path / "p1_timing" / "async-each-session"
    frozen = tmp_path / "hop1" / "frozen_p1_policy.json"
    frozen.parent.mkdir()
    frozen.write_text('{"frozen": true, "selected_policy": {"policy_id": "A"}}\n')
    payload = run_config_payload(
        _model_args(),
        split_hash="split",
        frozen_p1_policy_hash=frozen_artifact_content_hash(frozen),
    )
    digest = bind_run_config(dest, payload)
    assert digest == bind_run_config(dest, payload)
    frozen.write_text('{"frozen": true, "selected_policy": {"policy_id": "B"}}\n')
    changed = run_config_payload(
        _model_args(),
        split_hash="split",
        frozen_p1_policy_hash=frozen_artifact_content_hash(frozen),
    )
    with pytest.raises(ValueError, match="frozen-artifact"):
        bind_run_config(dest, changed)


def test_bind_run_config_rejects_p2_contract_content_change(tmp_path) -> None:
    dest = tmp_path / "e2e" / "point" / "G3"
    frozen_p1 = tmp_path / "hop1" / "frozen_p1_policy.json"
    contract = tmp_path / "hop1" / "p2_contract_point.json"
    frozen_p1.parent.mkdir()
    frozen_p1.write_text('{"frozen": true}\n')
    contract.write_text('{"method": "point", "delta": 0.1}\n')
    payload = run_config_payload(
        _model_args(),
        split_hash="split",
        contract_method="point",
        frozen_p1_policy_hash=frozen_artifact_content_hash(frozen_p1),
        p2_contract_hash=frozen_artifact_content_hash(contract),
    )
    digest = bind_run_config(dest, payload)
    assert digest == bind_run_config(dest, payload)
    contract.write_text('{"method": "point", "delta": 0.9}\n')
    changed = run_config_payload(
        _model_args(),
        split_hash="split",
        contract_method="point",
        frozen_p1_policy_hash=frozen_artifact_content_hash(frozen_p1),
        p2_contract_hash=frozen_artifact_content_hash(contract),
    )
    with pytest.raises(ValueError, match="frozen-artifact"):
        bind_run_config(dest, changed)


def test_bind_run_config_resumes_when_frozen_artifacts_unchanged(tmp_path) -> None:
    dest = tmp_path / "e2e" / "cp-upper" / "G3"
    frozen_p1 = tmp_path / "hop1" / "frozen_p1_policy.json"
    contract = tmp_path / "hop1" / "p2_contract_cp-upper.json"
    frozen_p1.parent.mkdir()
    frozen_p1.write_text('{"frozen": true, "selected_policy": {"policy_id": "T"}}\n')
    contract.write_text('{"method": "cp-upper", "held_out_certification": false}\n')
    payload = run_config_payload(
        _model_args(),
        split_hash="split",
        contract_method="cp-upper",
        frozen_p1_policy_hash=frozen_artifact_content_hash(frozen_p1),
        p2_contract_hash=frozen_artifact_content_hash(contract),
    )
    first = bind_run_config(dest, payload)
    frozen_p1.touch()
    contract.touch()
    second = bind_run_config(dest, payload)
    assert first == second
    stored = json.loads((dest / "run_config.json").read_text(encoding="utf-8"))
    assert stored["run_config_hash"] == first
    assert stored["frozen_p1_policy_hash"] == sha256_file(frozen_p1)
    assert stored["p2_contract_hash"] == sha256_file(contract)


def test_timing_and_e2e_bind_frozen_artifact_content_hashes() -> None:
    timing = inspect.getsource(
        __import__(
            "integrations.memebench.run_chronological_p1p2",
            fromlist=["cmd_run_p1_timing"],
        ).cmd_run_p1_timing
    )
    e2e = inspect.getsource(
        __import__(
            "integrations.memebench.run_chronological_p1p2",
            fromlist=["cmd_run_e2e"],
        ).cmd_run_e2e
    )
    assert "frozen_artifact_content_hash" in timing
    assert "frozen_p1_policy.json" in timing
    assert "frozen_p1_policy_hash" in timing
    assert "frozen_artifact_content_hash" in e2e
    assert "frozen_p1_policy_hash" in e2e
    assert "p2_contract_hash" in e2e
    assert 'p2_contract_{args.contract}.json' in e2e or "p2_contract_" in e2e


def test_load_frozen_plan_checks_current_split(tmp_path) -> None:
    from integrations.memebench.chronological_policy import (
        build_plan_to_json,
        registered_build_plans,
    )

    out = hop_dir(tmp_path, 1)
    write_json(
        out / "frozen_p1_policy.json",
        {
            "frozen": True,
            "split_hash": "old-split",
            "selected_policy": {
                "policy_id": "T_current_tau",
                "parameters": build_plan_to_json(registered_build_plans()["T_current_tau"]),
            },
        },
    )
    with pytest.raises(ValueError, match="split_hash"):
        load_frozen_plan(out, split_hash="new-split")
    plan, frozen = load_frozen_plan(out, split_hash="old-split")
    assert frozen is True
    assert plan.name == "T_current_tau"


def test_git_identity_does_not_use_parent_commit_as_code_version(tmp_path) -> None:
    repo = tmp_path / "ContextHub"
    repo.mkdir()
    identity = git_identity(repo)
    assert identity["git_commit"] is None
    assert identity["source_fingerprint"]
    assert identity.get("parent_git_tracks_this_tree") is False


def test_e2e_requires_judge_model() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run-e2e",
                "--hop",
                "1",
                "--contract",
                "cp-upper",
                "--chat-model",
                "gpt-4.1-mini",
                "--extract-model",
                "gpt-4.1-mini",
                "--p1-cheap-model",
                "gpt-4o-mini",
                "--p1-strong-model",
                "gpt-4.1-mini",
                "--p2-cheap-model",
                "gpt-4o-mini",
                "--p2-strong-model",
                "gpt-4.1-mini",
                "--embedding-model",
                "text-embedding-v4",
                "--embedding-provider",
                "aliyun",
                "--provider",
                "openlux",
            ]
        )
