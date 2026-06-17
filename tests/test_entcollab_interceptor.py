from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contexthub.enforcement.context import Boundary
from contexthub.enforcement.decision import GuardrailDecision, Verdict, Violation, ViolationKind
from contexthub.enforcement.repair import RepairPlan, RepairStrategy
from integrations.entcollabbench.interceptor import (
    EnforcementInterceptor,
    build_approval_checklist,
)
from integrations.entcollabbench.world_loader import LoadedWorld


class FakeRepo:
    @asynccontextmanager
    async def session(self, account_id: str):
        yield {"account_id": account_id}


class FakeService:
    def __init__(self, decision: GuardrailDecision):
        self.decision = decision
        self.calls = []

    async def enforce(self, db, ec):
        self.calls.append((db, ec))
        return self.decision


def _interceptor(
    decision: GuardrailDecision | None = None,
    *,
    repair_planner=None,
):
    service = FakeService(decision or GuardrailDecision(Verdict.ALLOW))
    interceptor = EnforcementInterceptor(
        FakeRepo(),
        "acme",
        LoadedWorld(),
        service=service,
        repair_planner=repair_planner or (lambda violations: RepairPlan(RepairStrategy.ESCALATE)),
    )
    return interceptor, service


@pytest.mark.asyncio
async def test_handoff_constructs_enforcement_context():
    interceptor, service = _interceptor()
    packet = {
        "sender": "a",
        "recipient": "b",
        "task_intent": "handoff work",
        "expected_action": "continue",
        "required_object_ids": ["incident/INC001"],
        "context_versions": ["ctx://entcollab/policy/P1@v1"],
    }

    decision = await interceptor.on_handoff("a", "b", packet)

    assert decision.verdict == Verdict.ALLOW
    db, ec = service.calls[0]
    assert db == {"account_id": "acme"}
    assert ec.boundary == Boundary.HANDOFF
    assert ec.actor.agent_id == "a"
    assert ec.recipient.agent_id == "b"
    assert ec.payload == packet
    assert ec.declared_context_uris == ["ctx://entcollab/policy/P1@v1"]


@pytest.mark.asyncio
async def test_closure_constructs_enforcement_context():
    interceptor, service = _interceptor()
    checklist = {
        "anchor": {"workflow_id": "wf1", "required_actions": [], "required_evidence": []},
        "completed_actions": [],
        "evidence": {},
        "open_questions": [],
    }

    await interceptor.on_closure("a", checklist, ["ctx://entcollab/policy/P1@v1"], "wf1")

    _, ec = service.calls[0]
    assert ec.boundary == Boundary.CLOSURE
    assert ec.actor.agent_id == "a"
    assert ec.workflow_id == "wf1"
    assert ec.payload == checklist
    assert ec.declared_context_uris == ["ctx://entcollab/policy/P1@v1"]


@pytest.mark.asyncio
async def test_tool_call_constructs_enforcement_context_without_tool_guardrail_dependency():
    interceptor, service = _interceptor()
    contract = {"tool_name": "hr.update_case", "depends_on_uris": ["ctx://entcollab/tool_schema/hr@v1"]}
    args = {"case_id": "57"}

    await interceptor.on_tool_call("hr_service_specialist", contract, args)

    _, ec = service.calls[0]
    assert ec.boundary == Boundary.TOOL_CALL
    assert ec.payload == {"contract": contract, "tool_args": args}
    assert ec.declared_context_uris == ["ctx://entcollab/tool_schema/hr@v1"]


def test_apply_allows_allowed_decision():
    interceptor, _ = _interceptor()

    action = interceptor.apply(GuardrailDecision(Verdict.ALLOW))

    assert action.action == "allow"
    assert action.allow is True
    assert action.retry is False


def test_apply_blocks_block_decision():
    interceptor, _ = _interceptor()

    action = interceptor.apply(GuardrailDecision(Verdict.BLOCK))

    assert action.action == "block"
    assert action.allow is False
    assert action.retry is False


def test_apply_repair_with_deterministic_patch_retries():
    violation = Violation(ViolationKind.SCHEMA_OR_ENUM, "bad enum")

    def repair_planner(violations):
        return RepairPlan(
            RepairStrategy.DETERMINISTIC,
            violations=list(violations),
            patch={"state": "resolved"},
        )

    interceptor, _ = _interceptor(repair_planner=repair_planner)

    action = interceptor.apply(GuardrailDecision(Verdict.REPAIR, violations=[violation]))

    assert action.action == "retry_with_patch"
    assert action.retry is True
    assert action.patch == {"state": "resolved"}


def test_apply_repair_with_escalate_plan_goes_pending():
    violation = Violation(ViolationKind.STALE_DEPENDENCY, "stale")

    def repair_planner(violations):
        return RepairPlan(RepairStrategy.ESCALATE, violations=list(violations))

    interceptor, _ = _interceptor(repair_planner=repair_planner)

    action = interceptor.apply(GuardrailDecision(Verdict.REPAIR, violations=[violation]))

    assert action.action == "pending"
    assert action.pending is True
    assert action.retry is False


def test_approval_checklist_requires_decision():
    checklist = build_approval_checklist(
        workflow_id="approval-wf",
        decision_label="approve",
        rule_citations=["finance/rulebook.md#L10"],
    )

    assert checklist["require_decision"] is True
    assert checklist["decision_label"] == "approve"
    assert checklist["rule_citations"] == ["finance/rulebook.md#L10"]
