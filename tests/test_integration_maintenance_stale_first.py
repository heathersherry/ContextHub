"""S5 dedicated real PostgreSQL contracts; never uses db_pool/clean_db."""

import os
from contextlib import asynccontextmanager
from uuid import uuid4
from unittest.mock import AsyncMock
import asyncpg
import pytest
import pytest_asyncio
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository, ScopedRepo
from contexthub.models.context import ContextLevel
from contexthub.models.request import RequestContext
from contexthub.models.knowledge import ContractError
from contexthub.errors import ConflictError
from contexthub.propagation.contracts import MaintenancePolicy
from contexthub.propagation.executor import MaintenanceExecutor
from contexthub.services.acl_service import ACLService
from contexthub.services.masking_service import MaskingService
from contexthub.store.context_store import ContextStore
from knowledge_helpers import policy as retry_policy
from maintenance_helpers import make_graph


def config():
    return MaintenancePolicy(
        id="synthetic-only",
        page_size=2,
        chunk_size=21,
        content_level="l2_content",
        timeout_seconds=120,
        retry=retry_policy(),
        retry_read_codes=("connection", "timeout", "serialization"),
    )


@pytest_asyncio.fixture
async def s5_repo():
    url = os.environ.get("S5_TEST_DATABASE_URL")
    assert url == "postgresql://s2_s5_app@127.0.0.1:55467/contexthub_s2_s5", (
        "S5 explicit isolated URL required"
    )
    pool = await asyncpg.create_pool(
        url, min_size=2, max_size=8, init=init_pg_connection
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
        assert not row["rolsuper"] and not row["rolbypassrls"]
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "011"
        assert await conn.fetchval("SELECT current_database()") == "contexthub_s2_s5"
    try:
        yield PgRepository(pool)
    finally:
        await pool.close()


@asynccontextmanager
async def independent_read(account, writer_pid):
    url = os.environ["S5_TEST_DATABASE_URL"]
    assert url == "postgresql://s2_s5_app@127.0.0.1:55467/contexthub_s2_s5"
    conn = await asyncpg.connect(url)
    await init_pg_connection(conn)
    try:
        assert await conn.fetchval("SELECT pg_backend_pid()") != writer_pid
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.account_id',$1,true)", account)
            yield ScopedRepo(conn)
    finally:
        await conn.close()


async def changed(repo, *, extra_reason=False):
    account, source, report = await make_graph(repo)
    ctx = RequestContext(account_id=account, agent_id="s5-test")
    executor = MaintenanceExecutor(repo)
    unrelated = uuid4()
    async with repo.session(account) as db:
        await db.execute(
            "INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content) VALUES($1,$2,$3,'memory','datalake','filler')",
            unrelated,
            account,
            "ctx://filler/" + str(unrelated),
        )
        refs = await executor.register_graph(db, report.reference(), config(), ctx)
        watches = [await executor.artifacts.get(db, r) for r in refs]
        watch = next(w for w in watches if w.source == source)
        if extra_reason:
            previous = await db.fetchval(
                "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','test') RETURNING event_id",
                unrelated,
                account,
            )
            await db.execute(
                "INSERT INTO context_invalidations(context_id,cause_event_id,source_context_id,source_version,reason_hash) VALUES($1,$2,$3,1,$4)",
                watch.affected[0].context_id,
                previous,
                unrelated,
                "a" * 64,
            )
            await db.execute(
                "UPDATE contexts SET validity_status='superseded' WHERE id=$1",
                watch.affected[0].context_id,
            )
    async with repo.session(account) as db:
        await db.execute(
            "UPDATE contexts SET l2_content='Changed source',version=version+1 WHERE id=$1",
            source.context_id,
        )
        event = await db.fetchrow(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','s5-test') RETURNING *",
            source.context_id,
            account,
        )
        event = dict(event)
        event["_writer_pid"] = await db.fetchval("SELECT pg_backend_pid()")
    return executor, ctx, watch, event, unrelated


@pytest.mark.asyncio
async def test_committed_event_before_worker_blocks_all_carriers(s5_repo):
    executor, ctx, watch, event, unrelated = await changed(s5_repo, extra_reason=True)
    assert len(watch.seeds) == 4 and len(watch.affected) == 4
    store = ContextStore(ACLService(), MaskingService())
    # Separate pool connection: event already committed; worker has not run.
    async with independent_read(ctx.account_id, event["_writer_pid"]) as db:
        for target in watch.affected:
            uri = "ctx://proposition/" + str(target.context_id)
            with pytest.raises(ConflictError):
                await store.read(db, uri, ContextLevel.L2, ctx)
            assert "non-fresh" in await store.read(
                db, uri, ContextLevel.L2, ctx, include_stale=True
            )
        assert (
            await store.read(db, "ctx://filler/" + str(unrelated), ContextLevel.L2, ctx)
            == "filler"
        )
        assert (
            await db.fetchval(
                "SELECT validity_status FROM contexts WHERE id=$1",
                watch.affected[0].context_id,
            )
            == "superseded"
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM context_invalidations WHERE context_id=$1",
                watch.affected[0].context_id,
            )
            == 2
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM propagation_trace WHERE event_id=$1 AND trace_type='s5_barrier'",
                event["event_id"],
            )
            == 1
        )
    async with s5_repo.session("other-" + ctx.account_id) as db:
        assert not await db.fetch(
            "SELECT * FROM contexts WHERE id=$1", watch.affected[0].context_id
        )


