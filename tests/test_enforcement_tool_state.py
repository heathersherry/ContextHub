from __future__ import annotations

import pytest

from contexthub.enforcement import Boundary, EnforcementContext, Verdict, ViolationKind
from contexthub.enforcement.guardrails.tool_state import ToolStateGuardrail
from contexthub.enforcement.staleness import StalenessResult
from contexthub.models.request import RequestContext


pytestmark = pytest.mark.asyncio


class FakeStaleness:
    def __init__(self, hits: list[StalenessResult] | None = None):
        self.hits = hits or []
        self.refs: list[str] | None = None

    async def any_stale_or_blocked_refs(self, db, refs: list[str]):
        self.refs = refs
        return self.hits


class RaisingStaleness:
    async def any_stale_or_blocked_refs(self, db, refs: list[str]):
        raise AssertionError("staleness should not be checked")


def _ec(payload: dict) -> EnforcementContext:
    return EnforcementContext(
        boundary=Boundary.TOOL_CALL,
        actor=RequestContext(account_id="acme", agent_id="agent-a"),
        payload=payload,
    )


def _payload(
    *,
    args: dict | None = None,
    required_role: str | None = "incident_writer",
    schema: dict | None = None,
    provenance_bound_args: list[str] | None = None,
    mutation_intent: str = "update",
    depends_on_uris: list[str] | None = None,
) -> dict:
    return {
        "contract": {
            "tool_name": "update_incident",
            "required_role": required_role,
            "arg_schema": schema
            if schema is not None
            else {
                "required": ["object_id", "state"],
                "properties": {
                    "object_id": {"type": "string"},
                    "state": {"enum": ["open", "resolved"]},
                    "priority": {"type": "integer"},
                },
            },
            "provenance_bound_args": provenance_bound_args or ["object_id"],
            "mutation_intent": mutation_intent,
            "depends_on_uris": depends_on_uris or [],
        },
        "tool_args": args
        if args is not None
        else {"object_id": "inc-1", "state": "resolved", "priority": 2},
    }


async def _role_ok(agent_id: str, required_role: str) -> bool:
    return True


async def _role_denied(agent_id: str, required_role: str) -> bool:
    return False


async def _exists(object_id: str) -> bool:
    return True


async def _missing(object_id: str) -> bool:
    return False


async def _trusted(arg_name: str, value: str) -> bool:
    return True


async def _untrusted(arg_name: str, value: str) -> bool:
    return False


def _kinds(decision):
    return {v.kind for v in decision.violations}


async def test_valid_call_allows():
    guardrail = ToolStateGuardrail(
        FakeStaleness(),
        role_checker=_role_ok,
        object_exists=_exists,
        provenance_check=_trusted,
    )

    decision = await guardrail.check(None, _ec(_payload()))

    assert decision.verdict == Verdict.ALLOW


async def test_role_denied_blocks():
    guardrail = ToolStateGuardrail(FakeStaleness(), role_checker=_role_denied)

    decision = await guardrail.check(None, _ec(_payload()))

    assert decision.verdict == Verdict.BLOCK
    assert ViolationKind.UNAUTHORIZED_FLOW in _kinds(decision)


async def test_missing_required_arg_repairs():
    guardrail = ToolStateGuardrail(FakeStaleness())

    decision = await guardrail.check(None, _ec(_payload(args={"object_id": "inc-1"})))

    assert decision.verdict == Verdict.REPAIR
    assert ViolationKind.SCHEMA_OR_ENUM in _kinds(decision)


async def test_enum_out_of_range_repairs_with_allowed_hint():
    guardrail = ToolStateGuardrail(FakeStaleness())

    decision = await guardrail.check(
        None,
        _ec(_payload(args={"object_id": "inc-1", "state": "closed"})),
    )

    assert decision.verdict == Verdict.REPAIR
    violation = decision.violations[0]
    assert violation.kind == ViolationKind.SCHEMA_OR_ENUM
    assert violation.repair_hint == {
        "arg": "state",
        "allowed": ["open", "resolved"],
        "got": "closed",
    }


async def test_type_mismatch_repairs():
    guardrail = ToolStateGuardrail(FakeStaleness())

    decision = await guardrail.check(
        None,
        _ec(_payload(args={"object_id": "inc-1", "state": "open", "priority": "high"})),
    )

    assert decision.verdict == Verdict.REPAIR
    assert ViolationKind.SCHEMA_OR_ENUM in _kinds(decision)


async def test_untrusted_provenance_blocks():
    guardrail = ToolStateGuardrail(
        FakeStaleness(),
        provenance_check=_untrusted,
    )

    decision = await guardrail.check(None, _ec(_payload()))

    assert decision.verdict == Verdict.BLOCK
    assert ViolationKind.UNTRUSTED_PROVENANCE in _kinds(decision)


async def test_create_instead_of_update_blocks():
    guardrail = ToolStateGuardrail(FakeStaleness(), object_exists=_missing)

    decision = await guardrail.check(None, _ec(_payload()))

    assert decision.verdict == Verdict.BLOCK
    assert ViolationKind.WRONG_OBJECT_MUTATION in _kinds(decision)


async def test_update_existing_target_does_not_report_wrong_object():
    guardrail = ToolStateGuardrail(FakeStaleness(), object_exists=_exists)

    decision = await guardrail.check(None, _ec(_payload()))

    assert ViolationKind.WRONG_OBJECT_MUTATION not in _kinds(decision)


async def test_stale_dependency_repairs():
    guardrail = ToolStateGuardrail(
        FakeStaleness(
            [
                StalenessResult(
                    uri="ctx://team/policies/tooling",
                    status="stale",
                    is_stale=True,
                    version_mismatch=False,
                    is_blocked=False,
                    is_unknown=False,
                )
            ]
        )
    )

    decision = await guardrail.check(
        None,
        _ec(_payload(depends_on_uris=["ctx://team/policies/tooling"])),
    )

    assert decision.verdict == Verdict.REPAIR
    assert ViolationKind.STALE_DEPENDENCY in _kinds(decision)


async def test_none_callbacks_do_not_report_role_provenance_or_object():
    guardrail = ToolStateGuardrail(FakeStaleness())

    decision = await guardrail.check(None, _ec(_payload()))

    assert decision.verdict == Verdict.ALLOW
    assert ViolationKind.UNAUTHORIZED_FLOW not in _kinds(decision)
    assert ViolationKind.UNTRUSTED_PROVENANCE not in _kinds(decision)
    assert ViolationKind.WRONG_OBJECT_MUTATION not in _kinds(decision)


async def test_applies_to_tool_call_and_state_mutation():
    assert ToolStateGuardrail.applies_to == frozenset(
        {Boundary.TOOL_CALL, Boundary.STATE_MUTATION}
    )


async def test_payload_shape_not_applicable_allows_without_staleness():
    guardrail = ToolStateGuardrail(RaisingStaleness())

    decision = await guardrail.check(None, _ec({"items": []}))

    assert decision.verdict == Verdict.ALLOW
    assert "not applicable" in decision.reason


async def test_version_mismatch_dependency_repairs_with_evidence():
    guardrail = ToolStateGuardrail(
        FakeStaleness(
            [
                StalenessResult(
                    uri="ctx://team/policies/tooling",
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
    )

    decision = await guardrail.check(
        None,
        _ec(_payload(depends_on_uris=["ctx://team/policies/tooling@v3"])),
    )

    assert decision.verdict == Verdict.REPAIR
    assert decision.violations[0].kind == ViolationKind.STALE_DEPENDENCY
    assert decision.violations[0].evidence["version_mismatch"] is True
