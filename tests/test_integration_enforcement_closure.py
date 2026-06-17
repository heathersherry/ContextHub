from __future__ import annotations

import uuid

import pytest

from contexthub.enforcement import (
    Boundary,
    EnforcementContext,
    StalenessChecker,
    Verdict,
    ViolationKind,
)
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.models.request import RequestContext


pytestmark = pytest.mark.asyncio


async def _insert_context(acme_session, *, status: str, version: int = 1) -> str:
    uri = f"ctx://team/closure-fixtures/{status}-{uuid.uuid4().hex[:8]}"
    await acme_session.execute(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id,
            status, version, l0_content
        )
        VALUES (
            $1, $2, 'memory', 'team', 'engineering',
            current_setting('app.account_id'), $3, $4, 'closure fixture'
        )
        """,
        uuid.uuid4(),
        uri,
        status,
        version,
    )
    return uri


def _payload():
    return {
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


def _ec(refs: list[str]) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.CLOSURE,
        actor=RequestContext(account_id="acme", agent_id="agent-a"),
        payload=_payload(),
        declared_context_uris=refs,
        workflow_id="workflow-closure",
    )


def _kinds(decision):
    return {v.kind for v in decision.violations}


async def test_blocks_closure_with_stale_dependency(acme_session):
    uri = await _insert_context(acme_session, status="stale")
    guardrail = ClosureGuardrail(StalenessChecker())

    decision = await guardrail.check(acme_session, _ec([uri]))

    assert decision.verdict == Verdict.BLOCK
    assert ViolationKind.STALE_DEPENDENCY in _kinds(decision)


async def test_allows_closure_with_active_dependency(acme_session):
    uri = await _insert_context(acme_session, status="active")
    guardrail = ClosureGuardrail(StalenessChecker())

    decision = await guardrail.check(acme_session, _ec([uri]))

    assert decision.verdict == Verdict.ALLOW


async def test_blocks_closure_with_runtime_ref_version_mismatch(acme_session):
    uri = await _insert_context(acme_session, status="active", version=2)
    guardrail = ClosureGuardrail(StalenessChecker())

    decision = await guardrail.check(acme_session, _ec([f"{uri}@v3"]))

    assert decision.verdict == Verdict.BLOCK
    assert ViolationKind.STALE_DEPENDENCY in _kinds(decision)
    assert decision.violations[0].evidence["version_mismatch"] is True
    assert decision.violations[0].evidence["expected_version"] == 3
    assert decision.violations[0].evidence["current_version"] == 2