@pytest.mark.asyncio
async def test_partial_initialization_never_releases_later_targets(s5_repo):
    executor, ctx, watch, event, _ = await changed(s5_repo)
    original = executor._prepare
    count = 0

    async def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise ContractError("injected_initialization_failure")
        return await original(*args)

    executor._prepare = fail_second
    with pytest.raises(ContractError, match="injected_initialization_failure"):
        await executor.begin_maintenance(event["event_id"], watch.reference())
    async with independent_read(ctx.account_id, event["_writer_pid"]) as db:
        assert (
            await db.fetchval(
                "SELECT count(*) FROM contexts WHERE id=ANY($1::uuid[]) AND status='active' AND validity_status='fresh'",
                [n.context_id for n in watch.affected],
            )
            == 0
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM knowledge_artifacts WHERE account_id=$1 AND kind='maintenance_frame'",
                ctx.account_id,
            )
            == 1
        )
    executor._prepare = original
    frames = await executor.begin_maintenance(event["event_id"], watch.reference())
    assert len(frames) == 4


from contexthub.propagation.evidence import EvidenceBuilder
from contexthub.propagation.contracts import InputRegistration, RequiredInput
from contexthub.models.knowledge import ContextVersion


async def prepared(repo):
    executor, ctx, watch, event, unrelated = await changed(repo)
    refs = await executor.begin_maintenance(event["event_id"], watch.reference())
    async with repo.session(ctx.account_id) as db:
        frames = [await executor.artifacts.get(db, r) for r in refs]
    return executor, ctx, watch, event, unrelated, frames


@pytest.mark.asyncio
async def test_evidence_manifest_history_boundary_and_m3_same_seal(s5_repo):
    executor, ctx, watch, event, unrelated, frames = await prepared(s5_repo)
    builder = EvidenceBuilder(executor)
    # Pick root carrier: no necessary semantic parents, unlike B/C/D.
    frame = next(f for f in frames if not f.expected_upstreams)
    async with s5_repo.session(ctx.account_id) as db:
        late = uuid4()
        await db.execute(
            "INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content) VALUES($1,$2,$3,'memory','datalake','late')",
            late,
            ctx.account_id,
            "ctx://late/" + str(late),
        )
    receipt = await builder.build_evidence(frame.reference(), "M2")
    assert receipt.complete and receipt.ready
    assert any(r.source.context_id == unrelated for r in receipt.required)
    assert not any(r.source.context_id == late for r in receipt.required)
    assert {r.role for r in receipt.required} >= {
        "before",
        "after",
        "target",
        "provenance",
    }
    original = builder._read_once
    builder._read_once = AsyncMock(
        side_effect=AssertionError("M3 must not read history")
    )
    assert await builder.reuse_m2_for_m3(receipt.reference()) == receipt.bundle
    builder._read_once.assert_not_called()
    async with s5_repo.session(ctx.account_id) as db:
        bundle = await executor.artifacts.get(db, receipt.bundle)
        assert (
            bundle.complete and bundle.required == bundle.read and not bundle.truncated
        )
        reread = await executor.artifacts.get(db, receipt.reference())
        assert reread == receipt
    builder._read_once = original


