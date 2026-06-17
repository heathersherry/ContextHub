from __future__ import annotations

from dataclasses import dataclass

import pytest

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.contracts import HandoffPacket
from contexthub.enforcement.decision import Verdict, ViolationKind
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.staleness import StalenessResult
from contexthub.models.request import RequestContext

pytestmark = pytest.mark.asyncio


@dataclass
class FakeAccessDecision:
    allowed: bool
    reason: str = "fake"


class FakeACL:
    def __init__(self, denied: set[str] | None = None):
        self.denied = denied or set()
        self.calls: list[tuple[str, str]] = []

    async def check_read_access(self, db, uri: str, ctx: RequestContext):
        self.calls.append((uri, ctx.agent_id))
        return FakeAccessDecision(
            allowed=uri not in self.denied,
            reason="denied by fake" if uri in self.denied else "allowed by fake",
        )


class FakeStaleness:
    def __init__(self, hits: dict[str, StalenessResult] | None = None):
        self.hits = hits or {}
        self.refs_calls: list[list[str]] = []

    async def any_stale_or_blocked_refs(self, db, refs: list[str]):
        self.refs_calls.append(refs)
        return [self.hits[ref] for ref in refs if ref in self.hits]


def _actor() -> RequestContext:
    return RequestContext(account_id="acme", agent_id="sender-agent")


def _recipient() -> RequestContext:
    return RequestContext(account_id="acme", agent_id="recipient-agent")


def _packet(**overrides) -> dict:
    data = HandoffPacket(
        sender="sender-agent",
        recipient="recipient-agent",
        task_intent="resolve ticket escalation",
        required_object_ids=["ctx://team/engineering/runbook"],
        source_artifacts=["ctx://team/engineering/ticket-123"],
        expected_action="diagnose and propose repair",
        context_versions=["ctx://team/policies/onboarding@v1"],
    ).model_dump()
    data.update(overrides)
    return data


_DEFAULT_RECIPIENT = object()


def _ec(
    payload: dict,
    recipient: RequestContext | None | object = _DEFAULT_RECIPIENT,
) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.HANDOFF,
        actor=_actor(),
        recipient=_recipient() if recipient is _DEFAULT_RECIPIENT else recipient,
        payload=payload,
        workflow_id="workflow-1",
    )


def _staleness_result(
    uri: str,
    *,
    status: str = "active",
    is_stale: bool = False,
    is_blocked: bool = False,
    version_mismatch: bool = False,
    current_version: int | None = None,
    expected_version: int | None = None,
) -> StalenessResult:
    return StalenessResult(
        uri=uri,
        status=status,
        is_stale=is_stale,
        is_blocked=is_blocked,
        is_unknown=False,
        version_mismatch=version_mismatch,
        current_version=current_version,
        expected_version=expected_version,
    )


async def test_complete_handoff_allows():
    guardrail = HandoffGuardrail(FakeACL(), FakeStaleness())

    result = await guardrail.check(None, _ec(_packet()))

    assert result.verdict == Verdict.ALLOW
    assert result.violations == []


async def test_missing_required_fields_repairs():
    payload = _packet()
    del payload["required_object_ids"]
    guardrail = HandoffGuardrail(FakeACL(), FakeStaleness())

    result = await guardrail.check(None, _ec(payload))

    assert result.verdict == Verdict.REPAIR
    violation = result.violations[0]
    assert violation.kind == ViolationKind.INCOMPLETE_HANDOFF
    assert "required_object_ids" in violation.repair_hint["missing_fields"]


async def test_recipient_without_object_access_blocks():
    denied_uri = "ctx://team/engineering/runbook"
    guardrail = HandoffGuardrail(FakeACL({denied_uri}), FakeStaleness())

    result = await guardrail.check(None, _ec(_packet(required_object_ids=[denied_uri])))

    assert result.verdict == Verdict.BLOCK
    assert {v.kind for v in result.violations} == {ViolationKind.UNAUTHORIZED_FLOW}


