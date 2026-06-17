from __future__ import annotations

import uuid

import pytest

from contexthub.enforcement import Boundary, EnforcementContext, StalenessChecker
from contexthub.enforcement.decision import Verdict, ViolationKind
from contexthub.enforcement.guardrails.tool_state import ToolStateGuardrail
from contexthub.models.request import RequestContext


pytestmark = pytest.mark.asyncio


async def _insert_context(acme_session, *, status: str, version: int = 1) -> str:
    uri = f"ctx://team/tool-state-fixtures/{status}-{uuid.uuid4().hex[:8]}"
    await acme_session.execute(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id,
            status, version, l0_content
        )
        VALUES (
            $1, $2, 'memory', 'team', 'engineering',
            current_setting('app.account_id'), $3, $4, 'tool state fixture'
        )
        """,
        uuid.uuid4(),
        uri,
        status,
        version,
    )
    return uri


def _ec(depends_on_uris: list[str]) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.TOOL_CALL,
        actor=RequestContext(account_id="acme", agent_id="agent-a"),
        payload={
            "contract": {
                "tool_name": "update_incident",
                "required_role": None,
                "arg_schema": {
                    "required": ["object_id", "state"],
                    "properties": {
                        "object_id": {"type": "string"},
                        "state": {"enum": ["open", "resolved"]},
                    },
                },
                "provenance_bound_args": [],
                "mutation_intent": "",
                "depends_on_uris": depends_on_uris,
            },
            "tool_args": {"object_id": "inc-1", "state": "resolved"},
        },
    )


async def test_real_stale_dependency_repairs(acme_session):
    uri = await _insert_context(acme_session, status="stale")
    guardrail = ToolStateGuardrail(StalenessChecker())

    decision = await guardrail.check(acme_session, _ec([uri]))

    assert decision.verdict == Verdict.REPAIR
    assert decision.violations[0].kind == ViolationKind.STALE_DEPENDENCY


async def test_real_active_dependency_allows(acme_session):
    uri = await _insert_context(acme_session, status="active")
    guardrail = ToolStateGuardrail(StalenessChecker())

    decision = await guardrail.check(acme_session, _ec([uri]))

    assert decision.verdict == Verdict.ALLOW


async def test_real_version_mismatch_runtime_ref_repairs(acme_session):
    uri = await _insert_context(acme_session, status="active", version=2)
    guardrail = ToolStateGuardrail(StalenessChecker())

    decision = await guardrail.check(acme_session, _ec([f"{uri}@v3"]))

    assert decision.verdict == Verdict.REPAIR
    assert decision.violations[0].kind == ViolationKind.STALE_DEPENDENCY
    assert decision.violations[0].evidence["version_mismatch"] is True