@pytest.mark.asyncio
async def test_upstream_unresolved_distinct_from_evidence_complete(s5_repo):
    executor, ctx, watch, event, _, frames = await prepared(s5_repo)
    frame = next(f for f in frames if f.expected_upstreams)
    receipt = await EvidenceBuilder(executor).build_evidence(frame.reference(), "M1")
    assert receipt.complete and not receipt.ready
    assert "upstream_unresolved" in receipt.reasons
    async with s5_repo.session(ctx.account_id) as db:
        assert (
            await executor.artifacts.get_work(db, ctx.account_id, frame.snapshot.id)
        ).status == "unresolved"
        assert (
            await db.fetchval(
                "SELECT count(*) FROM context_refresh_proofs WHERE account_id=$1",
                ctx.account_id,
            )
            == 0
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM maintenance_work_items WHERE account_id=$1 AND status IN ('updated','unchanged')",
                ctx.account_id,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_missing_registered_required_input_does_not_disappear(s5_repo):
    executor, ctx, watch, event, _ = await changed(s5_repo)
    missing = ContextVersion(context_id=uuid4(), version=1)
    async with s5_repo.session(ctx.account_id) as db:
        await executor.register_inputs(
            db,
            InputRegistration(
                account_id=ctx.account_id,
                id=uuid4(),
                target_id=watch.affected[0].context_id,
                inputs=(
                    RequiredInput(
                        source=missing,
                        role="rule",
                        rule_id="synthetic-rule",
                        rule_version="1",
                    ),
                ),
            ),
        )
    refs = await executor.begin_maintenance(event["event_id"], watch.reference())
    async with s5_repo.session(ctx.account_id) as db:
        frames = [await executor.artifacts.get(db, r) for r in refs]
    frame = next(f for f in frames if any(r.source == missing for r in f.required))
    result = await EvidenceBuilder(executor).build_evidence(frame.reference(), "M2")
    assert any(r.source == missing for r in result.required)
    assert not result.complete and not result.ready and result.bundle is None
    assert "required_source_missing_or_forbidden" in result.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["omit", "version", "cause", "registration", "provenance"]
)
async def test_omissions_and_drift_never_ready(s5_repo, mode):
    executor, ctx, watch, event, unrelated, frames = await prepared(s5_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    async with s5_repo.session(ctx.account_id) as db:
        snap = await executor.artifacts.get(db, frame.snapshot)
        if mode == "version":
            await db.execute(
                "UPDATE contexts SET version=version+1,l2_content='drift' WHERE id=$1",
                unrelated,
            )
        elif mode == "cause":
            cause = await db.fetchval(
                "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','test') RETURNING event_id",
                unrelated,
                ctx.account_id,
            )
            await db.execute(
                "INSERT INTO context_invalidations(context_id,cause_event_id,source_context_id,reason_hash) VALUES($1,$2,$3,$4)",
                snap.target.context_id,
                cause,
                unrelated,
                "b" * 64,
            )
        elif mode == "registration":
            await executor.register_inputs(
                db,
                InputRegistration(
                    account_id=ctx.account_id,
                    id=uuid4(),
                    target_id=snap.target.context_id,
                    inputs=(),
                ),
            )
        elif mode == "provenance":
            await db.execute(
                "INSERT INTO context_relations(context_id,related_context_id,relation_type) VALUES($1,$2,'materialized_from')",
                snap.target.context_id,
                unrelated,
            )
    result = await EvidenceBuilder(executor).build_evidence(
        frame.reference(), "M1", local_sources=() if mode == "omit" else None
    )
    assert not result.ready and not result.complete
    assert result.required == frame.required


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["recover", "persistent", "corrupt", "future"])
async def test_bounded_read_retry_and_each_attempt_admission(s5_repo, mode):
    executor, ctx, watch, event, _, frames = await prepared(s5_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    builder = EvidenceBuilder(executor)
    original = builder._read_once
    count = 0

    async def injected(db, f, w, slot):
        nonlocal count
        count += 1
        if mode in ("recover", "persistent") and (mode == "persistent" or count == 1):
            raise ConnectionError("secret must never enter logs")
        if mode == "corrupt":
            raise ContractError("source_version_mismatch")
        if mode == "future":
            from datetime import timedelta

            f = f.model_copy(
                update={"cutoff_at": slot.ingested_at - timedelta(seconds=1)}
            )
        return await original(db, f, w, slot)

    builder._read_once = injected
    result = await builder.build_evidence(frame.reference(), "M1")
    if mode == "recover":
        assert result.ready and result.complete
        assert count == len(result.required) + 1
    else:
        assert not result.ready and not result.complete
        assert count == (config().retry.max_attempts if mode == "persistent" else 1)
    async with s5_repo.session(ctx.account_id) as db:
        rows = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE account_id=$1", ctx.account_id
        )
        assert "secret must never" not in str(rows)
        assert any(r["payload"].get("local_work") for r in rows)
        assert all(
            r["payload"]["cost"]["amount"] is None
            for r in rows
            if r["payload"]["requested_model"] is None
        )


