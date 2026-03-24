"""Skill API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from contexthub.api.deps import get_db, get_request_context, get_skill_service
from contexthub.db.repository import ScopedRepo
from contexthub.models.request import RequestContext
from contexthub.models.skill import PublishVersionRequest, SubscribeRequest
from contexthub.services.skill_service import SkillService

router = APIRouter(prefix="/api/v1")


@router.post("/skills/versions", status_code=201)
async def publish_version(
    body: PublishVersionRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: SkillService = Depends(get_skill_service),
):
    result = await svc.publish_version(
        db, body.skill_uri, body.content, body.changelog, body.is_breaking, ctx,
    )
    return result.model_dump(mode="json")


@router.get("/skills/{uri:path}/versions")
async def get_versions(
    uri: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: SkillService = Depends(get_skill_service),
):
    versions = await svc.get_versions(db, uri, ctx)
    return [v.model_dump(mode="json") for v in versions]


@router.post("/skills/subscribe")
async def subscribe(
    body: SubscribeRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: SkillService = Depends(get_skill_service),
):
    result = await svc.subscribe(db, body.skill_uri, body.pinned_version, ctx)
    return result.model_dump(mode="json")
