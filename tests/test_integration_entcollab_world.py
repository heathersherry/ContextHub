from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import Verdict, ViolationKind
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.staleness import StalenessChecker
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from integrations.entcollabbench import mapping
from integrations.entcollabbench.world_loader import WorldLoader


pytestmark = pytest.mark.asyncio


def _sample_world(*, policy_version: int = 1):
    return {
        "roles": ["hr_service_specialist", "developer_engineer"],
        "tool_schemas": [
            {
                "tool_name": "hr",
                "version": 1,
                "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}}},
            }
        ],
        "policies": [
            {
                "policy_id": "FIN-APPROVAL-001",
                "version": policy_version,
                "text": "Approval requests require cited rulebook rationale.",
            }
        ],
        "objects": [
            {
                "object_id": "hr_case/57",
                "owner_space": "engineering",
                "depends_on_uris": [mapping.policy_uri("FIN-APPROVAL-001")],
            }
        ],
    }


async def test_world_loader_inserts_base_context_uris(repo, acme_session):
    loaded = await WorldLoader(repo, "acme").load(_sample_world())

    assert loaded.loaded_uris
    assert mapping.object_uri("hr_case/57") in loaded.loaded_uris
    assert all("@v" not in uri for uri in loaded.loaded_uris)

    rows = await acme_session.fetch(
        """
        SELECT uri, status, version
        FROM contexts
        WHERE uri = ANY($1::text[])
        ORDER BY uri
        """,
        sorted(loaded.loaded_uris),
    )
    assert {row["uri"] for row in rows} == loaded.loaded_uris
    assert {row["status"] for row in rows} == {"active"}


async def test_loaded_world_object_exists(repo):
    loaded = await WorldLoader(repo, "acme").load(_sample_world())

    assert loaded.object_exists("hr_case/57") is True
    assert loaded.object_exists(mapping.object_uri("hr_case/57")) is True
    assert loaded.object_exists("hr_case/unknown") is False


async def test_loaded_object_allows_visible_handoff_and_blocks_invisible_recipient(repo, acme_session):
    loaded = await WorldLoader(repo, "acme").load(_sample_world())
    guardrail = HandoffGuardrail(
        ACLService(),
        StalenessChecker(),
        object_uri_resolver=loaded.object_uri,
        version_uri_resolver=mapping.resolve_version_tag,
    )

    visible = await guardrail.check(
        acme_session,
        _handoff_ec("query-agent", ["hr_case/57"]),
    )
    invisible = await guardrail.check(
        acme_session,
        _handoff_ec("other-agent", ["hr_case/57"]),
    )

    assert visible.verdict == Verdict.ALLOW
    assert invisible.verdict == Verdict.BLOCK
    assert ViolationKind.UNAUTHORIZED_FLOW in {v.kind for v in invisible.violations}


async def test_closure_detects_stale_loaded_dependency(repo, acme_session):
    await WorldLoader(repo, "acme").load(_sample_world())
    policy_uri = mapping.policy_uri("FIN-APPROVAL-001")
    await acme_session.execute(
        "UPDATE contexts SET status = 'stale' WHERE uri = $1",
        policy_uri,
    )

    decision = await ClosureGuardrail(StalenessChecker()).check(
        acme_session,
        _closure_ec([policy_uri]),
    )

    assert decision.verdict == Verdict.BLOCK
    assert ViolationKind.STALE_DEPENDENCY in {v.kind for v in decision.violations}


async def test_closure_detects_loaded_dependency_version_mismatch(repo, acme_session):
    await WorldLoader(repo, "acme").load(_sample_world(policy_version=2))
    declared_ref = mapping.policy_uri("FIN-APPROVAL-001", version=3)

    decision = await ClosureGuardrail(StalenessChecker()).check(
        acme_session,
        _closure_ec([declared_ref]),
    )

    stale = next(v for v in decision.violations if v.kind == ViolationKind.STALE_DEPENDENCY)
    assert decision.verdict == Verdict.BLOCK
    assert stale.evidence["version_mismatch"] is True
    assert stale.evidence["expected_version"] == 3
    assert stale.evidence["current_version"] == 2


def _handoff_ec(recipient: str, required_object_ids: list[str]) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.HANDOFF,
        actor=RequestContext(account_id="acme", agent_id="hr_service_specialist"),
        recipient=RequestContext(account_id="acme", agent_id=recipient),
        payload={
            "sender": "hr_service_specialist",
            "recipient": recipient,
            "task_intent": "resolve hr case",
            "required_object_ids": required_object_ids,
            "source_artifacts": [],
            "expected_action": "continue investigation",
            "context_versions": [],
        },
    )


def _closure_ec(refs: list[str]) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.CLOSURE,
        actor=RequestContext(account_id="acme", agent_id="finance_approval_specialist"),
        payload={
            "anchor": {
                "workflow_id": "approval-wf",
                "required_actions": ["record_decision"],
                "required_evidence": ["decision"],
            },
            "completed_actions": ["record_decision"],
            "evidence": {"decision": "approved"},
            "open_questions": [],
            "require_decision": True,
            "decision_label": "approve",
            "rule_citations": ["FIN-APPROVAL-001"],
        },
        declared_context_uris=refs,
        workflow_id="approval-wf",
    )