from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.propagation_engine import PropagationEngine
from contexthub.services.retrieval_service import RetrievalService
from contexthub.retrieval.router import RetrievalRouter
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.search import SearchRequest


@pytest.mark.asyncio
async def test_actual_event_dispatch_lifecycle_and_retrieval_barriers(s5_repo):
    executor, ctx, watch, event, unrelated = await changed(s5_repo)
    async with s5_repo.session(ctx.account_id) as db:
        await db.execute(
            "UPDATE contexts SET l0_content='s5needle' WHERE account_id=$1",
            ctx.account_id,
        )
    registry = AsyncMock()
    engine = PropagationEngine(
        s5_repo,
        s5_repo._pool,
        "unused",
        registry,
        LifecycleService(),
        maintenance_executor=executor,
    )
    claimed = await engine._claim_ready_events(str(event["context_id"]), 1)
    assert len(claimed) == 1
    await engine._process_claimed_event(claimed[0])
    registry.get_dep_rule.assert_not_called()
    store = ContextStore(ACLService(), MaskingService())
    service = RetrievalService(
        RetrievalRouter.default(),
        NoOpEmbeddingClient(),
        ACLService(),
        masking_service=MaskingService(),
    )
    async with s5_repo.session(ctx.account_id) as db:
        assert (
            await db.fetchval(
                "SELECT delivery_status FROM change_events WHERE event_id=$1",
                event["event_id"],
            )
            == "succeeded"
        )
        target = watch.affected[0]
        with pytest.raises(ConflictError):
            await LifecycleService().recover_from_stale(db, target.context_id, ctx)
        # Also exercise the cause barrier independent of the materialized validity flag.
        await db.execute(
            "UPDATE contexts SET status='active',validity_status='fresh' WHERE id=$1",
            target.context_id,
        )
        with pytest.raises(ConflictError):
            await store.read(
                db, "ctx://proposition/" + str(target.context_id), ContextLevel.L2, ctx
            )
        response = await service.search(
            db, SearchRequest(query="s5needle", top_k=20), ctx
        )
        assert not {
            "ctx://proposition/" + str(n.context_id) for n in watch.affected
        } & {r.uri for r in response.results}
        assert "ctx://filler/" + str(unrelated) in {r.uri for r in response.results}
        debug = await service.search(
            db, SearchRequest(query="s5needle", top_k=20, include_stale=True), ctx
        )
        assert all(
            r.validity_status != "fresh"
            for r in debug.results
            if r.uri
            in {"ctx://proposition/" + str(n.context_id) for n in watch.affected}
        )
        assert not any(
            "proposition" in u for u in await store.ls(db, "ctx://proposition", ctx)
        )
    # Restore only the test's deliberately altered flag, never a production fresh write.
    async with s5_repo.session(ctx.account_id) as db:
        await db.execute(
            "UPDATE contexts SET status='stale',validity_status='stale' WHERE id=$1",
            target.context_id,
        )


