from __future__ import annotations

import copy

import pytest

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.contracts import HandoffPacket
from contexthub.enforcement.decision import Verdict, ViolationKind
from contexthub.enforcement.guardrails.flow import FlowGuardrail
from contexthub.models.request import RequestContext
from contexthub.services.access_decision import AccessDecision


class FakeACL:
    def __init__(self, decisions: dict[str, AccessDecision]):
        self._decisions = decisions
        self.calls: list[tuple[str, str]] = []

    async def check_read_access(self, db, uri: str, ctx: RequestContext) -> AccessDecision:
        self.calls.append((uri, ctx.agent_id))
        return self._decisions[uri]


def _ctx(
    payload: dict,
    *,
    recipient: RequestContext | None = None,
    actor: RequestContext | None = None,
    boundary: Boundary = Boundary.HANDOFF,
) -> EnforcementContext:
    return EnforcementContext(
        boundary=boundary,
        actor=actor or RequestContext(account_id="acme", agent_id="actor-agent"),
        recipient=recipient
        if recipient is not None
        else RequestContext(account_id="acme", agent_id="recipient-agent"),
        payload=payload,
    )


def _item(uri: str, **fields) -> dict:
    return {"uri": uri, "fields": fields}


@pytest.mark.asyncio
async def test_all_visible_allows_with_sanitized_payload():
    uri = "ctx://team/eng/case-1"
    payload = {"items": [_item(uri, summary="ok", note="visible")]}
    acl = FakeACL(
        {uri: AccessDecision(allowed=True, field_masks=None, reason="default baseline")}
    )

    result = await FlowGuardrail(acl).check(None, _ctx(payload))

    assert result.verdict == Verdict.ALLOW
    assert result.reason == "flow ok"
    assert result.sanitized_payload == payload


@pytest.mark.asyncio
async def test_unauthorized_context_is_dropped_and_blocks():
    uri = "ctx://team/hr/secret"
    payload = {"items": [_item(uri, summary="secret", ssn="123")]}
    acl = FakeACL(
        {uri: AccessDecision(allowed=False, field_masks=None, reason="default baseline")}
    )

    result = await FlowGuardrail(acl).check(None, _ctx(payload))

    assert result.verdict == Verdict.BLOCK
    assert result.sanitized_payload == {"items": []}
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation.kind == ViolationKind.UNAUTHORIZED_FLOW
    assert "cannot receive" in violation.message
    assert violation.evidence == {
        "uri": uri,
        "reason": "default baseline",
        "dropped": True,
    }


@pytest.mark.asyncio
async def test_masked_fields_repair_with_redacted_payload():
    uri = "ctx://team/hr/report"
    payload = {"items": [_item(uri, summary="ok", ssn="123")]}
    acl = FakeACL(
        {uri: AccessDecision(allowed=True, field_masks=["ssn"], reason="explicit allow")}
    )

    result = await FlowGuardrail(acl).check(None, _ctx(payload))

    assert result.verdict == Verdict.REPAIR
    assert result.sanitized_payload == {
        "items": [{"uri": uri, "fields": {"summary": "ok", "ssn": "[REDACTED]"}}]
    }
    assert result.violations[0].evidence == {
        "uri": uri,
        "masked": ["ssn"],
        "dropped": False,
    }


@pytest.mark.asyncio
async def test_mixed_items_block_priority_and_sanitizes_remaining_items():
    blocked_uri = "ctx://team/hr/blocked"
    masked_uri = "ctx://team/hr/masked"
    clean_uri = "ctx://team/eng/clean"
    payload = {
        "items": [
            _item(blocked_uri, summary="drop me"),
            _item(masked_uri, summary="keep me", ssn="123"),
            _item(clean_uri, summary="clean"),
        ]
    }
    acl = FakeACL(
        {
            blocked_uri: AccessDecision(
                allowed=False,
                field_masks=None,
                reason="explicit deny",
            ),
            masked_uri: AccessDecision(
                allowed=True,
                field_masks=["ssn"],
                reason="explicit allow",
            ),
            clean_uri: AccessDecision(
                allowed=True,
                field_masks=None,
                reason="default baseline",
            ),
        }
    )

    result = await FlowGuardrail(acl).check(None, _ctx(payload))

    assert result.verdict == Verdict.BLOCK
    assert result.sanitized_payload == {
        "items": [
            {
                "uri": masked_uri,
                "fields": {"summary": "keep me", "ssn": "[REDACTED]"},
            },
            {"uri": clean_uri, "fields": {"summary": "clean"}},
        ]
    }
    assert [v.evidence["dropped"] for v in result.violations] == [True, False]


@pytest.mark.asyncio
async def test_recipient_none_uses_actor_for_acl_subject():
    uri = "ctx://team/eng/case-2"
    payload = {"items": [_item(uri, summary="ok")]}
    actor = RequestContext(account_id="acme", agent_id="actor-agent")
    acl = FakeACL(
        {uri: AccessDecision(allowed=True, field_masks=None, reason="default baseline")}
    )
    ec = EnforcementContext(
        boundary=Boundary.INVOCATION,
        actor=actor,
        recipient=None,
        payload=payload,
    )

    result = await FlowGuardrail(acl).check(None, ec)

    assert result.verdict == Verdict.ALLOW
    assert acl.calls == [(uri, "actor-agent")]


@pytest.mark.asyncio
async def test_check_does_not_mutate_original_payload():
    uri = "ctx://team/hr/report"
    payload = {"items": [_item(uri, summary="ok", ssn="123")]}
    original = copy.deepcopy(payload)
    acl = FakeACL(
        {uri: AccessDecision(allowed=True, field_masks=["ssn"], reason="explicit allow")}
    )

    result = await FlowGuardrail(acl).check(None, _ctx(payload))

    assert result.verdict == Verdict.REPAIR
    assert payload == original
    assert result.sanitized_payload != payload


def test_applies_to_flow_boundaries():
    assert FlowGuardrail.applies_to == frozenset(
        {
            Boundary.HANDOFF,
            Boundary.SHARED_MEMORY_WRITE,
            Boundary.TOOL_CALL,
            Boundary.INVOCATION,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        HandoffPacket(
            sender="agent-a",
            recipient="agent-b",
            task_intent="handoff",
        ).model_dump(),
        {"contract": {"tool_name": "search"}, "args": {"query": "x"}},
        {"items": [{"uri": "ctx://team/eng/case-3", "fields": "not-a-dict"}]},
    ],
)
async def test_payload_shape_not_applicable_allows_without_acl_access(payload):
    acl = FakeACL({})

    result = await FlowGuardrail(acl).check(None, _ctx(payload))

    assert result.verdict == Verdict.ALLOW
    assert "not applicable" in result.reason
    assert result.sanitized_payload is None
    assert acl.calls == []
