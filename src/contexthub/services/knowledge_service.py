"""Public orchestration over S3–S6; no alternative discovery or maintenance rules."""

import asyncio
import logging
from time import monotonic

import anyio
from contextvars import ContextVar
from contextlib import asynccontextmanager
from functools import wraps
from contexthub.discovery._work import LocalOperation, MeteredDB
from uuid import uuid4, uuid5, NAMESPACE_URL

from contexthub.errors import ForbiddenError, NotFoundError, ConflictError
from contexthub.models.context import Scope, CreateContextRequest, UpdateContextRequest
from contexthub.models.request import RequestContext
from contexthub.models.execution import CallIdentity
from contexthub.models.knowledge import (
    ContextVersion,
    ContractError,
    canonical_hash,
    SealRef,
)
from contexthub.models.knowledge_public import (
    PublicOperation,
    PublicCompletion,
    KnowledgeResult,
    KnowledgeNode,
    KnowledgeWork,
)
from contexthub.discovery.search import (
    HistorySnapshot,
    HistoryEntry,
    ExplicitProvenance,
)
from contexthub.services.evidence_service import EvidenceService
from contexthub.propagation.executor import trace


def safe_reason(exc):
    if isinstance(exc, ContractError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "deadline_exhausted"
    if isinstance(exc, ForbiddenError):
        return "permission_denied"
    if isinstance(exc, ConflictError):
        return "version_conflict"
    return "operation_failed"


logger = logging.getLogger(__name__)


async def cancellation_shield(coroutine):
    """Drain a finite cleanup despite ASGI level cancellation or repeated cancel()."""
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(coroutine)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # The caller still re-raises its original cancellation afterwards.
                continue
        return task.result()


def recorded_public(method):
    @wraps(method)
    async def invoke(self, body, ctx):
        if self._closing:
            from contexthub.errors import ServiceUnavailableError

            raise ServiceUnavailableError("knowledge_closing")
        task = asyncio.current_task()
        self._active_requests.add(task)
        # This owner meters only S7 SQL, never S3–S6/model work a second time.
        execution = uuid5(
            NAMESPACE_URL,
            f"knowledge:{ctx.account_id}:{ctx.agent_id}:{body.request_id}",
        )
        if method.__name__ == "maintain":
            execution = uuid5(
                NAMESPACE_URL,
                f"knowledge-maintain:{ctx.account_id}:{body.event_id}:{body.graph_id}",
            )
        work = LocalOperation(
            self.executor.recorder,
            self._identity(
                ctx, execution, self.config, method.__name__ + "-public", "local"
            ),
            {"request_sha256": canonical_hash(body), "agent_id": ctx.agent_id},
        )
        work.tick = monotonic()
        token = self._local_work.set(work)
        try:
            await work.start()
            result = await method(self, body, ctx)
            await work.finish(
                {"operation_id": str(result.operation_id), "status": result.status}
            )
            return result
        except asyncio.CancelledError:
            try:
                await cancellation_shield(self._cancel_public(execution, ctx, work))
            except Exception:
                # DB unavailable/cleanup deadline: no invented rollback or success.
                logger.warning(
                    "knowledge_cancel_cleanup_failed",
                    extra={
                        "operation_id": str(execution),
                        "account_id": ctx.account_id,
                        "agent_id": ctx.agent_id,
                    },
                )
            if self._closing:
                from contexthub.errors import ServiceUnavailableError

                raise ServiceUnavailableError("knowledge_closing") from None
            raise
        except BaseException as exc:
            await work.finish({}, safe_reason(exc))
            raise
        finally:
            self._local_work.reset(token)
            self._active_requests.discard(task)

    return invoke


class _ShortSessions:
    """S3/S4 SQL protocol with a short transaction per statement, never per model.

    Intermediate immutable seals/carriers may survive failure. Final watch
    registration rechecks every version in a single transaction before readiness.
    """

    def __init__(self, service, ctx, sources):
        self.service, self.ctx, self.sources = service, ctx, sources

    async def _call(self, method, sql, *args):
        async with self.service.repo.session(self.ctx.account_id) as db:
            for source in self.sources:
                await self.service._authorize(db, source.context_id, self.ctx)
            return await getattr(db, method)(sql, *args)

    async def fetch(self, sql, *args):
        return await self._call("fetch", sql, *args)

    async def fetchrow(self, sql, *args):
        return await self._call("fetchrow", sql, *args)

    async def fetchval(self, sql, *args):
        return await self._call("fetchval", sql, *args)

    async def execute(self, sql, *args):
        return await self._call("execute", sql, *args)


class KnowledgeService:
    def __init__(
        self,
        repo,
        config,
        extractor,
        discovery,
        executor,
        runner,
        locator,
        context_service,
        clients,
    ):
        self.repo, self.config = repo, config
        self.extractor, self.discovery = extractor, discovery
        self.executor, self.runner, self.locator = executor, runner, locator
        self.context_service, self.clients = context_service, clients
        self.artifacts, self.acl = executor.artifacts, executor.acl
        self.evidence = EvidenceService(self.artifacts)
        self.config_hash = canonical_hash(config)
        self._local_work = ContextVar("knowledge_local_work", default=None)
        self._active_requests = set()
        self._closing = False

    @asynccontextmanager
    async def _session(self, account):
        async with self.repo.session(account) as db:
            work = self._local_work.get()
            yield MeteredDB(db, work) if work else db

    async def close(self):
        self._closing = True

        async def drain():
            active = tuple(
                t for t in self._active_requests if t is not asyncio.current_task()
            )
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            await self.clients.aclose()

        await cancellation_shield(drain())

    async def _cancel_public(self, ident, ctx, work):
        # No retry/recovery execution: only the actual request owner may stop its
        # unfinished work. This also handles cancellation at transaction commit.
        async with asyncio.timeout(self.config.maintenance.timeout_seconds):
            async with self._session(ctx.account_id) as db:
                await db.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(ident)
                )
                operation = await self._get(db, ident, "public_operation")
                if (
                    operation is not None
                    and operation.owner_call_id == work.identity.call_id
                ):
                    done = await self._get(
                        db, uuid5(ident, "completion"), "public_completion"
                    )
                    if done is None:
                        from contexthub.propagation.contracts import MaintenanceFrame

                        rows = await db.fetch(
                            """SELECT payload FROM knowledge_artifacts
                            WHERE account_id=$1 AND kind='maintenance_frame'
                            AND payload->>'execution_id'=$2""",
                            ctx.account_id,
                            str(ident),
                        )
                        for row in rows:
                            await self.executor.stop_work(
                                db,
                                MaintenanceFrame.model_validate(row["payload"]),
                                "request_cancelled",
                            )
                        # stop_work preserves all terminal outcomes/proofs/causes.
                        result = KnowledgeResult(
                            operation_id=ident,
                            request_id=operation.request_id,
                            execution_id=operation.execution_id,
                            config_sha256=operation.configuration_sha256,
                            operation=operation.operation,
                            status="error",
                            reason="request_cancelled",
                            content_saved=operation.operation == "ingest",
                            event_ids=(
                                (operation.event_id,) if operation.event_id else ()
                            ),
                        )
                        await self.artifacts.seal(
                            db,
                            PublicCompletion(
                                account_id=ctx.account_id,
                                id=uuid5(ident, "completion"),
                                operation_id=ident,
                                result=result,
                                report=operation.report,
                            ),
                        )
                        if operation.event_id:
                            await trace(
                                db,
                                operation.event_id,
                                "s7_cancelled",
                                {
                                    "operation_id": str(ident),
                                    "owner_call_id": str(work.identity.call_id),
                                    "reason": "request_cancelled",
                                },
                            )
            # Preserve existing per-attempt evidence. Interrupted inner attempts
            # stay explicitly incomplete: never invent tokens/cost/local work.
            records = await self.executor.recorder.records(
                ctx.account_id, work.identity.call_id
            )
            if records and not any(r.phase == "finished" for r in records):
                await work.finish({"operation_id": str(ident)}, "request_cancelled")

    def _identity(self, ctx, execution, config, operation, role):
        return CallIdentity(
            account_id=ctx.account_id,
            execution_id=execution,
            call_id=uuid4(),
            strategy=self.config.discovery.search.strategy,
            stage="S7",
            operation=operation,
            model_role=role,
            code_version=self.config.code_version,
            config_sha256=canonical_hash(config),
        )

    async def _authorize(self, db, source_id, ctx, *, write=False):
        row = await db.fetchrow(
            "SELECT * FROM contexts WHERE id=$1 AND account_id=$2",
            source_id,
            ctx.account_id,
        )
        if row is None:
            raise NotFoundError("knowledge_source_not_found")
        decision = await self.acl.check_read_access(db, row["uri"], ctx)
        if not decision.allowed or decision.field_masks:
            raise ForbiddenError("knowledge_source_not_accessible")
        if write and not await self.acl.check_write(db, row["uri"], ctx):
            raise ForbiddenError("knowledge_source_not_writable")
        return row

    async def _get(self, db, ident, kind):
        row = await db.fetchrow(
            "SELECT kind,sha256 FROM knowledge_artifacts WHERE account_id=$1 AND id=$2",
            await db.fetchval("SELECT current_setting('app.account_id')"),
            ident,
        )
        if row is None or row["kind"] != kind:
            return None
        return await self.artifacts.get(
            db,
            SealRef(
                account_id=await db.fetchval(
                    "SELECT current_setting('app.account_id')"
                ),
                id=ident,
                kind=kind,
                sha256=row["sha256"],
            ),
        )

    async def _operation_access(self, db, operation, ctx, *, write=False):
        if operation.agent_id != ctx.agent_id:
            raise ForbiddenError("knowledge_operation_agent_mismatch")
        for source in operation.source_ids:
            await self._authorize(db, source.context_id, ctx, write=write)

    async def _reserve_input(self, body, ctx):
        ident = uuid5(
            NAMESPACE_URL,
            f"knowledge:{ctx.account_id}:{ctx.agent_id}:{body.request_id}",
        )
        digest = canonical_hash(body)
        async with self._session(ctx.account_id) as db:
            # Lock only the short reservation/save transaction, never a model wait.
            await db.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(ident)
            )
            operation = await self._get(db, ident, "public_operation")
            if operation is not None:
                await self._operation_access(db, operation, ctx)
                if (
                    operation.request_sha256 != digest
                    or operation.configuration_sha256 != self.config_hash
                ):
                    raise ConflictError("knowledge_request_id_conflict")
            else:
                owner = ctx.agent_id if body.scope == "agent" else body.team
                if not await self.acl.check_write_target(
                    db, Scope(body.scope), owner, ctx
                ):
                    raise ForbiddenError("knowledge_scope_not_writable")
                from contexthub.models.knowledge_phases import PrepareRequest

                # Explicit prepared histories never scan unrelated runs.
                rows = []
                if not isinstance(body, PrepareRequest):
                    rows = await db.fetch(
                        """SELECT c.* FROM contexts c WHERE c.account_id=$1
                        AND c.scope=$2 AND c.owner_space=$3 AND c.status!='deleted'
                        AND c.uri LIKE 'ctx://%/knowledge/%' ORDER BY c.created_at,c.id""",
                        ctx.account_id,
                        body.scope,
                        owner,
                    )
                admitted = []
                if isinstance(body, PrepareRequest):
                    if body.previous:
                        previous = await self._prepared_access(db, body.previous, ctx)
                        if (previous.scope, previous.owner_space) != (
                            body.scope,
                            owner,
                        ):
                            raise ContractError("prepared_scope_mismatch")
                        admitted.extend(previous.source_ids)
                for row in rows:
                    decision = await self.acl.check_read_access(db, row["uri"], ctx)
                    if decision.allowed and not decision.field_masks:
                        admitted.append(
                            ContextVersion(context_id=row["id"], version=row["version"])
                        )
                for ref in body.provenance:
                    row = await self._authorize(db, ref.context_id, ctx)
                    if row["version"] != ref.version:
                        raise ConflictError("knowledge_provenance_version_conflict")
                    cv = ContextVersion(**ref.model_dump())
                    if cv not in admitted:
                        admitted.append(cv)
                source = await self.context_service.create(
                    db,
                    CreateContextRequest(
                        uri=f"ctx://{body.scope}/{owner}/knowledge/{ident}",
                        context_type="memory",
                        scope=body.scope,
                        owner_space=owner,
                        l2_content=body.content,
                    ),
                    ctx,
                )
                admitted.append(
                    ContextVersion(context_id=source.id, version=source.version)
                )
                operation = PublicOperation(
                    account_id=ctx.account_id,
                    id=ident,
                    owner_call_id=self._local_work.get().identity.call_id,
                    agent_id=ctx.agent_id,
                    request_sha256=digest,
                    configuration_sha256=self.config_hash,
                    operation="ingest",
                    request_id=body.request_id,
                    source_ids=tuple(admitted),
                    execution_id=ident,
                    scope=body.scope,
                    owner_space=owner,
                )
                await self.artifacts.seal(db, operation)
                created = True
        return operation, locals().get("created", False)

    @recorded_public
    async def ingest(self, body, ctx):
        operation, created = await self._reserve_input(body, ctx)
        ident = operation.id
        if not created:
            return await self.get_maintenance_result(ident, ctx)
        extraction = report = None
        watches = ()
        result = KnowledgeResult(
            operation_id=ident,
            request_id=body.request_id,
            execution_id=ident,
            config_sha256=self.config_hash,
            operation="ingest",
            status="pending",
            content_saved=True,
        )
        try:
            async with asyncio.timeout(self.config.ingest_timeout_seconds):
                result, extraction, report, watches = await self._ingest(
                    operation, body, ctx, result
                )
        except Exception as exc:
            result = result.model_copy(
                update={"status": "pending", "reason": safe_reason(exc)}
            )
        await self._finish(
            operation, result, extraction=extraction, report=report, watches=watches
        )
        return await self.get_maintenance_result(ident, ctx)

    async def _extract_input(self, operation, body, ctx):
        async with self._session(ctx.account_id) as db:
            cutoff = await db.fetchval("SELECT clock_timestamp()")
            refs = []
            for source in operation.source_ids:
                await self._authorize(db, source.context_id, ctx)
                refs.append(
                    await self.evidence.snapshot(
                        db,
                        account_id=ctx.account_id,
                        source=source,
                        content_level="l2_content",
                        cutoff_at=cutoff,
                    )
                )
            from contexthub.models.knowledge_phases import PrepareRequest

            if isinstance(body, PrepareRequest):
                previous = (
                    (await self._prepared_access(db, body.previous, ctx))
                    if body.previous
                    else None
                )
                prior_refs = (
                    (*previous.prior_extractions, previous.extraction)
                    if previous
                    else ()
                )
            else:
                prior_rows = await db.fetch(
                    """SELECT payload FROM knowledge_artifacts WHERE account_id=$1
                    AND kind='public_completion' AND payload->'extraction' IS NOT NULL""",
                    ctx.account_id,
                )
                prior_refs = tuple(
                    c.extraction
                    for r in prior_rows
                    if (c := PublicCompletion.model_validate(r["payload"])).extraction
                )
            prior = []
            allowed = set(operation.source_ids[:-1])
            for candidate_ref in dict.fromkeys(prior_refs):
                artifact = await self.artifacts.get(db, candidate_ref)
                versions = [
                    (await self.artifacts.get(db, s)).source for s in artifact.sources
                ]
                # Use exact original seals for past extracted batches.
                if set(versions) <= allowed:
                    prior.append(candidate_ref)
                    for v, ref in zip(versions, artifact.sources):
                        refs[operation.source_ids.index(v)] = ref
        db = _ShortSessions(self, ctx, operation.source_ids)
        extracted = await self.extractor.extract(
            db,
            (refs[-1],),
            identity=self._identity(
                ctx, operation.id, self.config.extraction, "extract", "extraction"
            ),
        )
        provenance = tuple(
            ExplicitProvenance(
                proposition_id=p.id,
                source=refs[
                    operation.source_ids.index(ContextVersion(**r.model_dump()))
                ],
                reason="user_declared_read_input",
            )
            for p in extracted.propositions
            for r in body.provenance
        )
        history = HistorySnapshot(
            account_id=ctx.account_id,
            entries=tuple(
                HistoryEntry(source=r, ingestion_ordinal=i) for i, r in enumerate(refs)
            ),
            cutoff_at=cutoff,
            cutoff_ordinal=len(refs) - 1,
            provenance=provenance,
        )
        return extracted, history, tuple(prior)

    async def _discover_input(
        self, operation, extracted, history, prior, ctx, *, activate
    ):
        db = _ShortSessions(self, ctx, operation.source_ids)
        report = await self.discovery.discover(
            db,
            extracted.reference(),
            history,
            identity=self._identity(
                ctx, operation.id, self.config.discovery, "discover", "local"
            ),
            prior_extractions=tuple(prior),
        )
        async with self._session(ctx.account_id) as tx:
            for source in operation.source_ids:
                await self._authorize(tx, source.context_id, ctx)
            graph = await self.artifacts.get(tx, report.graph)
            for node in graph.nodes:
                await self._authorize(tx, node.context_id, ctx, write=True)
                for rule in self.runner.rules:
                    await self.executor.register_rule_inputs(
                        tx, account_id=ctx.account_id, target=node, provider=rule
                    )
            watches = (
                await self.executor.register_graph(
                    tx, report.reference(), self.config.maintenance, ctx
                )
                if activate
                else ()
            )
        return report, watches

    async def _ingest(self, operation, body, ctx, result):
        extracted, history, prior = await self._extract_input(operation, body, ctx)
        result = result.model_copy(update={"cutoff_at": history.cutoff_at})
        if extracted.status != "complete":
            return (
                result.model_copy(update={"reason": extracted.failure_reason}),
                extracted.reference(),
                None,
                (),
            )
        report, watches = await self._discover_input(
            operation, extracted, history, prior, ctx, activate=True
        )
        refs = tuple(e.source for e in history.entries)
        result = result.model_copy(
            update={
                "status": "complete" if report.status == "complete" else "pending",
                "graph_status": (
                    "deliverable" if report.status == "complete" else "pending"
                ),
                "maintenance_ready": report.status == "complete",
                "report_id": report.id,
                "graph_id": report.graph.id,
                "watch_ids": tuple(w.id for w in watches),
                "reason": report.reasons[0] if report.reasons else None,
            }
        )
        if body.mode == "change":
            try:
                result = await self._locate_change(operation, refs, ctx, result)
            except Exception as exc:
                result = result.model_copy(
                    update={
                        "status": "pending",
                        "location_status": "pending",
                        "reason": safe_reason(exc),
                    }
                )
        return result, extracted.reference(), report.reference(), watches

    async def _locate_change(self, operation, refs, ctx, result):
        # Full declared candidate inventory, no entity/gold/answer target injection.
        candidates = []
        async with self._session(ctx.account_id) as db:
            for cv, ref in zip(operation.source_ids, refs):
                await self._authorize(db, cv.context_id, ctx)
                source = await self.artifacts.get(db, ref)
                data = dict(
                    context_id=str(cv.context_id),
                    version=cv.version,
                    snapshot_id=str(ref.id),
                    text=await self.evidence.read_source(db, source),
                )
                candidates.append(data)
        location = await self.locator.locate(
            candidates[-1],
            candidates[:-1],
            identity=self._identity(
                ctx,
                operation.id,
                self.config.change_location,
                "change_location",
                "locator",
            ),
        )
        # Persist exact locator evidence separately; public responses exclude raw text.
        from contexthub.services.change_location import ChangeLocationRecord

        async with self._session(ctx.account_id) as db:
            await self.artifacts.seal(
                db,
                ChangeLocationRecord(
                    account_id=ctx.account_id,
                    id=uuid5(operation.id, "location"),
                    operation_id=operation.id,
                    sources=tuple(refs),
                    result=location,
                ),
            )
        if location.status != "located":
            return result.model_copy(
                update={
                    "location_status": location.status,
                    "status": (
                        "pending" if location.status == "pending" else result.status
                    ),
                    "reason": "change_location_" + location.status,
                }
            )
        match = location.matches[0]
        async with self._session(ctx.account_id) as db:
            row = await self._authorize(db, match.source_id, ctx, write=True)
            # A real S5 watch must already be committed for this exact version.
            watches = await db.fetch(
                """SELECT id FROM knowledge_artifacts WHERE account_id=$1
                AND kind='maintenance_watch' AND payload->'source'->>'context_id'=$2
                AND (payload->'source'->>'version')::int=$3
                AND payload->'reasons'='[]'::jsonb""",
                ctx.account_id,
                str(match.source_id),
                match.version,
            )
            if not watches:
                raise ContractError("change_source_not_registered")
            updated = await self.context_service.update(
                db,
                row["uri"],
                UpdateContextRequest(l2_content=match.replacement.excerpt),
                RequestContext(
                    account_id=ctx.account_id,
                    agent_id=ctx.agent_id,
                    expected_version=match.version,
                ),
            )
            events = await db.fetch(
                """SELECT event_id FROM change_events WHERE account_id=$1
                AND context_id=$2 AND source_version=$3 AND change_type='modified'""",
                ctx.account_id,
                updated.id,
                updated.version,
            )
        return result.model_copy(
            update={
                "location_status": "located",
                "event_ids": tuple(e["event_id"] for e in events),
            }
        )

    async def _finish(
        self, operation, result, *, extraction=None, report=None, watches=()
    ):
        async with self._session(operation.account_id) as db:
            await self.artifacts.seal(
                db,
                PublicCompletion(
                    account_id=operation.account_id,
                    id=uuid5(operation.id, "completion"),
                    operation_id=operation.id,
                    result=result,
                    extraction=extraction,
                    report=report,
                    watches=watches,
                ),
            )

    async def _watch_access(self, db, event, graph_id, ctx):
        refs = tuple(
            SealRef.model_validate(r)
            for r in (event["metadata"] or {}).get("s5_watches", ())
        )
        matching = []
        for ref in refs:
            watch = await self.artifacts.get(db, ref)
            if watch.graph.id == graph_id:
                matching.append(watch)
        if len(matching) != 1:
            raise NotFoundError("knowledge_registered_graph_not_found")
        watch = matching[0]
        if watch.agent_id != ctx.agent_id:
            raise ForbiddenError("knowledge_watch_agent_mismatch")
        for cv in (watch.source, *watch.affected):
            await self._authorize(db, cv.context_id, ctx, write=True)
        return watch

    @recorded_public
    async def maintain(self, body, ctx):
        ident = uuid5(
            NAMESPACE_URL,
            f"knowledge-maintain:{ctx.account_id}:{body.event_id}:{body.graph_id}",
        )
        created = False
        async with self._session(ctx.account_id) as db:
            event = await db.fetchrow(
                "SELECT * FROM change_events WHERE account_id=$1 AND event_id=$2",
                ctx.account_id,
                body.event_id,
            )
            if event is None:
                raise NotFoundError("knowledge_event_not_found")
            watch = await self._watch_access(db, event, body.graph_id, ctx)
            if (event["metadata"] or {}).get("s6_execution_id"):
                # Child changes belong to the original controlled frontier.
                raise ConflictError("knowledge_child_event_owned_by_root")
            await db.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(ident)
            )
            operation = await self._get(db, ident, "public_operation")
            if operation is None:
                operation = PublicOperation(
                    account_id=ctx.account_id,
                    id=ident,
                    owner_call_id=self._local_work.get().identity.call_id,
                    agent_id=ctx.agent_id,
                    request_sha256=canonical_hash(body),
                    configuration_sha256=self.config_hash,
                    operation="maintain",
                    request_id=body.request_id,
                    source_ids=(watch.source, *watch.affected),
                    execution_id=ident,
                    scope="registered",
                    owner_space=ctx.agent_id,
                    event_id=body.event_id,
                    report=watch.report,
                )
                await self.artifacts.seal(db, operation)
                await trace(
                    db,
                    body.event_id,
                    "s7_manual_claim",
                    dict(
                        operation_id=str(ident),
                        request_id=str(body.request_id),
                        agent_id=ctx.agent_id,
                        config_sha256=self.config_hash,
                    ),
                )
                created = True
        if not created:
            return await self.get_maintenance_result(ident, ctx)
        result = KnowledgeResult(
            operation_id=ident,
            request_id=operation.request_id,
            execution_id=ident,
            config_sha256=self.config_hash,
            operation="maintain",
            status="running",
            graph_id=body.graph_id,
            graph_status="pending" if watch.reasons else "deliverable",
            maintenance_ready=not watch.reasons,
            watch_ids=(watch.id,),
            report_id=watch.report.id,
            event_ids=(body.event_id,),
        )
        try:
            refs = await self.executor.begin_maintenance(
                body.event_id, watch.reference(), execution_id=ident
            )
            await self._frontier(refs, ctx)
            result = result.model_copy(update={"status": "complete"})
        except Exception as exc:
            result = result.model_copy(
                update={"status": "error", "reason": safe_reason(exc)}
            )
        await self._finish(
            operation, result, report=watch.report, watches=(watch.reference(),)
        )
        return await self.get_maintenance_result(ident, ctx)

    async def _frontier(self, refs, ctx):
        frames = []
        async with self._session(ctx.account_id) as db:
            for ref in refs:
                frame = await self.artifacts.get(db, ref)
                snap = await self.artifacts.get(db, frame.snapshot)
                frames.append((frame, snap.target))
            graph = await self.artifacts.get(db, snap.graph) if frames else None
        remaining = dict(
            (target.context_id, (frame, target)) for frame, target in frames
        )
        proofs = []
        # At most three carrier levels (source carrier plus two downstream hops).
        for _ in range(3):
            ready = [
                (key, item)
                for key, item in remaining.items()
                if not any(
                    e.downstream == item[1] and e.upstream.context_id in remaining
                    for e in graph.edges
                )
            ]
            if not ready:
                break
            for key, (frame, target) in ready:
                async with self._session(ctx.account_id) as db:
                    await self._authorize(db, target.context_id, ctx, write=True)
                ref = frame.reference()
                if proofs:
                    # Real ancestor proofs only; core rejects unrelated drift or spent menus.
                    ancestors = set()
                    todo = [target]
                    while todo:
                        node = todo.pop()
                        for e in graph.edges:
                            if e.downstream == node and e.upstream not in ancestors:
                                ancestors.add(e.upstream)
                                todo.append(e.upstream)
                    selected = [p for cv, p in proofs if cv in ancestors]
                    if selected:
                        ref = await self.executor.continue_after_commits(
                            ref, tuple(selected)
                        )
                state = await self.runner.run(ref)
                if state.proof:
                    proofs.append((target, state.proof))
                remaining.pop(key)
        for frame, target in remaining.values():
            async with self._session(ctx.account_id) as db:
                await self.executor.stop_work(db, frame, "public_frontier_limit")

    async def handle_event(self, event):
        """Delivery only. Public manual maintenance is the sole generation owner.

        S6 child events remain covered by the root frontier. Never reinitialize
        them in the background or fall through to the legacy rule executor.
        """
        async with self._session(event["account_id"]) as db:
            await trace(
                db,
                event["event_id"],
                "s7_delivery",
                {"owner": "manual", "config_sha256": self.config_hash},
            )
        return ()

    @staticmethod
    def _node(row):
        return KnowledgeNode(
            context_id=row["id"],
            uri=row["uri"],
            version=row["version"],
            fresh=row["status"] == "active"
            and row["validity_status"] == "fresh"
            and not row.get("has_causes", False),
        )

    async def get_maintenance_result(self, operation_id, ctx):
        # Pure reads: no reconstruction, new frames, model calls, or repair writes.
        async with self._session(ctx.account_id) as db:
            op = await self._get(db, operation_id, "public_operation")
            if op is None:
                raise NotFoundError("knowledge_operation_not_found")
            await self._operation_access(db, op, ctx)
            done = await self._get(db, uuid5(op.id, "completion"), "public_completion")
            result = (
                done.result
                if done
                else KnowledgeResult(
                    operation_id=op.id,
                    request_id=op.request_id,
                    execution_id=op.execution_id,
                    config_sha256=op.configuration_sha256,
                    operation=op.operation,
                    status="running",
                    reason="execution_incomplete",
                    content_saved=op.operation == "ingest",
                )
            )
            if op.operation == "ingest":
                row = await self._current(db, op.source_ids[-1].context_id)
                events = await db.fetch(
                    """SELECT event_id FROM change_events WHERE account_id=$1
                    AND context_id=$2 AND change_type='modified' ORDER BY timestamp,event_id""",
                    ctx.account_id,
                    row["id"],
                )
                result = result.model_copy(
                    update={
                        "source": self._node(row),
                        "event_ids": tuple(
                            dict.fromkeys(
                                (*result.event_ids, *(e["event_id"] for e in events))
                            )
                        ),
                    }
                )
            report_ref = done.report if done else op.report
            nodes = []
            if report_ref:
                report = await self.artifacts.get(db, report_ref)
                graph = await self.artifacts.get(db, report.graph)
                for node in graph.nodes:
                    row = await self._current(db, node.context_id)
                    decision = await self.acl.check_read_access(db, row["uri"], ctx)
                    if decision.allowed and not decision.field_masks:
                        nodes.append(self._node(row))
            work_rows = await db.fetch(
                """SELECT f.payload FROM knowledge_artifacts f
                WHERE f.account_id=$1 AND f.kind='maintenance_frame'
                AND f.payload->>'execution_id'=$2 ORDER BY f.created_at,f.id""",
                ctx.account_id,
                str(op.execution_id),
            )
            works = []
            for row in work_rows:
                from contexthub.propagation.contracts import MaintenanceFrame

                frame = MaintenanceFrame.model_validate(row["payload"])
                snapshot = await self.artifacts.get(db, frame.snapshot)
                target = await self._current(db, snapshot.target.context_id)
                decision = await self.acl.check_read_access(db, target["uri"], ctx)
                if not decision.allowed or decision.field_masks:
                    continue
                state = await self.artifacts.get_work(db, ctx.account_id, snapshot.id)
                works.append(
                    KnowledgeWork(
                        work_id=snapshot.id,
                        frame_id=frame.id,
                        target_id=target["id"],
                        status=state.status,
                        reason=state.reason,
                        proof_id=state.proof.id if state.proof else None,
                        current_version=target["version"],
                        fresh=self._node(target).fresh,
                    )
                )
            delivery = None
            if op.event_id:
                delivery = await db.fetchval(
                    "SELECT delivery_status FROM change_events WHERE account_id=$1 AND event_id=$2",
                    ctx.account_id,
                    op.event_id,
                )
            return result.model_copy(
                update={
                    "nodes": tuple(nodes),
                    "works": tuple(works),
                    "event_delivery": delivery,
                }
            )

    async def _prepared_access(self, db, ref, ctx):
        from contexthub.models.knowledge_phases import PreparedInput

        prepared = await self.artifacts.get(db, ref)
        if not isinstance(prepared, PreparedInput):
            raise ContractError("prepared_reference_required")
        if prepared.agent_id != ctx.agent_id:
            raise ForbiddenError("prepared_agent_mismatch")
        if prepared.extraction_config_sha256 != canonical_hash(self.config.extraction):
            raise ContractError("prepared_extraction_configuration_mismatch")
        for cv in prepared.source_ids:
            row = await self._authorize(db, cv.context_id, ctx)
            if row["version"] != cv.version:
                raise ConflictError("prepared_source_version_drift")
        done = await self._get(
            db, uuid5(prepared.operation_id, "completion"), "public_completion"
        )
        if done is None or done.result.status != "complete":
            raise ContractError("prepared_operation_incomplete")
        extraction = await self.artifacts.get(db, prepared.extraction)
        if extraction.status not in ("complete", "partial"):
            raise ContractError("prepared_extraction_incomplete")
        if tuple(extraction.sources) != (prepared.history.entries[-1].source,):
            raise ContractError("prepared_source_mismatch")
        return prepared

    async def get_graph_handoff(self, operation_id, ctx):
        """Read the exact aggregate receipt with source, carrier and watch ACLs.

        This never rebuilds a graph or acquires a runtime commit capability.
        Current versions may have advanced; consumers bind a before-state
        database snapshot separately, rather than pretending this is that state.
        """
        from contexthub.discovery.contracts import GraphHandoff

        async with self._session(ctx.account_id) as db:
            operation = await self._get(db, operation_id, "public_operation")
            if operation is None:
                raise NotFoundError("knowledge_operation_not_found")
            await self._operation_access(db, operation, ctx)
            done = await self._get(
                db, uuid5(operation_id, "completion"), "public_completion"
            )
            if done is None or done.report is None:
                raise ContractError("graph_handoff_incomplete")
            handoff = await self.artifacts.get(db, done.report)
            if (
                not isinstance(handoff, GraphHandoff)
                or handoff.agent_id != ctx.agent_id
            ):
                raise ContractError("graph_handoff_required")
            graph = await self.artifacts.get(db, handoff.graph)
            for cv in (*handoff.sources, *graph.nodes):
                await self._authorize(db, cv.context_id, ctx)
            for ref in done.watches:
                watch = await self.artifacts.get(db, ref)
                if (
                    watch.report != done.report
                    or watch.graph != handoff.graph
                    or watch.agent_id != ctx.agent_id
                ):
                    raise ContractError("graph_watch_mismatch")
                await self._authorize(db, watch.source.context_id, ctx)
            return handoff

    async def get_prepared(self, reference, ctx):
        """Read a completed prepared input after the same reference/ACL checks."""
        async with self._session(ctx.account_id) as db:
            return await self._prepared_access(db, reference, ctx)

    async def discovery_report_reference(self, operation_id, ctx):
        """Read a batch report reference without exposing the artifact store."""
        async with self._session(ctx.account_id) as db:
            operation = await self._get(db, operation_id, "public_operation")
            if operation is None:
                raise NotFoundError("knowledge_operation_not_found")
            await self._operation_access(db, operation, ctx)
            done = await self._get(
                db, uuid5(operation_id, "completion"), "public_completion"
            )
            if (
                done is None
                or done.report is None
                or done.report.kind != "discovery_report"
            ):
                raise ContractError("discovery_report_incomplete")
            report = await self.artifacts.get(db, done.report)
            for mapping in report.mappings:
                await self._authorize(db, mapping.node.context_id, ctx)
            return done.report

    async def execution_records(self, operation_ids, ctx):
        """Export existing core facts; no new attempts or experimental charges."""
        calls = []
        async with self._session(ctx.account_id) as db:
            for ident in dict.fromkeys(operation_ids):
                operation = await self._get(db, ident, "public_operation")
                if operation is None:
                    raise NotFoundError("knowledge_operation_not_found")
                await self._operation_access(db, operation, ctx)
                calls.extend(
                    r["call_id"]
                    for r in await db.fetch(
                        "SELECT DISTINCT call_id FROM execution_attempts WHERE account_id=$1 AND execution_id=$2 ORDER BY call_id",
                        ctx.account_id,
                        operation.execution_id,
                    )
                )
        records = []
        for call in dict.fromkeys(calls):
            records.extend(await self.executor.recorder.records(ctx.account_id, call))
        return tuple(records)

    @recorded_public
    async def prepare(self, body, ctx):
        """Store one intact input and extract once; share only the sealed handle."""
        from contexthub.models.knowledge_phases import (
            PrepareRequest,
            PreparedInput,
            PreparedResult,
        )

        body = PrepareRequest.model_validate(body.model_dump(mode="json"))
        operation, created = await self._reserve_input(body, ctx)
        if not created:
            async with self._session(ctx.account_id) as db:
                prepared = await self._get(
                    db, uuid5(operation.id, "prepared"), "prepared_input"
                )
            return PreparedResult(
                result=await self.get_maintenance_result(operation.id, ctx),
                prepared=prepared.reference() if prepared else None,
            )
        result = KnowledgeResult(
            operation_id=operation.id,
            request_id=body.request_id,
            execution_id=operation.id,
            config_sha256=self.config_hash,
            operation="ingest",
            status="pending",
            content_saved=True,
        )
        prepared = extracted = None
        try:
            async with asyncio.timeout(self.config.ingest_timeout_seconds):
                extracted, history, prior = await self._extract_input(
                    operation, body, ctx
                )
                if extracted.status in ("complete", "partial"):
                    prepared = PreparedInput(
                        account_id=ctx.account_id,
                        id=uuid5(operation.id, "prepared"),
                        operation_id=operation.id,
                        agent_id=ctx.agent_id,
                        scope=operation.scope,
                        owner_space=operation.owner_space,
                        source_ids=operation.source_ids,
                        extraction=extracted.reference(),
                        extraction_config_sha256=canonical_hash(self.config.extraction),
                        prior_extractions=prior,
                        previous=body.previous,
                        history=history,
                    )
                    async with self._session(ctx.account_id) as db:
                        await self.artifacts.seal(db, prepared)
                    result = result.model_copy(
                        update={
                            "status": "complete",
                            "reason": "prepared_with_unresolved_exclusions" if extracted.unresolved else "prepared",
                            "cutoff_at": history.cutoff_at,
                        }
                    )
                else:
                    result = result.model_copy(
                        update={"reason": extracted.failure_reason}
                    )
        except Exception as exc:
            prepared = None
            result = result.model_copy(update={"reason": safe_reason(exc)})
        await self._finish(
            operation, result, extraction=extracted.reference() if extracted else None
        )
        return PreparedResult(
            result=await self.get_maintenance_result(operation.id, ctx),
            prepared=prepared.reference() if prepared else None,
        )

    async def _reserve_phase(
        self,
        db,
        body,
        ctx,
        sources,
        scope,
        owner,
        *,
        report=None,
        persist_content=False,
    ):
        ident = uuid5(
            NAMESPACE_URL,
            f"knowledge:{ctx.account_id}:{ctx.agent_id}:{body.request_id}",
        )
        await db.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(ident)
        )
        existing = await self._get(db, ident, "public_operation")
        if existing:
            await self._operation_access(db, existing, ctx)
            if (
                existing.request_sha256 != canonical_hash(body)
                or existing.configuration_sha256 != self.config_hash
            ):
                raise ConflictError("knowledge_request_id_conflict")
            return existing, False
        if persist_content:
            source = await self.context_service.create(
                db,
                CreateContextRequest(
                    uri=f"ctx://{scope}/{owner}/knowledge/{ident}",
                    context_type="memory",
                    scope=scope,
                    owner_space=owner,
                    l2_content=body.content,
                ),
                ctx,
            )
            sources = (
                *sources,
                ContextVersion(context_id=source.id, version=source.version),
            )
        operation = PublicOperation(
            account_id=ctx.account_id,
            id=ident,
            owner_call_id=self._local_work.get().identity.call_id,
            agent_id=ctx.agent_id,
            request_sha256=canonical_hash(body),
            configuration_sha256=self.config_hash,
            operation="ingest",
            request_id=body.request_id,
            source_ids=sources,
            execution_id=ident,
            scope=scope,
            owner_space=owner,
            report=report,
        )
        await self.artifacts.seal(db, operation)
        return operation, True

    @recorded_public
    async def discover_prepared(self, body, ctx):
        """Use the exact sealed extraction/history; does not activate batch watches."""
        from contexthub.models.knowledge_phases import DiscoverPreparedRequest

        body = DiscoverPreparedRequest.model_validate(body.model_dump(mode="json"))
        async with self._session(ctx.account_id) as db:
            prepared = await self._prepared_access(db, body.prepared, ctx)
            operation, created = await self._reserve_phase(
                db, body, ctx, prepared.source_ids, prepared.scope, prepared.owner_space
            )
            extracted = await self.artifacts.get(db, prepared.extraction)
        if not created:
            return await self.get_maintenance_result(operation.id, ctx)
        result = KnowledgeResult(
            operation_id=operation.id,
            request_id=body.request_id,
            execution_id=operation.id,
            config_sha256=self.config_hash,
            operation="ingest",
            status="pending",
            content_saved=True,
            cutoff_at=prepared.history.cutoff_at,
        )
        report = None
        try:
            async with asyncio.timeout(self.config.ingest_timeout_seconds):
                report, _ = await self._discover_input(
                    operation,
                    extracted,
                    prepared.history,
                    prepared.prior_extractions,
                    ctx,
                    activate=False,
                )
                result = result.model_copy(
                    update={
                        "status": report.status,
                        "graph_id": report.graph.id,
                        "report_id": report.id,
                        "reason": report.reasons[0]
                        if report.reasons
                        else "awaiting_graph_finalization",
                    }
                )
        except Exception as exc:
            result = result.model_copy(update={"reason": safe_reason(exc)})
        await self._finish(
            operation,
            result,
            extraction=prepared.extraction,
            report=report.reference() if report else None,
        )
        return await self.get_maintenance_result(operation.id, ctx)

    @recorded_public
    async def finalize_graph(self, body, ctx):
        """Seal and activate exactly the union of every batch in a prepared chain."""
        from contexthub.models.knowledge_phases import FinalizeGraphRequest
        from contexthub.discovery.contracts import (
            DiscoveryReport,
            GraphHandoff,
            encode_configuration,
        )
        from contexthub.discovery.service import assemble_graph_snapshot

        body = FinalizeGraphRequest.model_validate(body.model_dump(mode="json"))
        if len(body.reports) != len(body.prepared) or len(set(body.reports)) != len(
            body.reports
        ):
            raise ContractError("handoff_batch_mismatch")
        async with self._session(ctx.account_id) as db:
            preparations = [
                await self._prepared_access(db, ref, ctx) for ref in body.prepared
            ]
            reports = [await self.artifacts.get(db, ref) for ref in body.reports]
            graphs, mappings = [], {}
            for i, (prepared, report) in enumerate(zip(preparations, reports)):
                if prepared.previous != (body.prepared[i - 1] if i else None):
                    raise ContractError("handoff_history_not_complete")
                if not isinstance(report, DiscoveryReport) or (
                    report.shared_extraction != prepared.extraction
                    or report.prior_extractions != prepared.prior_extractions
                    or report.history_sha256 != canonical_hash(prepared.history)
                    or report.config_sha256 != canonical_hash(self.config.discovery)
                ):
                    raise ContractError("handoff_report_mismatch")
                graph = await self.artifacts.get(db, report.graph)
                graphs.append(graph)
                for m in report.mappings:
                    key = m.node.context_id
                    if key in mappings and mappings[key] != m:
                        raise ContractError("handoff_mapping_conflict")
                    mappings[key] = m
            last = preparations[-1]
            operation, created = await self._reserve_phase(
                db, body, ctx, last.source_ids, last.scope, last.owner_space
            )
            if created:
                graph = assemble_graph_snapshot(
                    graphs,
                    account_id=ctx.account_id,
                    graph_id=uuid5(operation.id, "aggregate-graph"),
                    config_sha256=canonical_hash(self.config.discovery),
                )
                await self.artifacts.seal(db, graph)
                reasons = tuple(
                    dict.fromkeys(r for report in reports for r in report.reasons)
                )
                handoff = GraphHandoff(
                    account_id=ctx.account_id,
                    id=uuid5(operation.id, "handoff"),
                    execution_id=operation.id,
                    agent_id=ctx.agent_id,
                    scope=last.scope,
                    owner_space=last.owner_space,
                    config_sha256=canonical_hash(self.config.discovery),
                    configuration=encode_configuration(self.config.discovery),
                    strategy=self.config.discovery.search.strategy,
                    excluded_unresolved_extractions=tuple(dict.fromkeys(
                        ref for report in reports for ref in report.excluded_unresolved_extractions)),
                    reports=body.reports,
                    prepared=body.prepared,
                    sources=last.source_ids,
                    graph=graph.reference(),
                    mappings=tuple(mappings.values()),
                    status="pending" if reasons or graph.pending else "complete",
                    reasons=reasons or (("graph_pending",) if graph.pending else ()),
                )
                for cv in (*handoff.sources, *graph.nodes):
                    await self._authorize(db, cv.context_id, ctx, write=True)
                await self.artifacts.seal(db, handoff)
                watches = await self.executor.register_graph(
                    db, handoff.reference(), self.config.maintenance, ctx
                )
                result = KnowledgeResult(
                    operation_id=operation.id,
                    request_id=body.request_id,
                    execution_id=operation.id,
                    config_sha256=self.config_hash,
                    operation="ingest",
                    status=handoff.status,
                    content_saved=True,
                    graph_id=graph.id,
                    report_id=handoff.id,
                    graph_status="deliverable"
                    if handoff.status == "complete"
                    else "pending",
                    maintenance_ready=handoff.status == "complete",
                    watch_ids=tuple(r.id for r in watches),
                    cutoff_at=last.history.cutoff_at,
                    reason=reasons[0] if reasons else None,
                )
                # Activation and receipt commit together; cancellation cannot expose
                # registered watches without an immutable final handoff receipt.
                await self.artifacts.seal(
                    db,
                    PublicCompletion(
                        account_id=ctx.account_id,
                        id=uuid5(operation.id, "completion"),
                        operation_id=operation.id,
                        result=result,
                        report=handoff.reference(),
                        watches=watches,
                    ),
                )
        return await self.get_maintenance_result(operation.id, ctx)

    @recorded_public
    async def ingest_assertion_change(self, body, ctx):
        """Save intact text and version grounded root carriers; never runs P1/P2.

        Derived nodes are excluded, even if the model attempts to name one.
        All matched updates/events commit atomically after rechecking every basis.
        """
        from contexthub.models.knowledge_phases import AssertionChangeRequest
        from contexthub.discovery.contracts import GraphHandoff
        from contexthub.services.assertion_change import (
            locate_assertions,
            AssertionChangeRecord,
        )

        body = AssertionChangeRequest.model_validate(body.model_dump(mode="json"))
        async with self._session(ctx.account_id) as db:
            handoff = await self.artifacts.get(db, body.handoff)
            if (
                not isinstance(handoff, GraphHandoff)
                or handoff.agent_id != ctx.agent_id
            ):
                raise ContractError("assertion_handoff_mismatch")
            if handoff.status != "complete":
                raise ContractError("assertion_graph_pending")
            graph = await self.artifacts.get(db, handoff.graph)
            for cv in (*handoff.sources, *graph.nodes):
                await self._authorize(db, cv.context_id, ctx, write=True)
            operation, created = await self._reserve_phase(
                db,
                body,
                ctx,
                handoff.sources,
                handoff.scope,
                handoff.owner_space,
                report=body.handoff,
                persist_content=True,
            )
            if created:
                # The stored source is the exact request text, not selected spans.
                source = operation.source_ids[-1]
                cutoff = await db.fetchval("SELECT clock_timestamp()")
                raw_ref = await self.evidence.snapshot(
                    db,
                    account_id=ctx.account_id,
                    source=source,
                    content_level="l2_content",
                    cutoff_at=cutoff,
                )
                raw = dict(
                    context_id=str(source.context_id),
                    version=source.version,
                    snapshot_id=str(raw_ref.id),
                    text=body.content,
                )
                candidates, refs = [], [raw_ref]
                derived = {
                    e.downstream.context_id for e in (*graph.edges, *graph.pending)
                }
                for m in handoff.mappings:
                    if m.node.context_id in derived or m.proposition_id is None:
                        continue
                    # A paraphrase or multi-premise derived claim is not a directly
                    # quoted assertion. Missing eligibility means no direct write.
                    assertions = tuple(e for e in m.evidence if e.excerpt == m.text)
                    if not assertions:
                        continue
                    row = await self._authorize(db, m.node.context_id, ctx, write=True)
                    carrier_ref = await self.evidence.snapshot(
                        db,
                        account_id=ctx.account_id,
                        source=ContextVersion(
                            context_id=m.node.context_id, version=row["version"]
                        ),
                        content_level="l2_content",
                        cutoff_at=cutoff,
                    )
                    refs.append(carrier_ref)
                    refs.extend(e.source for e in assertions)
                    candidates.append(
                        dict(
                            context_id=str(m.node.context_id),
                            version=row["version"],
                            snapshot_id=str(carrier_ref.id),
                            text=row["l2_content"],
                            assertions=[e.model_dump(mode="json") for e in assertions],
                        )
                    )
        if not created:
            return await self.get_maintenance_result(operation.id, ctx)
        result = KnowledgeResult(
            operation_id=operation.id,
            request_id=body.request_id,
            execution_id=operation.id,
            config_sha256=self.config_hash,
            operation="ingest",
            status="pending",
            content_saved=True,
            graph_id=graph.id,
            report_id=handoff.id,
            graph_status="deliverable",
            maintenance_ready=True,
            location_status="pending",
            cutoff_at=cutoff,
        )
        try:
            async with asyncio.timeout(self.config.ingest_timeout_seconds):
                location = await locate_assertions(
                    self.locator,
                    raw,
                    candidates,
                    identity=self._identity(
                        ctx,
                        operation.id,
                        self.config.change_location,
                        "assertion_location",
                        "locator",
                    ),
                )
                async with self._session(ctx.account_id) as db:
                    await self.artifacts.seal(
                        db,
                        AssertionChangeRecord(
                            account_id=ctx.account_id,
                            id=uuid5(operation.id, "assertion-change"),
                            operation_id=operation.id,
                            handoff=body.handoff,
                            sources=tuple(dict.fromkeys(refs)),
                            result=location,
                        ),
                    )
                async with self._session(ctx.account_id) as db:
                    events = []
                    # Lock in stable order, then recheck the entire graph/source
                    # basis before *any* write, including permission changes.
                    for cv in sorted(
                        (*handoff.sources, *graph.nodes),
                        key=lambda n: str(n.context_id),
                    ):
                        await db.fetchval(
                            "SELECT version FROM contexts WHERE id=$1 FOR UPDATE",
                            cv.context_id,
                        )
                        row = await self._authorize(db, cv.context_id, ctx, write=True)
                        if location.matches and row["version"] != cv.version:
                            raise ConflictError("assertion_graph_version_drift")
                    for match in location.matches:
                        row = await self._authorize(
                            db, match.carrier_id, ctx, write=True
                        )
                        outgoing = any(
                            e.upstream.context_id == match.carrier_id
                            for e in graph.edges
                        )
                        if outgoing:
                            watch = await db.fetchval(
                                """SELECT id FROM knowledge_artifacts WHERE account_id=$1
                                AND kind='maintenance_watch' AND payload->'report'->>'id'=$2
                                AND payload->'source'->>'context_id'=$3
                                AND (payload->'source'->>'version')::int=$4
                                AND payload->'reasons'='[]'::jsonb""",
                                ctx.account_id,
                                str(handoff.id),
                                str(match.carrier_id),
                                match.version,
                            )
                            if not watch:
                                raise ContractError("assertion_watch_missing")
                        updated = await self.context_service.update(
                            db,
                            row["uri"],
                            UpdateContextRequest(l2_content=match.replacement.excerpt),
                            RequestContext(
                                account_id=ctx.account_id,
                                agent_id=ctx.agent_id,
                                expected_version=match.version,
                            ),
                        )
                        rows = await db.fetch(
                            """SELECT event_id FROM change_events WHERE account_id=$1
                            AND context_id=$2 AND source_version=$3 AND change_type='modified'""",
                            ctx.account_id,
                            updated.id,
                            updated.version,
                        )
                        if len(rows) != 1:
                            raise ContractError("assertion_event_not_unique")
                        events.append(rows[0]["event_id"])
                    completed_result = result.model_copy(
                        update={
                            "status": "pending"
                            if location.status == "pending"
                            else "complete",
                            "location_status": location.status,
                            "event_ids": tuple(events),
                            "reason": location.reason,
                        }
                    )
                    # Receipt and actual versions/events commit together.
                    await self.artifacts.seal(
                        db,
                        PublicCompletion(
                            account_id=ctx.account_id,
                            id=uuid5(operation.id, "completion"),
                            operation_id=operation.id,
                            result=completed_result,
                            report=body.handoff,
                        ),
                    )
        except Exception as exc:
            # The transaction may have rolled back OR committed before its
            # acknowledgement failed. Only the durable receipt can distinguish
            # them. Never turn speculative event IDs into a success receipt,
            # and never overwrite an already committed immutable completion.
            # If this read also fails, propagate uncertainty for a later retry.
            async with self._session(ctx.account_id) as db:
                committed = await self._get(
                    db, uuid5(operation.id, "completion"), "public_completion"
                )
            if committed is None:
                failed_result = result.model_copy(update={"reason": safe_reason(exc)})
                await self._finish(operation, failed_result, report=body.handoff)
        return await self.get_maintenance_result(operation.id, ctx)

    async def _current(self, db, ident):
        return await db.fetchrow(
            """SELECT c.*,EXISTS(SELECT 1 FROM context_invalidations i
            WHERE i.context_id=c.id AND i.resolved_at IS NULL) has_causes
            FROM contexts c WHERE c.id=$1 AND c.account_id=current_setting('app.account_id')""",
            ident,
        )
