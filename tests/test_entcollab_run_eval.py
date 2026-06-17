from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import Verdict
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.guardrails.tool_state import ToolStateGuardrail
from contexthub.models.request import RequestContext
from integrations.entcollabbench.metrics import (
    InstanceResult,
    aggregate_main_table,
    compute_instance_metrics,
    cost_summary,
    h2_deltas,
    is_false_block,
    repair_success_counts,
    violation_precision_recall,
)
from integrations.entcollabbench.run_eval import EvalConfig, run_eval
from integrations.entcollabbench.systems import GenericGuardrail, build_system
from integrations.entcollabbench.world_loader import LoadedWorld


def test_build_system_conditions_and_s2_ablations():
    loaded = LoadedWorld(
        loaded_uris={"ctx://entcollab/object/hr_case/57"},
        role_to_owner_space={
            "hr_service_specialist": "human_resources",
            "it_service_desk_l1": "it",
        },
    )

    s0 = build_system("S0", repo=None, account_id="acme", loaded=loaded)
    s1 = build_system("S1", repo=None, account_id="acme", loaded=loaded)
    s2 = build_system("S2", repo=None, account_id="acme", loaded=loaded)
    s2a = build_system("S2a", repo=None, account_id="acme", loaded=loaded)
    s2b = build_system("S2b", repo=None, account_id="acme", loaded=loaded)

    assert s0.guardrails == []
    assert isinstance(s1.guardrails[0], GenericGuardrail)
    assert {type(g) for g in s2.guardrails} == {
        HandoffGuardrail,
        ClosureGuardrail,
        ToolStateGuardrail,
    }
    assert [type(g) for g in s2a.guardrails] == [HandoffGuardrail]
    assert [type(g) for g in s2b.guardrails] == [ClosureGuardrail]
    assert s2.interceptor is not None


def test_s1_does_not_inject_contexthub_state_or_guardrails():
    s1 = build_system(
        "S1",
        repo=object(),
        account_id="acme",
        loaded=LoadedWorld(loaded_uris={"ctx://entcollab/object/secret"}),
        acl=object(),
        audit=object(),
    )

    guardrail = s1.guardrails[0]
    assert isinstance(guardrail, GenericGuardrail)
    assert not isinstance(guardrail, (HandoffGuardrail, ClosureGuardrail, ToolStateGuardrail))
    assert not hasattr(guardrail, "_acl")
    assert not hasattr(guardrail, "_staleness")
    assert not hasattr(guardrail, "_loaded")
    assert not hasattr(guardrail, "_provenance_ok")


@pytest.mark.asyncio
async def test_s1_fixed_generic_behavior():
    guardrail = GenericGuardrail()

    missing_intent = await guardrail.check(
        None,
        _ec(
            Boundary.HANDOFF,
            {
                "sender": "a",
                "recipient": "b",
                "expected_action": "continue",
            },
        ),
    )
    assert missing_intent.verdict == Verdict.REPAIR

    enum_bad = await guardrail.check(
        None,
        _ec(
            Boundary.TOOL_CALL,
            {
                "tool_name": "update_ticket",
                "allowed_tools": ["update_ticket"],
                "tool_schema": {
                    "required": ["status"],
                    "properties": {"status": {"enum": ["open", "closed"]}},
                },
                "tool_args": {"status": "done"},
            },
        ),
    )
    assert enum_bad.verdict == Verdict.REPAIR

    approval_missing_citation = await guardrail.check(
        None,
        _ec(
            Boundary.CLOSURE,
            {
                "final_output": "DECISION: approve",
                "decision_label": "approve",
                "allowed_decision_labels": ["approve", "deny"],
            },
        ),
    )
    assert approval_missing_citation.verdict == Verdict.ALLOW

    illegal_tool = await guardrail.check(
        None,
        _ec(
            Boundary.TOOL_CALL,
            {
                "tool_name": "delete_everything",
                "allowed_tools": ["update_ticket"],
                "tool_args": {},
            },
        ),
    )
    assert illegal_tool.verdict == Verdict.BLOCK


def test_violation_precision_recall_all_correct_all_wrong_and_mixed():
    all_correct = violation_precision_recall(
        [
            {"guardrail_verdict": "block", "oracle_violation": True},
            {"guardrail_verdict": "allow", "oracle_violation": False},
        ]
    )
    assert all_correct["precision"] == 1.0
    assert all_correct["recall"] == 1.0

    all_wrong = violation_precision_recall(
        [
            {"guardrail_verdict": "allow", "oracle_violation": True},
            {"guardrail_verdict": "block", "oracle_violation": False},
        ]
    )
    assert all_wrong["precision"] == 0.0
    assert all_wrong["recall"] == 0.0

    mixed = violation_precision_recall(
        [
            {"guardrail_verdict": "repair", "oracle_violation": True},
            {"guardrail_verdict": "block", "oracle_violation": False},
            {"guardrail_verdict": "allow", "oracle_violation": True},
        ]
    )
    assert mixed["precision"] == 0.5
    assert mixed["recall"] == 0.5


