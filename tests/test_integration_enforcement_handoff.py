from __future__ import annotations

import uuid

import pytest

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import Verdict, ViolationKind
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.staleness import StalenessChecker
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService

pytestmark = pytest.mark.asyncio


async def _insert_context(
    acme_session,
    *,
    scope: str = "team",
    owner_space: str | None = "engineering",
    status: str = "active",
    version: int = 1,
) -> str:
    uri = f"ctx://team/handoff-fixtures/{uuid.uuid4().hex[:8]}"
    await acme_session.execute(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id,
            status, version, l0_content
        )
        VALUES (
            $1, $2, 'memory', $3, $4,
            current_setting('app.account_id'), $5, $6, 'handoff fixture'
        )
        """,
        uuid.uuid4(),
        uri,
        scope,
        owner_space,
        status,
        version,
    )
    return uri


def _guardrail() -> HandoffGuardrail:
    return HandoffGuardrail(
        ACLService(),
        StalenessChecker(),
        object_uri_resolver=lambda value: value,
        version_uri_resolver=lambda value: value,
    )


def _ec(
    *,
    required_object_ids: list[str],
    context_versions: list[str] | None = None,
) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.HANDOFF,
        actor=RequestContext(account_id="acme", agent_id="analysis-agent"),
        recipient=RequestContext(account_id="acme", agent_id="query-agent"),
        payload={
            "sender": "analysis-agent",
            "recipient": "query-agent",
            "task_intent": "continue incident analysis",
            "required_object_ids": required_object_ids,
            "source_artifacts": [],
            "expected_action": "validate and execute next step",
            "context_versions": context_versions or [],
        },
        workflow_id="workflow-handoff",
    )


async def test_handoff_real_acl_allows_visible_context(acme_session):
    uri = await _insert_context(acme_session, scope="team", owner_space="")

    result = await _guardrail().check(
        acme_session,
        _ec(required_object_ids=[uri]),
    )

    assert result.verdict == Verdict.ALLOW
    assert ViolationKind.UNAUTHORIZED_FLOW not in {v.kind for v in result.violations}


async def test_handoff_real_acl_blocks_private_agent_context(acme_session):
    uri = await _insert_context(
        acme_session,
        scope="agent",
        owner_space="other-agent",
    )

    result = await _guardrail().check(
        acme_session,
        _ec(required_object_ids=[uri]),
    )

    assert result.verdict == Verdict.BLOCK
    assert ViolationKind.UNAUTHORIZED_FLOW in {v.kind for v in result.violations}


async def test_handoff_real_stale_dependency_repairs(acme_session):
    uri = await _insert_context(acme_session, status="stale")

    result = await _guardrail().check(
        acme_session,
        _ec(required_object_ids=[uri], context_versions=[uri]),
    )

    assert result.verdict == Verdict.REPAIR
    assert ViolationKind.STALE_DEPENDENCY in {v.kind for v in result.violations}


async def test_handoff_real_version_mismatch_repairs(acme_session):
    uri = await _insert_context(acme_session, status="active", version=2)

    result = await _guardrail().check(
        acme_session,
        _ec(required_object_ids=[uri], context_versions=[f"{uri}@v3"]),
    )

    stale_violation = next(
        v for v in result.violations if v.kind == ViolationKind.STALE_DEPENDENCY
    )
    assert result.verdict == Verdict.REPAIR
    assert stale_violation.evidence["expected_version"] == 3
    assert stale_violation.evidence["current_version"] == 2
    assert stale_violation.evidence["version_mismatch"] is True
