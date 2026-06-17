from __future__ import annotations

from contexthub.db.repository import ScopedRepo
from contexthub.enforcement.base import Guardrail
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import (
    GuardrailDecision,
    Verdict,
    Violation,
    ViolationKind,
)
from contexthub.services.acl_service import ACLService

_MASK_TOKEN = "[REDACTED]"


class FlowGuardrail(Guardrail):
    name = "flow"
    applies_to = frozenset(
        {
            Boundary.HANDOFF,
            Boundary.SHARED_MEMORY_WRITE,
            Boundary.TOOL_CALL,
            Boundary.INVOCATION,
        }
    )

    def __init__(self, acl: ACLService):
        self._acl = acl

    async def check(self, db: ScopedRepo, ec: EnforcementContext) -> GuardrailDecision:
        if not _looks_like_flow_payload(ec.payload):
            return GuardrailDecision(
                verdict=Verdict.ALLOW,
                reason="payload shape not applicable",
                guardrail=self.name,
            )

        recipient = ec.recipient or ec.actor
        violations: list[Violation] = []
        sanitized_items: list[dict] = []

        for item in ec.payload["items"]:
            uri = item["uri"]
            fields = dict(item["fields"])
            decision = await self._acl.check_read_access(db, uri, recipient)

            if not decision.allowed:
                violations.append(
                    Violation(
                        kind=ViolationKind.UNAUTHORIZED_FLOW,
                        message=f"recipient {recipient.agent_id} cannot receive {uri}",
                        evidence={
                            "uri": uri,
                            "reason": decision.reason,
                            "dropped": True,
                        },
                    )
                )
                continue

            masked_fields = set(decision.field_masks or [])
            redacted = sorted(k for k in fields if k in masked_fields)
            for key in redacted:
                fields[key] = _MASK_TOKEN

            if redacted:
                violations.append(
                    Violation(
                        kind=ViolationKind.UNAUTHORIZED_FLOW,
                        message=f"masked sensitive fields {redacted} for {uri}",
                        evidence={
                            "uri": uri,
                            "masked": redacted,
                            "dropped": False,
                        },
                    )
                )

            sanitized_items.append({"uri": uri, "fields": fields})

        return _decide(violations, {"items": sanitized_items})


def _looks_like_flow_payload(payload: dict | None) -> bool:
    """Avoid colliding with other guardrails sharing the same boundary."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("uri"), str)
        and isinstance(item.get("fields"), dict)
        for item in payload["items"]
    )


def _decide(
    violations: list[Violation],
    sanitized_payload: dict,
) -> GuardrailDecision:
    if not violations:
        return GuardrailDecision(
            verdict=Verdict.ALLOW,
            reason="flow ok",
            guardrail=FlowGuardrail.name,
            sanitized_payload=sanitized_payload,
        )

    unauthorized = [v for v in violations if v.evidence.get("dropped") is True]
    verdict = Verdict.BLOCK if unauthorized else Verdict.REPAIR
    return GuardrailDecision(
        verdict=verdict,
        violations=violations,
        reason="flow guardrail: minimal disclosure enforced",
        guardrail=FlowGuardrail.name,
        sanitized_payload=sanitized_payload,
    )


__all__ = ["FlowGuardrail"]
