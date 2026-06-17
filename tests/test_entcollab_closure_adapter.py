from __future__ import annotations

import pytest

from contexthub.enforcement import Boundary, EnforcementContext, Verdict
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.models.request import RequestContext
from integrations.entcollabbench.closure_adapter import (
    build_approval_closure_payload,
    build_workflow_closure_payload,
    extract_completed_actions,
    required_actions_from_ground_truth,
)


class FakeStaleness:
    async def any_stale_or_blocked_refs(self, db, refs):
        return []


def _tool_call(agent: str, tool_name: str, call_id: str, ts: float = 1.0) -> dict:
    return {
        "agent_name": agent,
        "event": "tool_call",
        "ts": ts,
        "data": {
            "tool_call_id": call_id,
            "tool_name": "mcp_csm_call_tool",
            "arguments": {
                "tool_name": tool_name,
                "arguments_json": '{"id": "1"}',
            },
        },
    }


def _tool_result(
    agent: str,
    call_id: str,
    ts: float = 2.0,
    *,
    status: str = "ok",
    error: str | None = None,
) -> dict:
    data = {
        "tool_call_id": call_id,
        "status": status,
        "result": {"id": "1"},
    }
    if error:
        data["error"] = error
    return {
        "agent_name": agent,
        "event": "tool_result",
        "ts": ts,
        "data": data,
    }


def _ground_truth() -> list[dict]:
    return [
        {
            "mcp_server_name": "csm",
            "tool_name": "update_case",
            "agent": "customer_support_specialist",
            "arguments": {"case_id": "CS-1"},
        },
        {
            "mcp_server_name": "csm",
            "tool_name": "update_knowledge",
            "agent": "knowledge_base_specialist",
            "arguments": {"knowledge_id": "KB-1"},
        },
    ]


def test_required_and_completed_actions_are_adapter_labels() -> None:
    gt = _ground_truth()
    events = [
        _tool_call("customer_support_specialist", "update_case", "call-1", 1.0),
        _tool_result("customer_support_specialist", "call-1", 2.0),
        _tool_call("knowledge_base_specialist", "update_knowledge", "call-2", 3.0),
        _tool_result("knowledge_base_specialist", "call-2", 4.0),
    ]

    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_passed",
        ground_truth=gt,
        trace_events=events,
        runtime_summary={"timeout": False},
    )

    assert required_actions_from_ground_truth(gt) == [
        "customer_support_specialist.update_case",
        "knowledge_base_specialist.update_knowledge",
    ]
    assert payload["completed_actions"] == payload["anchor"]["required_actions"]
    assert payload["diagnostics"]["missing_actions"] == []
    assert payload["open_questions"] == []


def test_failed_tool_result_is_not_completed() -> None:
    events = [
        _tool_call("knowledge_base_specialist", "update_knowledge", "call-1", 1.0),
        _tool_result(
            "knowledge_base_specialist",
            "call-1",
            2.0,
            status="error",
            error="permission denied",
        ),
    ]

    completed, evidence, diagnostics = extract_completed_actions(events)

    assert completed == []
    assert evidence == {}
    assert diagnostics["failed_tool_results"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_missing_update_knowledge_blocks_closure_guardrail() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_145",
        ground_truth=_ground_truth(),
        trace_events=[
            _tool_call("customer_support_specialist", "update_case", "call-1", 1.0),
            _tool_result("customer_support_specialist", "call-1", 2.0),
        ],
        runtime_summary={"timeout": True, "failure_reason": "TimeoutError: timed out"},
    )
    ec = EnforcementContext(
        boundary=Boundary.CLOSURE,
        actor=RequestContext(account_id="acme", agent_id="collaboration_ops_specialist"),
        payload=payload,
        workflow_id="mcp_single_145",
    )

    decision = await ClosureGuardrail(FakeStaleness()).check(None, ec)

    assert "knowledge_base_specialist.update_knowledge" in payload["diagnostics"]["missing_actions"]
    assert any("knowledge_base_specialist.update_knowledge" in q for q in payload["open_questions"])
    assert decision.verdict == Verdict.BLOCK
    assert decision.violations[0].repair_hint["missing_actions"] == [
        "knowledge_base_specialist.update_knowledge"
    ]


def test_s0_passed_complete_trace_has_no_missing_actions() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_146",
        ground_truth=_ground_truth(),
        trace_events=[
            _tool_call("customer_support_specialist", "update_case", "call-1", 1.0),
            _tool_result("customer_support_specialist", "call-1", 2.0),
            _tool_call("knowledge_base_specialist", "update_knowledge", "call-2", 3.0),
            _tool_result("knowledge_base_specialist", "call-2", 4.0),
        ],
        runtime_summary={"status": "ok", "timeout": False},
    )

    assert payload["diagnostics"]["missing_actions"] == []
    assert payload["diagnostics"]["runtime"]["timeout"] is False
    assert payload["open_questions"] == []


def test_timeout_case_marks_incomplete_closure_evidence() -> None:
    payload = build_workflow_closure_payload(
        workflow_id="mcp_single_145",
        ground_truth=_ground_truth(),
        trace_events=[
            _tool_call("customer_support_specialist", "update_case", "call-1", 1.0),
            _tool_result("customer_support_specialist", "call-1", 2.0),
            {
                "agent_name": "collaboration_ops_specialist",
                "event": "delegate_error",
                "ts": 3.0,
                "data": {"error": "TimeoutError: timed out"},
            },
        ],
        runtime_summary={"status": "error", "timeout": True},
    )

    assert payload["diagnostics"]["runtime"]["timeout"] is True
    assert payload["diagnostics"]["runtime"]["partial_trace"] is True
    assert "knowledge_base_specialist.update_knowledge" in payload["diagnostics"]["missing_actions"]
    assert any(question.startswith("timeout_or_partial_trace:") for question in payload["open_questions"])


def test_approval_subset_payload_requires_decision() -> None:
    payload = build_approval_closure_payload(
        workflow_id="approval-wf",
        completed_actions=["review_policy"],
        decision_label="approve",
        rule_citations=["finance/rule-1"],
    )

    assert payload["require_decision"] is True
    assert payload["decision_label"] == "approve"
    assert payload["rule_citations"] == ["finance/rule-1"]
