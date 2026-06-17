from __future__ import annotations

from collections.abc import Callable

from contexthub.db.repository import ScopedRepo
from contexthub.enforcement.base import Guardrail
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.contracts import HandoffPacket
from contexthub.enforcement.decision import (
    GuardrailDecision,
    Verdict,
    Violation,
    ViolationKind,
)
from contexthub.enforcement.staleness import StalenessChecker
from contexthub.services.acl_service import ACLService

_REQUIRED_FIELDS = {
    "sender",
    "recipient",
    "task_intent",
    "expected_action",
    "required_object_ids",
}
_HANDOFF_KEYS = {
    "sender",
    "recipient",
    "task_intent",
    "expected_action",
    "required_object_ids",
}


class HandoffGuardrail(Guardrail):
    name = "handoff"
    applies_to = frozenset({Boundary.HANDOFF})

    def __init__(
        self,
        acl: ACLService,
        staleness: StalenessChecker,
        object_uri_resolver: Callable[[str], str] | None = None,
        version_uri_resolver: Callable[[str], str] | None = None,
    ):
        self._acl = acl
        self._staleness = staleness
        self._resolve_obj = object_uri_resolver or (lambda value: value)
        self._resolve_ver = version_uri_resolver or _default_version_resolver

    async def check(
        self,
        db: ScopedRepo,
        ec: EnforcementContext,
    ) -> GuardrailDecision:
        if not _looks_like_handoff_payload(ec.payload):
            return GuardrailDecision(
                verdict=Verdict.ALLOW,
                reason="payload shape not applicable",
                guardrail=self.name,
            )

        packet = HandoffPacket(**ec.payload)
        recipient = ec.recipient
        violations: list[Violation] = []

        missing = packet.static_missing(_REQUIRED_FIELDS)
        if missing:
            violations.append(
                Violation(
                    kind=ViolationKind.INCOMPLETE_HANDOFF,
                    message=f"missing required handoff fields: {missing}",
                    repair_hint={"missing_fields": missing},
                    evidence={
                        "sender": packet.sender,
                        "recipient": packet.recipient,
                    },
                )
            )

        if recipient is not None:
            for obj_id in packet.required_object_ids:
                uri = self._resolve_obj(obj_id)
                decision = await self._acl.check_read_access(db, uri, recipient)
                if not decision.allowed:
                    violations.append(
                        Violation(
                            kind=ViolationKind.UNAUTHORIZED_FLOW,
                            message=f"recipient {recipient.agent_id} cannot read {uri}",
                            repair_hint={"object_id": obj_id, "uri": uri},
                            evidence={"uri": uri, "reason": decision.reason},
                        )
                    )

        refs = [self._resolve_ver(version) for version in packet.context_versions]
        stale_hits = await self._staleness.any_stale_or_blocked_refs(db, refs)
        for result in stale_hits:
            violations.append(
                Violation(
                    kind=ViolationKind.STALE_DEPENDENCY,
                    message=(
                        "handoff depends on stale/blocked/version-mismatched "
                        f"context {result.uri} (status={result.status})"
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


def _looks_like_handoff_payload(payload: dict | None) -> bool:
    """No-op on other HANDOFF payload contracts that share the same boundary."""
    if not isinstance(payload, dict):
        return False
    if "items" in payload and not any(key in payload for key in _HANDOFF_KEYS):
        return False
    return any(key in payload for key in _HANDOFF_KEYS)


def _default_version_resolver(version_tag: str) -> str:
    if version_tag.startswith("ctx://"):
        return version_tag
    tag, suffix = (version_tag.split("@", 1) + [""])[:2]
    version_suffix = f"@{suffix}" if suffix else ""
    if ":" in tag:
        kind, name = tag.split(":", 1)
        return f"ctx://{kind}/{name}{version_suffix}"
    return version_tag


def _decide(violations: list[Violation]) -> GuardrailDecision:
    if not violations:
        return GuardrailDecision(
            verdict=Verdict.ALLOW,
            reason="handoff complete",
            guardrail="handoff",
        )

    kinds = {violation.kind for violation in violations}
    if ViolationKind.UNAUTHORIZED_FLOW in kinds:
        verdict = Verdict.BLOCK
    else:
        verdict = Verdict.REPAIR

    return GuardrailDecision(
        verdict=verdict,
        violations=violations,
        reason=f"handoff guardrail: {sorted(kind.value for kind in kinds)}",
        guardrail="handoff",
    )
