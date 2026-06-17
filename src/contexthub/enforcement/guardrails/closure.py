from __future__ import annotations

from contexthub.db.repository import ScopedRepo
from contexthub.enforcement.base import Guardrail
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.contracts import ClosureChecklist, WorkflowAnchor
from contexthub.enforcement.decision import (
    GuardrailDecision,
    Verdict,
    Violation,
    ViolationKind,
)
from contexthub.enforcement.staleness import StalenessChecker


class ClosureGuardrail(Guardrail):
    name = "closure"
    applies_to = frozenset({Boundary.CLOSURE})

    def __init__(
        self,
        staleness: StalenessChecker,
        default_require_decision: bool = False,
    ):
        """payload.require_decision=True enters approval mode; default is a fallback."""
        self._staleness = staleness
        self._default_require_decision = default_require_decision

    async def check(self, db: ScopedRepo, ec: EnforcementContext) -> GuardrailDecision:
        if not _looks_like_closure_payload(ec.payload):
            return GuardrailDecision(
                verdict=Verdict.ALLOW,
                reason="payload shape not applicable",
                guardrail=self.name,
            )

        payload = ec.payload or {}
        checklist = _rebuild_checklist(payload)
        anchor = checklist.anchor
        require_decision = _requires_decision(payload, self._default_require_decision)
        violations: list[Violation] = []

        done = set(checklist.completed_actions)
        missing_actions = [a for a in anchor.required_actions if a not in done]
        if missing_actions:
            violations.append(
                Violation(
                    kind=ViolationKind.UNCLOSED_WORKFLOW,
                    message=f"required actions not completed: {missing_actions}",
                    repair_hint={"missing_actions": missing_actions},
                    evidence={"workflow_id": anchor.workflow_id},
                )
            )

        missing_evidence = [
            k for k in anchor.required_evidence if not checklist.evidence.get(k)
        ]
        if missing_evidence:
            violations.append(
                Violation(
                    kind=ViolationKind.UNCLOSED_WORKFLOW,
                    message=f"commitments missing evidence: {missing_evidence}",
                    repair_hint={"missing_evidence": missing_evidence},
                    evidence={"workflow_id": anchor.workflow_id},
                )
            )

        if checklist.open_questions:
            violations.append(
                Violation(
                    kind=ViolationKind.UNCLOSED_WORKFLOW,
                    message=f"open questions remain: {checklist.open_questions}",
                    repair_hint={"open_questions": checklist.open_questions},
                    evidence={"workflow_id": anchor.workflow_id},
                )
            )

        if require_decision:
            if not checklist.decision_label or not checklist.rule_citations:
                violations.append(
                    Violation(
                        kind=ViolationKind.WEAK_DECISION,
                        message="approval decision missing label or rule citations",
                        repair_hint={
                            "has_label": bool(checklist.decision_label),
                            "has_citations": bool(checklist.rule_citations),
                        },
                        evidence={"workflow_id": anchor.workflow_id},
                    )
                )

        stale_hits = await self._staleness.any_stale_or_blocked_refs(
            db,
            ec.declared_context_uris or [],
        )
        for result in stale_hits:
            violations.append(
                Violation(
                    kind=ViolationKind.STALE_DEPENDENCY,
                    message=(
                        "closing with stale/blocked/version-mismatched dependency "
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


def _looks_like_closure_payload(payload: dict | None) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("anchor"), dict)


def _rebuild_checklist(payload: dict) -> ClosureChecklist:
    anchor_payload = payload.get("anchor", {})
    anchor = WorkflowAnchor(
        workflow_id=anchor_payload.get("workflow_id", ""),
        required_actions=list(anchor_payload.get("required_actions", [])),
        required_evidence=list(anchor_payload.get("required_evidence", [])),
    )
    return ClosureChecklist(
        anchor=anchor,
        completed_actions=list(payload.get("completed_actions", [])),
        evidence=dict(payload.get("evidence", {})),
        open_questions=list(payload.get("open_questions", [])),
        require_decision=bool(payload.get("require_decision", False)),
        decision_label=payload.get("decision_label"),
        rule_citations=payload.get("rule_citations"),
    )


def _requires_decision(payload: dict, default_require_decision: bool) -> bool:
    if "require_decision" in payload:
        return bool(payload["require_decision"])
    return default_require_decision


def _decide(violations: list[Violation]) -> GuardrailDecision:
    if not violations:
        return GuardrailDecision(
            verdict=Verdict.ALLOW,
            reason="workflow closed",
            guardrail=ClosureGuardrail.name,
        )

    kinds = {v.kind for v in violations}
    return GuardrailDecision(
        verdict=Verdict.BLOCK,
        violations=violations,
        reason=f"closure guardrail: {sorted(k.value for k in kinds)}",
        guardrail=ClosureGuardrail.name,
    )