@pytest.mark.asyncio
async def test_pending_graph_protects_known_targets_and_stops(s5_repo):
    account, source, report = await make_graph(s5_repo, pending=True)
    assert report.status == "pending"
    executor = MaintenanceExecutor(s5_repo)
    ctx = RequestContext(account_id=account, agent_id="s5-test")
    async with s5_repo.session(account) as db:
        refs = await executor.register_graph(db, report.reference(), config(), ctx)
        watches = [await executor.artifacts.get(db, r) for r in refs]
        watch = next(w for w in watches if w.source == source)
    async with s5_repo.session(account) as db:
        await db.execute(
            "UPDATE contexts SET version=2,l2_content='new' WHERE id=$1",
            source.context_id,
        )
        event = await db.fetchval(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','test') RETURNING event_id",
            source.context_id,
            account,
        )
    frames = await executor.begin_maintenance(event, watch.reference())
    assert len(frames) == 4
    for ref in frames:
        result = await EvidenceBuilder(executor).build_evidence(ref, "M1")
        assert not result.ready and "graph_pending" in result.reasons


@pytest.mark.asyncio
async def test_immutable_and_exact_change_version(s5_repo):
    executor, ctx, watch, event, _ = await changed(s5_repo)
    async with s5_repo.session(ctx.account_id) as db:
        with pytest.raises(asyncpg.CheckViolationError):
            async with db._conn.transaction():
                await db.execute(
                    "UPDATE knowledge_artifacts SET sha256=$1 WHERE account_id=$2 AND id=$3",
                    "0" * 64,
                    ctx.account_id,
                    watch.id,
                )
        await db.execute(
            "UPDATE contexts SET version=3,l2_content='third' WHERE id=$1",
            watch.source.context_id,
        )
        next_event = await db.fetchval(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','test') RETURNING event_id",
            watch.source.context_id,
            ctx.account_id,
        )
    with pytest.raises(ContractError, match="change_version_drift"):
        await executor.begin_maintenance(next_event, watch.reference())
    async with s5_repo.session(ctx.account_id) as db:
        assert (
            await db.fetchval(
                "SELECT count(*) FROM contexts WHERE id=ANY($1::uuid[]) AND validity_status='fresh'",
                [n.context_id for n in watch.affected],
            )
            == 0
        )


@pytest.mark.asyncio
async def test_registered_rule_conflict_alternative_inputs_are_in_m1(s5_repo):
    executor, ctx, watch, event, unrelated = await changed(s5_repo)
    target = watch.affected[0]
    rule_source = ContextVersion(context_id=unrelated, version=1)

    class Rule:
        id = "fixture-only"
        version = "v1"

        def required_inputs(self, target):
            return (
                RequiredInput(
                    source=rule_source,
                    role="rule",
                    rule_id=self.id,
                    rule_version=self.version,
                ),
            )

    async with s5_repo.session(ctx.account_id) as db:
        await executor.register_rule_inputs(
            db, account_id=ctx.account_id, target=target, provider=Rule()
        )
        await executor.register_inputs(
            db,
            InputRegistration(
                account_id=ctx.account_id,
                id=uuid4(),
                target_id=target.context_id,
                inputs=(
                    RequiredInput(source=rule_source, role="conflict"),
                    RequiredInput(source=rule_source, role="alternative"),
                ),
                unresolved_conflict_ids=(uuid4(),),
            ),
        )
    refs = await executor.begin_maintenance(event["event_id"], watch.reference())
    async with s5_repo.session(ctx.account_id) as db:
        frames = [await executor.artifacts.get(db, r) for r in refs]
    frame = next(f for f in frames if any(r.role == "rule" for r in f.required))
    result = await EvidenceBuilder(executor).build_evidence(frame.reference(), "M1")
    assert {r.role for r in result.required} >= {
        "before",
        "after",
        "target",
        "provenance",
        "rule",
        "conflict",
        "alternative",
    }
    assert (
        result.complete
        and not result.ready
        and "registered_conflict_unresolved" in result.reasons
    )