def test_false_block_counts_only_when_s0_would_pass():
    s0_pass = InstanceResult("i1", "weak", "S0", task_success=True)
    s2_fail_block = InstanceResult(
        "i1",
        "weak",
        "S2",
        task_success=False,
        guardrail_events=[{"guardrail_verdict": "block"}],
    )
    s0_fail = InstanceResult("i2", "weak", "S0", task_success=False)

    assert is_false_block(s2_fail_block, s0_oracle=s0_pass) is True
    assert is_false_block(s2_fail_block, s0_oracle=s0_fail) is False


def test_repair_success_requires_one_shot_legal_and_task_success():
    events = [
        {
            "guardrail_verdict": "repair",
            "repair_legal_after_one_shot": True,
        },
        {
            "guardrail_verdict": "repair",
            "repair_legal_after_one_shot": False,
        },
    ]

    counts = repair_success_counts(events, task_success=True)

    assert counts == {"attempts": 2.0, "successes": 1.0}


def test_cost_accounting_includes_guardrail_llm_excludes_contract_authoring():
    result = InstanceResult(
        "i1",
        "weak",
        "S2",
        trace=[{"boundary": "tool_call"}, {"boundary": "handoff"}],
        costs={
            "total_tokens": 100,
            "guardrail_llm_tokens": 7,
            "contract_authoring_tokens": 999,
        },
        latency_overheads_ms=[10, 20],
    )

    costs = cost_summary(result)

    assert costs["total_tokens"] == 107.0
    assert costs["contract_authoring_tokens"] == 0.0
    assert costs["tool_calls"] == 1.0
    assert costs["delegations"] == 1.0
    assert costs["per_boundary_latency_overhead_ms"] == 15.0


def test_main_table_aggregation_and_h2_deltas():
    results = [
        InstanceResult("i1", "weak", "S0", seed=0, task_success=False, workflow_closure=False),
        InstanceResult("i2", "weak", "S0", seed=1, task_success=True, workflow_closure=True),
        InstanceResult("i1", "weak", "S2", seed=0, task_success=True, workflow_closure=True),
        InstanceResult("i2", "weak", "S2", seed=1, task_success=True, workflow_closure=True),
        InstanceResult("i3", "strong", "S0", seed=0, task_success=True, workflow_closure=True),
        InstanceResult("i3", "strong", "S2", seed=0, task_success=True, workflow_closure=True),
    ]

    table = aggregate_main_table(results)
    deltas = h2_deltas(table)

    assert table["S0"]["weak"]["task_success"]["mean"] == 0.5
    assert table["S2"]["weak"]["task_success"]["mean"] == 1.0
    assert deltas["weak"] == 0.5
    assert deltas["strong"] == 0.0


def test_compute_instance_metrics_false_block_and_failure_modes():
    s0 = InstanceResult("i1", "weak", "S0", task_success=True)
    s2 = InstanceResult(
        "i1",
        "weak",
        "S2",
        task_success=False,
        trace=[{"boundary": "tool_call"}],
        grader={"failure_modes": ["wrong_object"]},
        guardrail_events=[
            {"guardrail_verdict": "block", "oracle_violation": False},
        ],
    )

    metrics = compute_instance_metrics(s2, s0_oracle=s0)

    assert metrics["false_block"] == 1.0
    assert metrics["wrong_object_rate"] == 1.0
    assert metrics["blocked_actions"] == 1.0


@pytest.mark.asyncio
async def test_run_eval_dry_run_writes_results(tmp_path):
    out = tmp_path / "results.jsonl"
    config = EvalConfig(
        models={"weak": "mock-weak", "strong": "mock-strong"},
        systems=("S0", "S1", "S2"),
        subsets=("workflow",),
        instances=1,
        seeds=1,
        out=out,
        dry_run=True,
    )

    summary = await run_eval(config)

    assert out.exists()
    assert Path(summary["summary_path"]).exists()
    assert Path(summary["h2_path"]).exists()
    assert len(summary["results"]) == 6
    assert set(summary["main_table"]) == {"S0", "S1", "S2"}


def _ec(boundary: Boundary, payload: dict) -> EnforcementContext:
    return EnforcementContext(
        boundary=boundary,
        actor=RequestContext("acme", "agent-a"),
        payload=payload,
    )
