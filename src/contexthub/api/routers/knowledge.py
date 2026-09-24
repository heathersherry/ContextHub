"""Thin public knowledge routes. No request-scoped long DB transaction."""

from uuid import UUID
from fastapi import APIRouter, Depends
from contexthub.api.deps import get_request_context, get_knowledge_service
from contexthub.models.knowledge_public import (
    KnowledgeIngestRequest,
    KnowledgeMaintainRequest,
    KnowledgeResult,
)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


@router.post("/ingest", response_model=KnowledgeResult)
async def ingest(
    body: KnowledgeIngestRequest,
    ctx=Depends(get_request_context),
    svc=Depends(get_knowledge_service),
):
    return await svc.ingest(body, ctx)


@router.post("/maintain", response_model=KnowledgeResult)
async def maintain(
    body: KnowledgeMaintainRequest,
    ctx=Depends(get_request_context),
    svc=Depends(get_knowledge_service),
):
    return await svc.maintain(body, ctx)


@router.get("/operations/{operation_id}", response_model=KnowledgeResult)
async def result(
    operation_id: UUID,
    ctx=Depends(get_request_context),
    svc=Depends(get_knowledge_service),
):
    return await svc.get_maintenance_result(operation_id, ctx)