async def test_stale_dependency_repairs():
    ref = "ctx://team/policies/onboarding@v1"
    guardrail = HandoffGuardrail(
        FakeACL(),
        FakeStaleness(
            {
                ref: _staleness_result(
                    "ctx://team/policies/onboarding",
                    status="stale",
                    is_stale=True,
                )
            }
        ),
    )

    result = await guardrail.check(None, _ec(_packet(context_versions=[ref])))

    assert result.verdict == Verdict.REPAIR
    assert {v.kind for v in result.violations} == {ViolationKind.STALE_DEPENDENCY}


async def test_blocked_dependency_repairs():
    ref = "ctx://team/policies/onboarding@v1"
    guardrail = HandoffGuardrail(
        FakeACL(),
        FakeStaleness(
            {
                ref: _staleness_result(
                    "ctx://team/policies/onboarding",
                    status="archived",
                    is_blocked=True,
                )
            }
        ),
    )

    result = await guardrail.check(None, _ec(_packet(context_versions=[ref])))

    assert result.verdict == Verdict.REPAIR
    assert {v.kind for v in result.violations} == {ViolationKind.STALE_DEPENDENCY}


async def test_missing_fields_and_unauthorized_flow_blocks():
    denied_uri = "ctx://team/engineering/runbook"
    payload = _packet(required_object_ids=[denied_uri])
    del payload["expected_action"]
    guardrail = HandoffGuardrail(FakeACL({denied_uri}), FakeStaleness())

    result = await guardrail.check(None, _ec(payload))

    assert result.verdict == Verdict.BLOCK
    assert {v.kind for v in result.violations} == {
        ViolationKind.INCOMPLETE_HANDOFF,
        ViolationKind.UNAUTHORIZED_FLOW,
    }


async def test_recipient_none_skips_readability_check():
    acl = FakeACL({"ctx://team/engineering/runbook"})
    guardrail = HandoffGuardrail(acl, FakeStaleness())

    result = await guardrail.check(None, _ec(_packet(), recipient=None))

    assert result.verdict == Verdict.ALLOW
    assert acl.calls == []
    assert ViolationKind.UNAUTHORIZED_FLOW not in {v.kind for v in result.violations}


async def test_default_version_resolver_passes_runtime_ref_to_staleness():
    staleness = FakeStaleness()
    guardrail = HandoffGuardrail(FakeACL(), staleness)

    result = await guardrail.check(
        None,
        _ec(_packet(context_versions=["tool_schema:itsm@v3"])),
    )

    assert result.verdict == Verdict.ALLOW
    assert staleness.refs_calls == [["ctx://tool_schema/itsm@v3"]]


async def test_applies_to_handoff_boundary():
    assert HandoffGuardrail.applies_to == frozenset({Boundary.HANDOFF})


async def test_payload_shape_not_applicable_noops():
    acl = FakeACL({"ctx://team/engineering/runbook"})
    staleness = FakeStaleness()
    guardrail = HandoffGuardrail(acl, staleness)

    result = await guardrail.check(None, _ec({"items": [{"uri": "ctx://x"}]}))

    assert result.verdict == Verdict.ALLOW
    assert "not applicable" in result.reason
    assert acl.calls == []
    assert staleness.refs_calls == []


async def test_version_mismatch_repairs_with_evidence():
    ref = "ctx://team/policies/onboarding@v3"
    guardrail = HandoffGuardrail(
        FakeACL(),
        FakeStaleness(
            {
                ref: _staleness_result(
                    "ctx://team/policies/onboarding",
                    version_mismatch=True,
                    current_version=2,
                    expected_version=3,
                )
            }
        ),
    )

    result = await guardrail.check(None, _ec(_packet(context_versions=[ref])))

    assert result.verdict == Verdict.REPAIR
    violation = result.violations[0]
    assert violation.kind == ViolationKind.STALE_DEPENDENCY
    assert violation.evidence["version_mismatch"] is True
    assert violation.evidence["expected_version"] == 3
    assert violation.evidence["current_version"] == 2
