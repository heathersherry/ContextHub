from __future__ import annotations

import pytest

from contexthub.enforcement import (
    Boundary,
    EnforcementContext,
    StalenessResult,
    Verdict,
    ViolationKind,
)
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.models.request import RequestContext


pytestmark = pytest.mark.asyncio


class FakeStaleness:
    def __init__(self, hits: list[StalenessResult] | None = None):
        self.hits = hits or []
        self.refs: list[str] | None = None

    async def any_stale_or_blocked_refs(self, db, refs):
        self.refs = refs
        return self.hits


def _payload(**overrides):
    payload = {
        "anchor": {
            "workflow_id": "workflow-closure",
            "required_actions": ["send_summary"],
            "required_evidence": ["ticket"],
        },
        "completed_actions": ["send_summary"],
        "evidence": {"ticket": "ctx://team/tickets/123"},
        "open_questions": [],
        "require_decision": False,
        "decision_label": None,
        "rule_citations": None,
    }
    payload.update(overrides)
    return payload


def _ec(payload=None, declared_context_uris=None) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.CLOSURE,
        actor=RequestContext(account_id="acme", agent_id="agent-a"),
        payload=payload if payload is not None else _payload(),
        declared_context_uris=declared_context_uris or [],
        workflow_id="workflow-closure",
    )


async def _check(payload=None, *, staleness=None, guardrail=None, refs=None):
    guardrail = guardrail or ClosureGuardrail(staleness or FakeStaleness())
    return await guardrail.check(None, _ec(payload, refs))


def _kinds(decision):
    return {v.kind for v in decision.violations}


async def test_allows_closed_workflow():
    decision = await _check()

    assert decision.verdict == Verdict.ALLOW
    assert decision.reason == "workflow closed"


async def test_blocks_missing_required_action():
    payload = _payload(
        anchor={
            "workflow_id": "workflow-closure",
            "required_actions": ["send_summary", "close_ticket"],
            "required_evidence": ["ticket"],
        },
        completed_actions=["send_summary"],
    )

    decision = await _check(payload)

    assert decision.verdict == Verdict.BLOCK
    assert _kinds(decision) == {ViolationKind.UNCLOSED_WORKFLOW}
    assert decision.violations[0].repair_hint["missing_actions"] == ["close_ticket"]


async def test_blocks_missing_evidence():
    decision = await _check(_payload(evidence={}))

    assert decision.verdict == Verdict.BLOCK
    assert _kinds(decision) == {ViolationKind.UNCLOSED_WORKFLOW}
    assert decision.violations[0].repair_hint["missing_evidence"] == ["ticket"]


async def test_blocks_open_questions():
    decision = await _check(_payload(open_questions=["which rollout region?"]))

    assert decision.verdict == Verdict.BLOCK
    assert _kinds(decision) == {ViolationKind.UNCLOSED_WORKFLOW}
    assert decision.violations[0].repair_hint["open_questions"] == [
        "which rollout region?"
    ]


async def test_blocks_approval_missing_label():
    decision = await _check(_payload(require_decision=True, decision_label=None))

    assert decision.verdict == Verdict.BLOCK
    assert _kinds(decision) == {ViolationKind.WEAK_DECISION}
    assert decision.violations[0].repair_hint["has_label"] is False


async def test_blocks_approval_missing_citation():
    decision = await _check(
        _payload(
            require_decision=True,
            decision_label="approve",
            rule_citations=None,
        )
    )

    assert decision.verdict == Verdict.BLOCK
    assert _kinds(decision) == {ViolationKind.WEAK_DECISION}
    assert decision.violations[0].repair_hint["has_citations"] is False


async def test_allows_complete_approval_decision():
    decision = await _check(
        _payload(
            require_decision=True,
            decision_label="approve",
            rule_citations=["policy-7"],
        )
    )

    assert decision.verdict == Verdict.ALLOW


async def test_non_approval_does_not_require_decision_fields():
    decision = await _check(
        _payload(
            require_decision=False,
            decision_label=None,
            rule_citations=None,
        )
    )

    assert decision.verdict == Verdict.ALLOW
    assert ViolationKind.WEAK_DECISION not in _kinds(decision)


async def test_accumulates_multiple_violations():
    decision = await _check(_payload(completed_actions=[], evidence={}))

    assert decision.verdict == Verdict.BLOCK
    assert len(decision.violations) >= 2
    assert _kinds(decision) == {ViolationKind.UNCLOSED_WORKFLOW}


async def test_applies_to_closure_boundary():
    assert ClosureGuardrail.applies_to == frozenset({Boundary.CLOSURE})


async def test_default_require_decision_fallback_when_payload_omits_field():
    payload = _payload()
    payload.pop("require_decision")
    guardrail = ClosureGuardrail(FakeStaleness(), default_require_decision=True)

    decision = await _check(payload, guardrail=guardrail)

    assert decision.verdict == Verdict.BLOCK
    assert _kinds(decision) == {ViolationKind.WEAK_DECISION}


async def test_explicit_payload_false_overrides_default_require_decision():
    guardrail = ClosureGuardrail(FakeStaleness(), default_require_decision=True)

    decision = await _check(_payload(require_decision=False), guardrail=guardrail)

    assert decision.verdict == Verdict.ALLOW


async def test_allows_non_closure_payload_shape():
    decision = await _check({"completed_actions": []})

    assert decision.verdict == Verdict.ALLOW
    assert "not applicable" in decision.reason


async def test_blocks_version_mismatched_dependency_from_refs():
    staleness = FakeStaleness(
        [
            StalenessResult(
                uri="ctx://team/policies/closure",
                status="active",
                is_stale=False,
                version_mismatch=True,
                is_blocked=False,
                is_unknown=False,
                current_version=2,
                expected_version=3,
            )
        ]
    )

    decision = await _check(
        staleness=staleness,
        refs=["ctx://team/policies/closure@v3"],
    )

    assert staleness.refs == ["ctx://team/policies/closure@v3"]
    assert decision.verdict == Verdict.BLOCK
    assert _kinds(decision) == {ViolationKind.STALE_DEPENDENCY}
    assert decision.violations[0].evidence["version_mismatch"] is True
