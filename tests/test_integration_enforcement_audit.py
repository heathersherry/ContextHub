from __future__ import annotations

import uuid

import pytest

from contexthub.enforcement import (
    Boundary,
    EnforcementContext,
    EnforcementService,
    Guardrail,
    GuardrailDecision,
    StalenessChecker,
    Verdict,
    Violation,
    ViolationKind,
)
from contexthub.models.request import RequestContext
from contexthub.services.audit_service import AuditService


pytestmark = pytest.mark.asyncio


class BlockingGuardrail(Guardrail):
    name = "blocking"
    applies_to = frozenset({Boundary.HANDOFF})

    async def check(self, db, ec: EnforcementContext) -> GuardrailDecision:
        return GuardrailDecision(
            verdict=Verdict.BLOCK,
            violations=[
                Violation(
                    ViolationKind.STALE_DEPENDENCY,
                    "policy is stale",
                    evidence={"uri": "ctx://team/policies/onboarding"},
                )
            ],
            reason="blocked",
            guardrail=self.name,
        )


async def _insert_context(acme_session, *, status: str, version: int = 1) -> str:
    uri = f"ctx://team/enforcement-fixtures/{status}-{uuid.uuid4().hex[:8]}"
    await acme_session.execute(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id,
            status, version, l0_content
        )
        VALUES (
            $1, $2, 'memory', 'team', 'engineering',
            current_setting('app.account_id'), $3, $4, 'enforcement fixture'
        )
        """,
        uuid.uuid4(),
        uri,
        status,
        version,
    )
    return uri


async def test_staleness_active(acme_session):
    uri = await _insert_context(acme_session, status="active")

    result = (await StalenessChecker().check_uris(acme_session, [uri]))[uri]

    assert result.is_stale is False
    assert result.is_blocked is False
    assert result.is_unknown is False


async def test_staleness_stale(acme_session):
    uri = await _insert_context(acme_session, status="stale")

    result = (await StalenessChecker().check_uris(acme_session, [uri]))[uri]

    assert result.is_stale is True


@pytest.mark.parametrize("status", ["archived", "deleted"])
async def test_staleness_archived_or_deleted_is_blocked(acme_session, status):
    uri = await _insert_context(acme_session, status=status)

    result = (await StalenessChecker().check_uris(acme_session, [uri]))[uri]

    assert result.is_blocked is True


async def test_staleness_unknown_is_blocked(acme_session):
    uri = f"ctx://team/enforcement-fixtures/missing-{uuid.uuid4().hex[:8]}"

    result = (await StalenessChecker().check_uris(acme_session, [uri]))[uri]

    assert result.is_unknown is True
    assert result.is_blocked is True
    assert result.status is None


async def test_enforcement_audit_persists(acme_session, db_pool):
    service = EnforcementService(
        [BlockingGuardrail()],
        audit=AuditService(pool=db_pool),
    )
    ec = EnforcementContext(
        boundary=Boundary.HANDOFF,
        actor=RequestContext(account_id="acme", agent_id="agent-a"),
        recipient=RequestContext(account_id="acme", agent_id="agent-b"),
        declared_context_uris=["ctx://team/policies/onboarding@v3"],
        workflow_id="workflow-enforcement",
    )

    result = await service.enforce(acme_session, ec)
    row = await acme_session.fetchrow(
        """
        SELECT action, result, metadata
        FROM audit_log
        WHERE action = 'enforcement'
        ORDER BY timestamp DESC
        LIMIT 1
        """
    )

    assert result.verdict == Verdict.BLOCK
    assert row["action"] == "enforcement"
    assert row["result"] == "success"
    assert row["metadata"]["verdict"] == "block"
    assert row["metadata"]["violations"][0]["kind"] == "stale_dependency"


async def test_staleness_version_mismatch_from_runtime_ref(acme_session):
    uri = await _insert_context(acme_session, status="active", version=2)

    result = (await StalenessChecker().check_refs(acme_session, [f"{uri}@v3"]))[uri]
    flagged = await StalenessChecker().any_stale_or_blocked_refs(
        acme_session,
        [f"{uri}@v3"],
    )

    assert result.version_mismatch is True
    assert result.expected_version == 3
    assert result.current_version == 2
    assert flagged == [result]
