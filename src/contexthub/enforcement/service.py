from __future__ import annotations

from contexthub.db.repository import ScopedRepo
from contexthub.enforcement.base import Guardrail
from contexthub.enforcement.context import EnforcementContext
from contexthub.enforcement.decision import GuardrailDecision, Verdict
from contexthub.services.audit_service import AuditService


_PRIORITY = {
    Verdict.BLOCK: 3,
    Verdict.ESCALATE: 2,
    Verdict.REPAIR: 1,
    Verdict.ALLOW: 0,
}


class EnforcementService:
    def __init__(self, guardrails: list[Guardrail], audit: AuditService | None = None):
        self._guardrails = guardrails
        self._audit = audit

    async def enforce(self, db: ScopedRepo, ec: EnforcementContext) -> GuardrailDecision:
        active = [g for g in self._guardrails if ec.boundary in g.applies_to]
        decisions = [await g.check(db, ec) for g in active]
        merged = self._merge(decisions)
        await self._record(db, ec, merged)
        return merged

    @staticmethod
    def _merge(decisions: list[GuardrailDecision]) -> GuardrailDecision:
        if not decisions:
            return GuardrailDecision(
                verdict=Verdict.ALLOW,
                reason="no guardrail applied",
            )

        worst = max(decisions, key=lambda d: _PRIORITY[d.verdict])
        violations = [v for d in decisions for v in d.violations]
        return GuardrailDecision(
            verdict=worst.verdict,
            violations=violations,
            reason=worst.reason,
            sanitized_payload=worst.sanitized_payload,
            guardrail="+".join(sorted({d.guardrail for d in decisions if d.violations})),
        )

    async def _record(
        self,
        db: ScopedRepo,
        ec: EnforcementContext,
        merged: GuardrailDecision,
    ) -> None:
        if self._audit is None:
            return

        await self._audit.log_strict(
            db,
            actor=ec.actor.agent_id,
            action="enforcement",
            resource_uri=ec.workflow_id,
            result="success",
            context_used=ec.declared_context_uris,
            metadata={
                "verdict": merged.verdict.value,
                "boundary": ec.boundary.value,
                "guardrail": merged.guardrail,
                "violations": [
                    {
                        "kind": v.kind.value,
                        "message": v.message,
                        "evidence": v.evidence,
                    }
                    for v in merged.violations
                ],
                "recipient": ec.recipient.agent_id if ec.recipient else None,
            },
        )
