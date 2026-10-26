"""Memory API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from contexthub.api.deps import (
    get_db,
    get_memory_service,
    get_request_context,
    get_memory_engine,
)
from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError
from contexthub.memory.context import ContextBudgetError
from contexthub.memory.public import (
    MemoryIngestRequest,
    MemoryContextRequest,
    MemoryIngestResult,
    MemoryContextResult,
)
from contexthub.memory.schemas import MemoryScope
from contexthub.models.memory import AddMemoryRequest, PromoteRequest
from contexthub.models.request import RequestContext
from contexthub.services.memory_service import MemoryService

router = APIRouter(prefix="/api/v1")


@router.post("/memories", status_code=201)
async def add_memory(
    body: AddMemoryRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: MemoryService = Depends(get_memory_service),
):
    result = await svc.add_memory(db, body, ctx)
    return result.model_dump(mode="json")


@router.get("/memories")
async def list_memories(
    include_stale: bool = Query(False),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: MemoryService = Depends(get_memory_service),
):
    return await svc.list_memories(db, ctx, include_stale=include_stale)


@router.post("/memories/promote", status_code=201)
async def promote_memory(
    body: PromoteRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: MemoryService = Depends(get_memory_service),
):
    result = await svc.promote(db, body, ctx)
    return result.model_dump(mode="json")


# Managed memory owns its DB transactions; no get_db transaction spans model calls.
@router.post("/memories/ingest", response_model=MemoryIngestResult)
async def ingest_memory(
    body: MemoryIngestRequest,
    ctx: RequestContext = Depends(get_request_context),
    engine=Depends(get_memory_engine),
):
    scope = MemoryScope(
        tenant_id=ctx.account_id, owner_id=ctx.agent_id, namespace=body.namespace
    )
    return await engine.ingest(
        scope,
        body.sequence,
        [m.model_dump(exclude_none=True) for m in body.messages],
        timestamp=body.timestamp,
    )


@router.post("/memories/context", response_model=MemoryContextResult)
async def memory_context(
    body: MemoryContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    engine=Depends(get_memory_engine),
):
    scope = MemoryScope(
        tenant_id=ctx.account_id, owner_id=ctx.agent_id, namespace=body.namespace
    )
    try:
        return await engine.context(
            scope, body.query, mode=body.mode, max_chars=body.max_chars
        )
    except ContextBudgetError as exc:
        raise BadRequestError(str(exc)) from exc
