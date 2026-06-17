"""EntCollabBench execution-boundary hooks for ContextHub enforcement."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from contexthub.db.repository import PgRepository
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import GuardrailDecision, Verdict
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.repair import RepairPlan, RepairStrategy, plan_repair
from contexthub.enforcement.service import EnforcementService
from contexthub.enforcement.staleness import StalenessChecker
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService

from integrations.entcollabbench import mapping
from integrations.entcollabbench.world_loader import LoadedWorld


@dataclass(frozen=True)
class EnforcementAction:
    """How the EntCollabBench loop should handle an enforcement decision."""

    action: str
    allow: bool = False
    retry: bool = False
    pending: bool = False
    patch: dict | None = None
    feedback: dict | None = None
    repair_plan: RepairPlan | None = None
    decision: GuardrailDecision | None = None


class EnforcementInterceptor:
    """在 EntCollabBench agent 循环的执行边界挂钩。"""

    def __init__(
        self,
        repo: PgRepository,
        account_id: str,
        loaded: LoadedWorld,
        guardrails: list | None = None,
        audit=None,
        service: EnforcementService | None = None,
        repair_planner: Callable = plan_repair,
    ):
        self._repo = repo
        self._account_id = account_id
        self._loaded = loaded
        self._repair_planner = repair_planner
        self._svc = service or EnforcementService(
            guardrails if guardrails is not None else self._default_guardrails(),
            audit=audit,
        )

    async def on_handoff(self, sender, recipient, packet_dict) -> GuardrailDecision:
        ec = EnforcementContext(
            boundary=Boundary.HANDOFF,
            actor=RequestContext(self._account_id, sender),
            recipient=RequestContext(self._account_id, recipient),
            payload=packet_dict,
            declared_context_uris=list(packet_dict.get("context_versions") or []),
        )
        return await self._enforce(ec)

    async def on_tool_call(self, agent_id, contract_dict, tool_args) -> GuardrailDecision:
        ec = EnforcementContext(
            boundary=Boundary.TOOL_CALL,
            actor=RequestContext(self._account_id, agent_id),
            payload={"contract": contract_dict, "tool_args": tool_args},
            declared_context_uris=list(contract_dict.get("depends_on_uris") or []),
        )
        return await self._enforce(ec)

    async def on_closure(
        self,
        agent_id,
        checklist_dict,
        declared_uris,
        workflow_id,
    ) -> GuardrailDecision:
        ec = EnforcementContext(
            boundary=Boundary.CLOSURE,
            actor=RequestContext(self._account_id, agent_id),
            payload=checklist_dict,
            declared_context_uris=list(declared_uris or []),
            workflow_id=workflow_id,
        )
        return await self._enforce(ec)

    def apply(self, decision: GuardrailDecision) -> EnforcementAction:
        if decision.verdict == Verdict.ALLOW:
            return EnforcementAction(action="allow", allow=True, decision=decision)

        if decision.verdict == Verdict.BLOCK:
            return EnforcementAction(action="block", decision=decision)

        if decision.verdict == Verdict.ESCALATE:
            return EnforcementAction(action="pending", pending=True, decision=decision)

        plan = self._repair_planner(decision.violations)
        if plan.strategy == RepairStrategy.DETERMINISTIC:
            return EnforcementAction(
                action="retry_with_patch",
                retry=True,
                patch=plan.patch,
                repair_plan=plan,
                decision=decision,
            )
        if plan.strategy == RepairStrategy.ONE_SHOT_MODEL:
            return EnforcementAction(
                action="retry_with_feedback",
                retry=True,
                feedback={"violations": decision.violations},
                repair_plan=plan,
                decision=decision,
            )
        if plan.strategy == RepairStrategy.ESCALATE:
            return EnforcementAction(
                action="pending",
                pending=True,
                repair_plan=plan,
                decision=decision,
            )
        return EnforcementAction(
            action="block",
            repair_plan=plan,
            decision=decision,
        )

    async def _enforce(self, ec: EnforcementContext) -> GuardrailDecision:
        async with self._repo.session(self._account_id) as db:
            return await self._svc.enforce(db, ec)

    def _default_guardrails(self) -> list:
        staleness = StalenessChecker()
        return [
            HandoffGuardrail(
                ACLService(),
                staleness,
                object_uri_resolver=self._loaded.object_uri,
                version_uri_resolver=mapping.resolve_version_tag,
            ),
            ClosureGuardrail(staleness),
        ]


def build_approval_checklist(
    *,
    workflow_id: str,
    completed_actions: list[str] | None = None,
    evidence: dict[str, str] | None = None,
    open_questions: list[str] | None = None,
    required_actions: list[str] | None = None,
    required_evidence: list[str] | None = None,
    decision_label: str | None = None,
    rule_citations: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict:
    """Build a ClosureGuardrail payload for approval tasks.

    Approval mode is selected by payload ``require_decision=True``; callers do
    not need a separate ClosureGuardrail instance for approval workflows.
    """

    payload = {
        "anchor": {
            "workflow_id": workflow_id,
            "required_actions": list(required_actions or []),
            "required_evidence": list(required_evidence or []),
        },
        "completed_actions": list(completed_actions or []),
        "evidence": dict(evidence or {}),
        "open_questions": list(open_questions or []),
        "require_decision": True,
        "decision_label": decision_label,
        "rule_citations": list(rule_citations) if rule_citations is not None else None,
    }
    if extra:
        payload.update(extra)
        payload["require_decision"] = True
    return payload
