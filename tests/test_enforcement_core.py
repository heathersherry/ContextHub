from __future__ import annotations

import pytest

from contexthub.enforcement import (
    Boundary,
    EnforcementContext,
    EnforcementService,
    Guardrail,
    GuardrailDecision,
    Verdict,
    Violation,
    ViolationKind,
)
from contexthub.enforcement.contracts import HandoffPacket
from contexthub.models.request import RequestContext


class FakeGuardrail(Guardrail):
    name = "fake"
    applies_to = frozenset({Boundary.HANDOFF})

    def __init__(self, decision: GuardrailDecision | None = None):
        self.called = 0
        self.decision = decision or GuardrailDecision(
            verdict=Verdict.ALLOW,
            reason="ok",
            guardrail=self.name,
        )

    async def check(self, db, ec: EnforcementContext) -> GuardrailDecision:
        self.called += 1
        return self.decision


class FakeAudit:
    def __init__(self):
        self.calls: list[dict] = []

    async def log_strict(self, db, **kwargs) -> None:
        self.calls.append(kwargs)


def _decision(
    verdict: Verdict,
    *,
    guardrail: str = "fake",
    violation: Violation | None = None,
) -> GuardrailDecision:
    return GuardrailDecision(
        verdict=verdict,
        violations=[violation] if violation else [],
        reason=verdict.value,
        guardrail=guardrail,
    )


def _ec(boundary: Boundary = Boundary.HANDOFF) -> EnforcementContext:
    return EnforcementContext(
        boundary=boundary,
        actor=RequestContext(account_id="acme", agent_id="agent-a"),
        recipient=RequestContext(account_id="acme", agent_id="agent-b"),
        declared_context_uris=["ctx://team/policy/foo@v3"],
        workflow_id="workflow-1",
    )


def test_merge_all_allow():
    merged = EnforcementService._merge(
        [_decision(Verdict.ALLOW), _decision(Verdict.ALLOW)]
    )

    assert merged.verdict == Verdict.ALLOW


def test_merge_block_priority():
    merged = EnforcementService._merge(
        [
            _decision(Verdict.ALLOW),
            _decision(Verdict.REPAIR),
            _decision(Verdict.BLOCK),
            _decision(Verdict.ESCALATE),
        ]
    )

    assert merged.verdict == Verdict.BLOCK


def test_merge_escalate_over_repair():
    merged = EnforcementService._merge(
        [_decision(Verdict.REPAIR), _decision(Verdict.ESCALATE)]
    )

    assert merged.verdict == Verdict.ESCALATE


def test_merge_empty_allows():
    merged = EnforcementService._merge([])

    assert merged.verdict == Verdict.ALLOW
    assert "no guardrail" in merged.reason


def test_merge_accumulates_violations():
    v1 = Violation(ViolationKind.INCOMPLETE_HANDOFF, "missing task intent")
    v2 = Violation(ViolationKind.STALE_DEPENDENCY, "stale policy")

    merged = EnforcementService._merge(
        [
            _decision(Verdict.REPAIR, guardrail="handoff", violation=v1),
            _decision(Verdict.BLOCK, guardrail="staleness", violation=v2),
        ]
    )

    assert len(merged.violations) == 2


@pytest.mark.asyncio
async def test_service_skips_non_matching_guardrail():
    guardrail = FakeGuardrail()
    service = EnforcementService([guardrail])

    result = await service.enforce(None, _ec(Boundary.TOOL_CALL))

    assert guardrail.called == 0
    assert result.verdict == Verdict.ALLOW


@pytest.mark.asyncio
async def test_service_calls_matching_guardrail():
    guardrail = FakeGuardrail()
    service = EnforcementService([guardrail])

    result = await service.enforce(None, _ec(Boundary.HANDOFF))

    assert guardrail.called == 1
    assert result.verdict == Verdict.ALLOW


@pytest.mark.asyncio
async def test_service_records_enforcement_audit():
    violation = Violation(
        ViolationKind.INCOMPLETE_HANDOFF,
        "missing object id",
        evidence={"uri": "ctx://team/policy/foo", "version": 3},
    )
    guardrail = FakeGuardrail(
        _decision(Verdict.BLOCK, guardrail="handoff", violation=violation)
    )
    audit = FakeAudit()
    service = EnforcementService([guardrail], audit=audit)

    await service.enforce(None, _ec())

    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["action"] == "enforcement"
    assert call["result"] == "success"
    assert call["metadata"]["verdict"] == "block"
    assert call["metadata"]["boundary"] == "handoff"
    assert call["metadata"]["violations"] == [
        {
            "kind": "incomplete_handoff",
            "message": "missing object id",
            "evidence": {"uri": "ctx://team/policy/foo", "version": 3},
        }
    ]


def test_handoff_packet_static_missing():
    packet = HandoffPacket(
        sender="agent-a",
        recipient="agent-b",
        task_intent="triage ticket",
    )

    assert packet.static_missing({"task_intent", "required_object_ids"}) == [
        "required_object_ids"
    ]


@pytest.mark.asyncio
async def test_service_without_audit_does_not_raise():
    service = EnforcementService([], None)

    result = await service.enforce(None, _ec())

    assert result.verdict == Verdict.ALLOW
