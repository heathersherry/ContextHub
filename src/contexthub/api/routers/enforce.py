"""Runtime enforcement API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from contexthub.api.deps import get_db, get_enforcement_service, get_request_context
from contexthub.db.repository import ScopedRepo
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.service import EnforcementService
from contexthub.models.request import RequestContext

router = APIRouter(prefix="/api/v1", tags=["enforcement"])


class EnforceRequest(BaseModel):
    boundary: Boundary
    payload: dict = Field(default_factory=dict)
    recipient_agent_id: str | None = None
    declared_context_uris: list[str] | None = None
    workflow_id: str | None = None


class ViolationDTO(BaseModel):
    kind: str
    message: str
    repair_hint: dict | None = None
    evidence: dict = Field(default_factory=dict)


class EnforceResponse(BaseModel):
    verdict: str
    reason: str
    guardrail: str
    violations: list[ViolationDTO] = Field(default_factory=list)
    sanitized_payload: dict | None = None


@router.post("/enforce", response_model=EnforceResponse)
async def enforce(
    body: EnforceRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: EnforcementService = Depends(get_enforcement_service),
) -> EnforceResponse:
    recipient = (
        RequestContext(account_id=ctx.account_id, agent_id=body.recipient_agent_id)
        if body.recipient_agent_id
        else None
    )
    ec = EnforcementContext(
        boundary=body.boundary,
        actor=ctx,
        recipient=recipient,
        payload=body.payload,
        declared_context_uris=body.declared_context_uris,
        workflow_id=body.workflow_id,
    )
    decision = await svc.enforce(db, ec)
    return EnforceResponse(
        verdict=decision.verdict.value,
        reason=decision.reason,
        guardrail=decision.guardrail,
        violations=[
            ViolationDTO(
                kind=violation.kind.value,
                message=violation.message,
                repair_hint=violation.repair_hint,
                evidence=violation.evidence,
            )
            for violation in decision.violations
        ],
        sanitized_payload=decision.sanitized_payload,
    )
