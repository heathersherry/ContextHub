from __future__ import annotations

import uuid

import pytest

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import Verdict
from contexthub.enforcement.guardrails.flow import FlowGuardrail
from contexthub.models.request import RequestContext


pytestmark = pytest.mark.asyncio


async def _insert_context(
    db,
    uri: str,
    *,
    scope: str = "team",
    owner_space: str | None = "",
    l1: str = "flow fixture",
) -> None:
    await db.execute(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id, l1_content
        )
        VALUES (
            $1, $2, 'memory', $3, $4, current_setting('app.account_id'), $5
        )
        """,
        uuid.uuid4(),
        uri,
        scope,
        owner_space,
        l1,
    )


async def _insert_policy(
    db,
    *,
    pattern: str,
    principal: str,
    effect: str = "allow",
    field_masks: list[str] | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO access_policies (
            resource_uri_pattern, principal, effect, actions,
            field_masks, account_id
        )
        VALUES ($1, $2, $3, $4::text[], $5, current_setting('app.account_id'))
        """,
        pattern,
        principal,
        effect,
        ["read"],
        field_masks,
    )


def _ec(recipient: RequestContext, uri: str, fields: dict) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.HANDOFF,
        actor=RequestContext(account_id="acme", agent_id="actor-agent"),
        recipient=recipient,
        payload={"items": [{"uri": uri, "fields": fields}]},
    )


async def test_real_unauthorized_flow_is_dropped(acme_session, phase2_services):
    uri = f"ctx://agent/other/flow-secret-{uuid.uuid4().hex[:8]}"
    recipient = RequestContext(account_id="acme", agent_id="query-agent")
    await _insert_context(
        acme_session,
        uri,
        scope="agent",
        owner_space="other",
        l1="private note",
    )

    result = await FlowGuardrail(phase2_services.acl).check(
        acme_session,
        _ec(recipient, uri, {"summary": "private note"}),
    )

    assert result.verdict == Verdict.BLOCK
    assert result.sanitized_payload == {"items": []}
    assert result.violations[0].evidence["dropped"] is True


async def test_real_visible_root_team_context_allows(acme_session, phase2_services):
    uri = f"ctx://team/flow-public-{uuid.uuid4().hex[:8]}"
    recipient = RequestContext(account_id="acme", agent_id="query-agent")
    await _insert_context(
        acme_session,
        uri,
        scope="team",
        owner_space="",
        l1="root-visible note",
    )

    result = await FlowGuardrail(phase2_services.acl).check(
        acme_session,
        _ec(recipient, uri, {"summary": "root-visible note"}),
    )

    assert result.verdict == Verdict.ALLOW
    assert result.sanitized_payload == {
        "items": [{"uri": uri, "fields": {"summary": "root-visible note"}}]
    }


async def test_real_field_mask_repairs_payload(acme_session, phase2_services):
    uri = f"ctx://team/data/analytics/flow-mask-{uuid.uuid4().hex[:8]}"
    recipient = RequestContext(account_id="acme", agent_id="query-agent")
    await _insert_context(
        acme_session,
        uri,
        scope="team",
        owner_space="data/analytics",
        l1="masked note",
    )
    await _insert_policy(
        acme_session,
        pattern=uri,
        principal="query-agent",
        effect="allow",
        field_masks=["ssn"],
    )

    result = await FlowGuardrail(phase2_services.acl).check(
        acme_session,
        _ec(recipient, uri, {"summary": "masked note", "ssn": "123-45-6789"}),
    )

    assert result.verdict == Verdict.REPAIR
    assert result.sanitized_payload == {
        "items": [
            {
                "uri": uri,
                "fields": {"summary": "masked note", "ssn": "[REDACTED]"},
            }
        ]
    }
    assert result.violations[0].evidence == {
        "uri": uri,
        "masked": ["ssn"],
        "dropped": False,
    }