@pytest.mark.asyncio
async def test_permission_loss_stops_without_body_read(s5_repo):
    executor, ctx, watch, event, unrelated, frames = await prepared(s5_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    async with s5_repo.session(ctx.account_id) as db:
        await db.execute(
            "UPDATE contexts SET scope='agent',owner_space='different-agent' WHERE id=$1",
            unrelated,
        )
    builder = EvidenceBuilder(executor)
    builder._read_once = AsyncMock(
        side_effect=AssertionError("permission before body read")
    )
    receipt = await builder.build_evidence(frame.reference(), "M2")
    assert not receipt.ready and "source_permission_denied" in receipt.reasons
    builder._read_once.assert_not_called()


@pytest.mark.asyncio
async def test_sidecar_change_cannot_change_runtime_decision(s5_repo):
    executor, ctx, watch, event, _, frames = await prepared(s5_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    builder = EvidenceBuilder(executor)
    sidecar = {"task_type": "Cas", "hop": 1, "gold": "old", "path": ["a", "b"]}
    first = await builder.build_evidence(frame.reference(), "M2")
    sidecar.update(task_type="Abs", hop=99, gold="different", path=[])
    second = await builder.build_evidence(frame.reference(), "M2")
    # The scorer-only object is never a core argument; actual evidence is read twice.
    assert (
        first.required == second.required
        and first.read_ordinals == second.read_ordinals
    )
    assert (first.complete, first.ready, first.reasons) == (
        second.complete,
        second.ready,
        second.reasons,
    )
    async with s5_repo.session(ctx.account_id) as db:
        a = await executor.artifacts.get(db, first.bundle)
        b = await executor.artifacts.get(db, second.bundle)
        assert a.required == b.required and a.read == b.read
        assert (
            await db.fetchval(
                "SELECT count(*) FROM contexts WHERE id=ANY($1::uuid[]) AND validity_status='fresh'",
                [n.context_id for n in watch.affected],
            )
            == 0
        )


@pytest.mark.asyncio
async def test_mixed_content_levels_keep_original_span(s5_repo):
    executor, ctx, watch, event, unrelated = await changed(s5_repo)
    async with s5_repo.session(ctx.account_id) as db:
        # Make a genuine new content version carrying separate original text layers.
        await db.execute(
            "UPDATE contexts SET version=2,l0_content='short rule',l2_content='long filler body' WHERE id=$1",
            unrelated,
        )
        await executor.register_inputs(
            db,
            InputRegistration(
                account_id=ctx.account_id,
                id=uuid4(),
                target_id=watch.affected[0].context_id,
                inputs=(
                    RequiredInput(
                        source=ContextVersion(context_id=unrelated, version=2),
                        content_level="l0_content",
                        role="rule",
                        rule_id="r",
                        rule_version="1",
                        start=0,
                        end=5,
                    ),
                ),
            ),
        )
    refs = await executor.begin_maintenance(event["event_id"], watch.reference())
    async with s5_repo.session(ctx.account_id) as db:
        frames = [await executor.artifacts.get(db, r) for r in refs]
    frame = next(f for f in frames if any(r.role == "rule" for r in f.required))
    receipt = await EvidenceBuilder(executor).build_evidence(frame.reference(), "M2")
    assert receipt.complete
    async with s5_repo.session(ctx.account_id) as db:
        bundle = await executor.artifacts.get(db, receipt.bundle)
        assert any(
            e.evidence.excerpt == "short" and e.evidence.provenance == ("rule",)
            for e in bundle.read
        )
        corrupted = receipt.model_copy(
            update={"id": uuid4(), "pages": receipt.pages + receipt.pages[:1]}
        )
        with pytest.raises(ContractError, match="incomplete_manifest"):
            await executor.artifacts.seal(db, corrupted)


@pytest.mark.asyncio
async def test_corrupt_graph_basis_cannot_be_resnapshotted_as_valid(s5_repo):
    executor, ctx, watch, event, _ = await changed(s5_repo)
    async with s5_repo.session(ctx.account_id) as db:
        await db.execute(
            "UPDATE context_versions SET l2_content='corrupt historical source' WHERE context_id=$1 AND version=1",
            watch.source.context_id,
        )
    with pytest.raises(ContractError, match="source_version_mismatch"):
        await executor.begin_maintenance(event["event_id"], watch.reference())
    async with s5_repo.session(ctx.account_id) as db:
        audit = await db.fetchrow(
            "SELECT payload FROM propagation_trace WHERE event_id=$1 AND trace_type='s5_required_manifest' ORDER BY trace_id LIMIT 1",
            event["event_id"],
        )
        assert {r["role"] for r in audit["payload"]["required"]} >= {
            "before",
            "after",
            "target",
        }
        assert (
            await db.fetchval(
                "SELECT count(*) FROM contexts WHERE id=ANY($1::uuid[]) AND validity_status='fresh'",
                [n.context_id for n in watch.affected],
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "drop_page",
        "drop_block",
        "duplicate_page",
        "truncated_span",
        "old_for_new",
        "reverse_blocks",
    ],
)
async def test_complete_receipt_binds_actual_sealed_bundle(s5_repo, fault):
    """S5-R1: inject only bundle entries, retaining the independent plan/progress."""
    from contexthub.models.knowledge import text_hash
    from contexthub.models.maintenance import EvidenceBundle

    executor, ctx, watch, event, _, frames = await prepared(s5_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    builder = EvidenceBuilder(executor)
    seal = executor.artifacts.seal
    injected = []

    async def damaged_bundle(db, obj):
        if isinstance(obj, EvidenceBundle):
            rows = list(obj.read)
            if fault == "drop_page":
                assert any(r.page == 1 for r in rows)
                rows = [r for r in rows if r.page != 1]
            elif fault == "drop_block":
                rows = rows[:-1]
            elif fault == "duplicate_page":
                rows = [r.model_copy(update={"page": 0}) for r in rows]
            elif fault == "reverse_blocks":
                rows = [
                    r.model_copy(update={"ordinal": i})
                    for i, r in enumerate(reversed(rows))
                ]
            elif fault == "truncated_span":
                i = next(i for i, r in enumerate(rows) if len(r.evidence.excerpt) > 1)
                e = rows[i].evidence
                rows[i] = rows[i].model_copy(
                    update={
                        "evidence": e.model_copy(
                            update={
                                "end": e.end - 1,
                                "excerpt": e.excerpt[:-1],
                                "excerpt_sha256": text_hash(e.excerpt[:-1]),
                            }
                        )
                    }
                )
            else:
                old = next(s for s in frame.history if s.source == frame.old)
                text = await builder.evidence.read_source(
                    db, await executor.artifacts.get(db, old.seal)
                )
                for i, row in enumerate(rows):
                    if row.evidence.provenance == ("after",):
                        e = row.evidence
                        value = text[e.start : e.end]
                        rows[i] = row.model_copy(
                            update={
                                "evidence": e.model_copy(
                                    update={
                                        "source": old.seal,
                                        "excerpt": value,
                                        "excerpt_sha256": text_hash(value),
                                    }
                                )
                            }
                        )
            obj = obj.model_copy(update={"required": tuple(rows), "read": tuple(rows)})
            injected.append(obj)
        return await seal(db, obj)

    executor.artifacts.seal = damaged_bundle
    with pytest.raises(ContractError, match="bundle_manifest_mismatch"):
        await builder.build_evidence(frame.reference(), "M2")
    assert len(injected) == 1
    async with s5_repo.session(ctx.account_id) as db:
        # Receipt rejection rolls back the bundle in the same short transaction.
        assert not await db.fetchval(
            "SELECT 1 FROM knowledge_artifacts WHERE id=$1", injected[0].id
        )
        assert not await db.fetchval(
            "SELECT 1 FROM knowledge_artifacts WHERE kind='maintenance_receipt'"
        )
        assert not await db.fetchval(
            "SELECT 1 FROM contexts WHERE id=ANY($1::uuid[]) AND validity_status='fresh'",
            [n.context_id for n in watch.affected],
        )
    # A controlled fault does not destroy the legal success path on this frame.
    executor.artifacts.seal = seal
    receipt = await builder.build_evidence(frame.reference(), "M2")
    assert receipt.complete and receipt.ready
    builder.evidence.read_source = AsyncMock(
        side_effect=AssertionError("M3 history read")
    )
    assert await builder.reuse_m2_for_m3(receipt.reference()) == receipt.bundle
    builder.evidence.read_source.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [False, True])
async def test_l2_body_and_validity_share_snapshot_during_committed_event(
    s5_repo, debug
):
    """S5-R2: commit a real source write between revalidation and L2 SQL."""
    from contexthub.llm.base import NoOpEmbeddingClient
    from contexthub.models.search import SearchRequest
    from contexthub.retrieval.router import RetrievalRouter
    from contexthub.services.retrieval_service import RetrievalService

    account, source, report = await make_graph(s5_repo)
    ctx = RequestContext(account_id=account, agent_id="system", expected_version=1)
    executor = MaintenanceExecutor(s5_repo)
    filler = uuid4()
    async with s5_repo.session(account) as db:
        refs = await executor.register_graph(db, report.reference(), config(), ctx)
        watches = [await executor.artifacts.get(db, ref) for ref in refs]
        watch = next(w for w in watches if w.source == source)
        source_uri = await db.fetchval(
            "SELECT uri FROM contexts WHERE id=$1", source.context_id
        )
        await db.execute(
            "INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content) VALUES($1,$2,$3,'memory','datalake','filler')",
            filler,
            account,
            "ctx://filler/" + str(filler),
        )
        await db.execute(
            "UPDATE contexts SET l0_content='revisionneedle' WHERE account_id=$1",
            account,
        )
    store = ContextStore(ACLService(), MaskingService())
    service = RetrievalService(
        RetrievalRouter.default(),
        NoOpEmbeddingClient(),
        ACLService(),
        masking_service=MaskingService(),
    )

    class Interleave:
        def __init__(self, db):
            self.db, self.did = db, False

        def __getattr__(self, name):
            return getattr(self.db, name)

        async def fetch(self, sql, *args):
            if "SELECT id, version, l2_content" in sql and not self.did:
                self.did = True
                reader_pid = await self.db.fetchval("SELECT pg_backend_pid()")
                async with s5_repo.session(account) as writer:
                    assert (
                        await writer.fetchval("SELECT pg_backend_pid()") != reader_pid
                    )
                    assert (
                        await store.write(
                            writer, source_uri, ContextLevel.L2, "changed", ctx
                        )
                        == 2
                    )
            return await self.db.fetch(sql, *args)

    async with s5_repo.session(account) as raw:
        db = Interleave(raw)
        response = await service.search(
            db,
            SearchRequest(
                query="revisionneedle",
                level=ContextLevel.L2,
                top_k=100,
                include_stale=debug,
            ),
            ctx,
        )
        assert db.did
        affected = {"ctx://proposition/" + str(n.context_id) for n in watch.affected}
        found = [r for r in response.results if r.uri in affected]
        if debug:
            assert len(found) == len(affected)
            assert all(r.validity_status == "stale" for r in found)
        else:
            assert not found
        unrelated = next(
            r for r in response.results if r.uri == "ctx://filler/" + str(filler)
        )
        assert unrelated.validity_status == "fresh" and unrelated.l2_content == "filler"
        assert await raw.fetchval(
            "SELECT count(*) FROM contexts WHERE id=ANY($1::uuid[]) AND validity_status='stale'",
            [n.context_id for n in watch.affected],
        ) == len(affected)
