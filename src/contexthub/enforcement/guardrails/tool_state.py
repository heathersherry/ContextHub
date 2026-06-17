from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from contexthub.db.repository import ScopedRepo
from contexthub.enforcement.base import Guardrail
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.contracts import ToolCallContract
from contexthub.enforcement.decision import (
    GuardrailDecision,
    Verdict,
    Violation,
    ViolationKind,
)
from contexthub.enforcement.staleness import StalenessChecker

RoleChecker = Callable[[str, str], Awaitable[bool]]
ObjectExists = Callable[[str], Awaitable[bool]]
ProvenanceCheck = Callable[[str, str], Awaitable[bool]]


class ToolStateGuardrail(Guardrail):
    name = "tool_state"
    applies_to = frozenset({Boundary.TOOL_CALL, Boundary.STATE_MUTATION})

    def __init__(
        self,
        staleness: StalenessChecker,
        role_checker: RoleChecker | None = None,
        object_exists: ObjectExists | None = None,
        provenance_check: ProvenanceCheck | None = None,
    ):
        self._staleness = staleness
        self._role_ok = role_checker
        self._object_exists = object_exists
        self._provenance_ok = provenance_check

    async def check(self, db: ScopedRepo, ec: EnforcementContext) -> GuardrailDecision:
        if not _looks_like_tool_state_payload(ec.payload):
            return GuardrailDecision(
                verdict=Verdict.ALLOW,
                reason="payload shape not applicable",
                guardrail=self.name,
            )

        contract = ToolCallContract(**ec.payload["contract"])
        args: dict = ec.payload.get("tool_args", {})
        violations: list[Violation] = []

        if contract.required_role and self._role_ok is not None:
            ok = await self._role_ok(ec.actor.agent_id, contract.required_role)
            if not ok:
                violations.append(
                    Violation(
                        kind=ViolationKind.UNAUTHORIZED_FLOW,
                        message=(
                            f"{ec.actor.agent_id} lacks role "
                            f"{contract.required_role} for {contract.tool_name}"
                        ),
                        evidence={
                            "tool": contract.tool_name,
                            "required_role": contract.required_role,
                        },
                    )
                )

        violations.extend(_validate_args(contract.arg_schema, args, contract.tool_name))

        if self._provenance_ok is not None:
            for arg_name in contract.provenance_bound_args:
                if arg_name in args:
                    ok = await self._provenance_ok(arg_name, str(args[arg_name]))
                    if not ok:
                        violations.append(
                            Violation(
                                kind=ViolationKind.UNTRUSTED_PROVENANCE,
                                message=(
                                    f"arg {arg_name} value not from trusted provenance"
                                ),
                                repair_hint={"arg": arg_name},
                                evidence={
                                    "tool": contract.tool_name,
                                    "arg": arg_name,
                                },
                            )
                        )

        if contract.mutation_intent == "update" and self._object_exists is not None:
            target = args.get("object_id") or args.get("id")
            if target is not None:
                exists = await self._object_exists(str(target))
                if not exists:
                    violations.append(
                        Violation(
                            kind=ViolationKind.WRONG_OBJECT_MUTATION,
                            message=(
                                "update intent but target object "
                                f"{target} does not exist (create-instead-of-update)"
                            ),
                            repair_hint={"object_id": target},
                            evidence={
                                "tool": contract.tool_name,
                                "object_id": target,
                            },
                        )
                    )

        stale_hits = await self._staleness.any_stale_or_blocked_refs(
            db,
            contract.depends_on_uris,
        )
        for result in stale_hits:
            violations.append(
                Violation(
                    kind=ViolationKind.STALE_DEPENDENCY,
                    message=(
                        "tool call depends on stale/blocked/version-mismatched "
                        f"{result.uri} (status={result.status})"
                    ),
                    repair_hint={
                        "uri": result.uri,
                        "status": result.status,
                        "expected_version": result.expected_version,
                        "current_version": result.current_version,
                    },
                    evidence={
                        "uri": result.uri,
                        "status": result.status,
                        "expected_version": result.expected_version,
                        "current_version": result.current_version,
                        "version_mismatch": result.version_mismatch,
                    },
                )
            )

        return _decide(violations)


def _looks_like_tool_state_payload(payload: dict | None) -> bool:
    """Avoid misclassifying other TOOL_CALL guardrail payloads."""
    return isinstance(payload, dict) and "contract" in payload and "tool_args" in payload


def _validate_args(schema: dict, args: dict, tool: str) -> list[Violation]:
    violations: list[Violation] = []

    for req in schema.get("required", []):
        if req not in args or args[req] in (None, "", []):
            violations.append(
                Violation(
                    kind=ViolationKind.SCHEMA_OR_ENUM,
                    message=f"missing required arg '{req}' for {tool}",
                    repair_hint={"missing_arg": req},
                    evidence={"tool": tool, "arg": req},
                )
            )

    for name, spec in schema.get("properties", {}).items():
        if name not in args or args[name] is None:
            continue

        val = args[name]
        if "enum" in spec and val not in spec["enum"]:
            violations.append(
                Violation(
                    kind=ViolationKind.SCHEMA_OR_ENUM,
                    message=f"arg '{name}'={val!r} not in enum {spec['enum']}",
                    repair_hint={"arg": name, "allowed": spec["enum"], "got": val},
                    evidence={"tool": tool, "arg": name},
                )
            )

        expected_type = spec.get("type")
        if expected_type and not _type_ok(expected_type, val):
            violations.append(
                Violation(
                    kind=ViolationKind.SCHEMA_OR_ENUM,
                    message=(
                        f"arg '{name}' expected type {expected_type}, "
                        f"got {type(val).__name__}"
                    ),
                    repair_hint={"arg": name, "expected_type": expected_type},
                    evidence={"tool": tool, "arg": name},
                )
            )

    return violations


def _type_ok(expected_type: str, val: Any) -> bool:
    return {
        "string": isinstance(val, str),
        "integer": isinstance(val, int) and not isinstance(val, bool),
        "number": isinstance(val, (int, float)) and not isinstance(val, bool),
        "boolean": isinstance(val, bool),
        "array": isinstance(val, list),
        "object": isinstance(val, dict),
    }.get(expected_type, True)


def _decide(violations: list[Violation]) -> GuardrailDecision:
    if not violations:
        return GuardrailDecision(
            verdict=Verdict.ALLOW,
            reason="tool call valid",
            guardrail=ToolStateGuardrail.name,
        )

    kinds = {v.kind for v in violations}
    fail_closed = {
        ViolationKind.UNAUTHORIZED_FLOW,
        ViolationKind.WRONG_OBJECT_MUTATION,
        ViolationKind.UNTRUSTED_PROVENANCE,
    }
    verdict = Verdict.BLOCK if kinds & fail_closed else Verdict.REPAIR

    return GuardrailDecision(
        verdict=verdict,
        violations=violations,
        reason=f"tool_state guardrail: {sorted(k.value for k in kinds)}",
        guardrail=ToolStateGuardrail.name,
    )
