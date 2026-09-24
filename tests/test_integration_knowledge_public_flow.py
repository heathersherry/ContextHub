"""S7 ordinary-user flow through real lifespan, SDK, S3–S6 and PostgreSQL."""

import os
import sys
import json
from pathlib import Path
from uuid import uuid4
import pytest
import pytest_asyncio
import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk/src"))
from contexthub.main import create_app, ApplicationDependencies
from contexthub.config import Settings
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub_sdk import (
    ContextHubClient,
    KnowledgeIngestRequest,
    KnowledgeMaintainRequest,
)
from s7_helpers import configuration, secrets, SyntheticModels

DSN = "postgresql://s2_s7_app@127.0.0.1:55557/contexthub_s2_s7"


class OfflineChat:
    async def complete(self, *a, **k):
        raise AssertionError("legacy chat must not be called")

    async def close(self):
        pass


@pytest_asyncio.fixture
async def public_app(tmp_path, request):
    assert os.environ.get("S7_TEST_DATABASE_URL") == DSN
    c = await asyncpg.connect(DSN)
    assert await c.fetchval("SELECT current_database()") == "contexthub_s2_s7"
    assert await c.fetchval("SHOW port") == "55557"
    assert await c.fetchval("SHOW server_encoding") == "UTF8"
    assert await c.fetchval("SELECT version_num FROM alembic_version") == "011"
    role = await c.fetchrow(
        "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
    )
    assert not role["rolsuper"] and not role["rolbypassrls"]
    await c.close()
    models = SyntheticModels()
    cfg = tmp_path / "knowledge.json"
    config = configuration().model_dump(mode="json")
    config["discovery"]["search"]["strategy"] = getattr(
        request, "param", "full-history"
    )
    cfg.write_text(json.dumps(config))
    app = create_app(
        Settings(
            _env_file=None,
            database_url=DSN,
            api_key="s7-local",
            knowledge_enabled=True,
            knowledge_config_path=str(cfg),
            propagation_enabled=False,
            lifecycle_enabled=False,
        ),
        dependencies=ApplicationDependencies(
            embedding_factory=lambda s: NoOpEmbeddingClient(),
            chat_factory=lambda s: OfflineChat(),
            knowledge_transport=models.transport,
            knowledge_secrets=secrets(),
        ),
    )
    async with app.router.lifespan_context(app):
        yield app, models


def client(app, account, agent="reader"):
    return ContextHubClient(
        url="http://local",
        api_key="s7-local",
        account_id=account,
        agent_id=agent,
        transport=httpx.ASGITransport(app=app),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["updated", "unchanged", "unresolved"])
async def test_public_real_flow(public_app, outcome):
    app, models = public_app
    models.outcome = outcome
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        ingested = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(
                request_id=uuid4(),
                content="A: allowance 500|B: uses A for reimbursement",
            )
        )
        assert ingested.maintenance_ready, ingested
        assert ingested.graph_status == "deliverable" and len(ingested.nodes) == 2
        await sdk.context.update(
            ingested.source.uri,
            expected_version=1,
            l2_content="A: allowance 400|B: uses A for reimbursement",
        )
        state = await sdk.knowledge.get_maintenance_result(ingested.operation_id)
        assert state.event_ids
        assert not any(n.fresh for n in state.nodes)
        result = await sdk.knowledge.maintain(
            KnowledgeMaintainRequest(
                request_id=uuid4(),
                event_id=state.event_ids[-1],
                graph_id=state.graph_id,
            )
        )
        assert result.status == "complete", result
        assert result.graph_status == "deliverable" and result.maintenance_ready
        assert result.watch_ids
        assert any(w.status == outcome for w in result.works), result
        if outcome != "unresolved":
            assert all(n.fresh for n in result.nodes), result
        else:
            assert not any(n.fresh for n in result.nodes)
        from contexthub_sdk import ContextLevel, ConflictError

        for node in result.nodes:
            if outcome == "unresolved":
                with pytest.raises(ConflictError):
                    await sdk.context.read(node.uri, level=ContextLevel.L2)
            else:
                assert (await sdk.context.read(node.uri, level=ContextLevel.L2)).content
        count = len(models.calls)
        queried = await sdk.knowledge.get_maintenance_result(result.operation_id)
        again = await sdk.knowledge.maintain(
            KnowledgeMaintainRequest(
                request_id=uuid4(),
                event_id=state.event_ids[-1],
                graph_id=state.graph_id,
            )
        )
        assert len(models.calls) == count
        assert queried == again
        if outcome == "updated":
            async with app.state.repo.session(account) as db:
                child_id = await db.fetchval(
                    "SELECT event_id FROM change_events WHERE metadata->>'s6_execution_id'=$1 LIMIT 1",
                    str(result.execution_id),
                )
            assert child_id
            with pytest.raises(ConflictError):
                await sdk.knowledge.maintain(
                    KnowledgeMaintainRequest(
                        request_id=uuid4(), event_id=child_id, graph_id=state.graph_id
                    )
                )
            assert len(models.calls) == count


@pytest.mark.asyncio
async def test_two_hop_exact_versions_and_sidecar_independence(public_app):
    app, models = public_app
    traces = []
    for sidecar in ({"gold": "wrong", "hop": 9}, {"gold": "anything", "hop": 0}):
        # Sidecar is intentionally never submitted to the public runtime.
        account = "s7-" + str(uuid4())
        start = len(models.calls)
        async with client(app, account) as sdk:
            result = await sdk.knowledge.ingest(
                KnowledgeIngestRequest(
                    request_id=uuid4(),
                    content="A: allowance 500|B: uses A for reimbursement|C: uses B in report",
                )
            )
            assert result.maintenance_ready, result
            await sdk.context.update(
                result.source.uri,
                expected_version=1,
                l2_content="A: allowance 400|B: uses A for reimbursement|C: uses B in report",
            )
            state = await sdk.knowledge.get_maintenance_result(result.operation_id)
            result = await sdk.knowledge.maintain(
                KnowledgeMaintainRequest(
                    request_id=uuid4(),
                    event_id=state.event_ids[-1],
                    graph_id=state.graph_id,
                )
            )
            assert result.status == "complete", result
            assert len([w for w in result.works if w.status == "updated"]) == 3, result
            assert all(n.fresh and n.version == 2 for n in result.nodes)
            async with app.state.repo.session(account) as db:
                frames = await db.fetch(
                    "SELECT payload FROM knowledge_artifacts WHERE kind='maintenance_frame'"
                )
                replacements = [
                    r["payload"]
                    for r in frames
                    if any(
                        v["version"] == 2 for v in r["payload"]["expected_upstreams"]
                    )
                ]
                assert len(replacements) == 2
            traces.append(
                (
                    [c["model"] for c in models.calls[start:]],
                    sorted(
                        (w.status, w.current_version, w.fresh) for w in result.works
                    ),
                )
            )
    assert traces[0] == traces[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["located", "unrelated", "pending"])
async def test_raw_change_locates_real_old_new_or_abstains(public_app, location):
    app, models = public_app
    models.location = location
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        original = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        changed = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(
                request_id=uuid4(),
                mode="change",
                content="The allowance for A is now A: allowance 400",
            )
        )
        assert changed.location_status == location, changed
        async with app.state.repo.session(account) as db:
            source = await db.fetchrow(
                "SELECT version,l2_content FROM contexts WHERE id=$1",
                original.source.context_id,
            )
            if location == "located":
                assert (
                    source["version"] == 2
                    and source["l2_content"] == "A: allowance 400"
                )
                assert changed.event_ids
                event = await db.fetchrow(
                    "SELECT * FROM change_events WHERE event_id=$1",
                    changed.event_ids[-1],
                )
                assert (
                    event["context_id"] == original.source.context_id
                    and event["source_version"] == 2
                )
                assert event["metadata"]["s5_watches"]
            else:
                assert source["version"] == 1
                assert not changed.event_ids


@pytest.mark.asyncio
async def test_permissions_conflicts_pending_and_no_public_proof(public_app):
    from contexthub_sdk import ForbiddenError, NotFoundError, ConflictError

    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        request = KnowledgeIngestRequest(
            request_id=uuid4(), content="A: limit 500|B: uses A"
        )
        result = await sdk.knowledge.ingest(request)
        count = len(models.calls)
        assert await sdk.knowledge.ingest(request) == result
        with pytest.raises(ConflictError):
            await sdk.knowledge.ingest(
                request.model_copy(update={"content": "different"})
            )
        async with client(app, account, "other") as other:
            with pytest.raises(ForbiddenError):
                await other.knowledge.get_maintenance_result(result.operation_id)
        async with client(app, "other-" + account) as other:
            with pytest.raises(NotFoundError):
                await other.knowledge.get_maintenance_result(result.operation_id)
        with pytest.raises(ConflictError):
            await sdk.context.update(
                result.source.uri, expected_version=99, l2_content="A: limit 400"
            )
        assert len(models.calls) == count
        await sdk.context.update(
            result.source.uri, expected_version=1, l2_content="A: limit 400"
        )
        state = await sdk.knowledge.get_maintenance_result(result.operation_id)
        body = KnowledgeMaintainRequest(
            request_id=uuid4(), event_id=state.event_ids[-1], graph_id=state.graph_id
        )
        async with client(app, account, "other") as other:
            with pytest.raises(ForbiddenError):
                await other.knowledge.maintain(body)
        for key in (
            "proof",
            "PASS",
            "fresh",
            "affected_targets",
            "cause_ids",
            "gold",
            "hop",
            "task_type",
            "question",
            "path_labels",
            "account_id",
            "agent_id",
        ):
            response = await sdk._http.post(
                "/api/v1/knowledge/maintain",
                json={**body.model_dump(mode="json"), key: True},
            )
            assert response.status_code == 422
        assert len(models.calls) == count
        models.pending = True
        pending = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="D: note|E: independent")
        )
        assert pending.status == "pending" and not pending.maintenance_ready


@pytest.mark.asyncio
async def test_background_delivery_has_no_generator_responsibility(public_app):
    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        result = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: limit 500|B: uses A")
        )
        await sdk.context.update(
            result.source.uri, expected_version=1, l2_content="A: limit 400"
        )
        state = await sdk.knowledge.get_maintenance_result(result.operation_id)
        count = len(models.calls)
        await app.state.propagation_engine.drain_once(
            context_id=str(result.source.context_id)
        )
        await app.state.propagation_engine.drain_once(
            context_id=str(result.source.context_id)
        )
        assert len(models.calls) == count
        maintained = await sdk.knowledge.maintain(
            KnowledgeMaintainRequest(
                request_id=uuid4(),
                event_id=state.event_ids[-1],
                graph_id=state.graph_id,
            )
        )
        assert maintained.event_delivery == "succeeded"
        assert all(n.fresh for n in maintained.nodes)
        count = len(models.calls)
        await app.state.propagation_engine.drain_once(
            context_id=str(result.source.context_id)
        )
        await app.state.propagation_engine.drain_once(
            context_id=str(result.source.context_id)
        )
        assert len(models.calls) == count


@pytest.mark.asyncio
async def test_history_batches_share_extraction_and_readonly_service_equivalence(
    public_app,
):
    from contexthub.models.request import RequestContext

    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        a = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        b = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(
                request_id=uuid4(), content="B: uses A for reimbursement"
            )
        )
        assert b.maintenance_ready and len(b.nodes) == 2, b
        assert sum(c["model"] == "extract" for c in models.calls) == 2
        ctx = RequestContext(account_id=account, agent_id="reader")
        direct = await app.state.knowledge_service.get_maintenance_result(
            b.operation_id, ctx
        )
        assert direct.model_dump(mode="json") == b.model_dump(mode="json")
        async with app.state.repo.session(account) as db:
            before = await db.fetchval("SELECT count(*) FROM execution_attempts")
            reports = await db.fetch(
                "SELECT payload FROM knowledge_artifacts WHERE kind='discovery_report' ORDER BY created_at"
            )
            assert len(reports) == 2
            assert reports[1]["payload"]["prior_extractions"] == [
                reports[0]["payload"]["shared_extraction"]
            ]
        count = len(models.calls)
        await sdk.knowledge.get_maintenance_result(a.operation_id)
        await sdk.knowledge.get_maintenance_result(b.operation_id)
        async with app.state.repo.session(account) as db:
            assert (
                await db.fetchval("SELECT count(*) FROM execution_attempts") == before
            )
        assert len(models.calls) == count


@pytest.mark.asyncio
async def test_ambiguous_identical_sources_not_overwritten(public_app):
    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        first = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        second = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        models.location = "located"
        changed = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(
                request_id=uuid4(), mode="change", content="A is now A: allowance 400"
            )
        )
        assert changed.location_status == "pending" and changed.status == "pending"
        assert changed.reason == "change_location_ambiguous", changed
        async with app.state.repo.session(account) as db:
            rows = await db.fetch(
                "SELECT version FROM contexts WHERE id=ANY($1::uuid[])",
                [first.source.context_id, second.source.context_id],
            )
            assert [r["version"] for r in rows] == [1, 1]


@pytest.mark.asyncio
async def test_current_acl_mask_denial_and_now_nonfresh_after_historical_update(
    public_app,
):
    from contexthub_sdk import ForbiddenError

    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        ingested = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        await sdk.context.update(
            ingested.source.uri, expected_version=1, l2_content="A: allowance 400"
        )
        state = await sdk.knowledge.get_maintenance_result(ingested.operation_id)
        result = await sdk.knowledge.maintain(
            KnowledgeMaintainRequest(
                request_id=uuid4(),
                event_id=state.event_ids[-1],
                graph_id=state.graph_id,
            )
        )
        assert result.works[0].status == "updated" and result.works[0].fresh
        async with app.state.repo.session(account) as db:
            # Ordinary later invalidation; query must not report historical proof as current freshness.
            await db.execute(
                "UPDATE contexts SET validity_status='stale' WHERE id=$1",
                result.works[0].target_id,
            )
        queried = await sdk.knowledge.get_maintenance_result(result.operation_id)
        assert queried.works[0].status == "updated" and not queried.works[0].fresh
        async with app.state.repo.session(account) as db:
            await db.execute(
                """INSERT INTO access_policies(account_id,resource_uri_pattern,principal,effect,actions,created_by)
                VALUES($1,$2,'reader','deny',ARRAY['read'],'reader')""",
                account,
                ingested.source.uri,
            )
        count = len(models.calls)
        with pytest.raises(ForbiddenError):
            await sdk.knowledge.get_maintenance_result(result.operation_id)
        assert len(models.calls) == count


@pytest.mark.asyncio
async def test_concurrent_manual_claim_cannot_duplicate_generation(public_app):
    import asyncio

    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        ingested = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        await sdk.context.update(
            ingested.source.uri, expected_version=1, l2_content="A: allowance 400"
        )
        state = await sdk.knowledge.get_maintenance_result(ingested.operation_id)
        before = len(models.calls)
        requests = [
            KnowledgeMaintainRequest(
                request_id=uuid4(),
                event_id=state.event_ids[-1],
                graph_id=state.graph_id,
            )
            for _ in range(2)
        ]
        results = await asyncio.gather(*(sdk.knowledge.maintain(r) for r in requests))
        assert results[0].operation_id == results[1].operation_id
        result = await sdk.knowledge.get_maintenance_result(results[0].operation_id)
        assert result.works[0].status == "updated"
        assert [c["model"] for c in models.calls[before:]] == ["base", "verifier"]


@pytest.mark.asyncio
async def test_partial_response_failure_does_not_replay_committed_work(
    public_app, monkeypatch
):
    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        ingested = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        await sdk.context.update(
            ingested.source.uri, expected_version=1, l2_content="A: allowance 400"
        )
        state = await sdk.knowledge.get_maintenance_result(ingested.operation_id)
        svc = app.state.knowledge_service
        original = svc._finish

        async def fail_after_commit(op, result, **kw):
            if op.operation == "maintain":
                raise RuntimeError("synthetic response persistence failure")
            return await original(op, result, **kw)

        monkeypatch.setattr(svc, "_finish", fail_after_commit)
        request = KnowledgeMaintainRequest(
            request_id=uuid4(), event_id=state.event_ids[-1], graph_id=state.graph_id
        )
        with pytest.raises(RuntimeError):
            await sdk.knowledge.maintain(request)
        count = len(models.calls)
        monkeypatch.setattr(svc, "_finish", original)
        result = await sdk.knowledge.maintain(
            request.model_copy(update={"request_id": uuid4()})
        )
        assert result.works[0].status == "updated" and result.works[0].fresh
        assert result.reason == "execution_incomplete"
        assert len(models.calls) == count


@pytest.mark.asyncio
async def test_real_worker_scheduler_lifecycle_and_zero_network(tmp_path, monkeypatch):
    async def forbidden_network(*args, **kwargs):
        raise AssertionError("real HTTP transport is forbidden")

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network
    )

    class Embedding(NoOpEmbeddingClient):
        closed = False

        async def close(self):
            self.closed = True

    class Chat(OfflineChat):
        closed = False

        async def close(self):
            self.closed = True

    embedding, chat, models = Embedding(), Chat(), SyntheticModels()
    path = tmp_path / "knowledge.json"
    path.write_text(configuration().model_dump_json())
    app = create_app(
        Settings(
            _env_file=None,
            database_url=DSN,
            api_key="s7-local",
            knowledge_enabled=True,
            knowledge_config_path=str(path),
            propagation_enabled=True,
            lifecycle_enabled=True,
        ),
        dependencies=ApplicationDependencies(
            embedding_factory=lambda s: embedding,
            chat_factory=lambda s: chat,
            knowledge_transport=models.transport,
            knowledge_secrets=secrets(),
        ),
    )
    async with app.router.lifespan_context(app):
        assert app.state.propagation_engine._running
        assert app.state.lifecycle_scheduler._running
    assert embedding.closed and chat.closed
    assert app.state.knowledge_service.runner.base.client._client.is_closed
    assert not app.state.propagation_engine._running
    assert app.state.lifecycle_scheduler._task is None
    assert models.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["A at another company has allowance 300", "B: lunch at noon"]
)
async def test_raw_distinct_entity_or_independent_fact_does_not_update(
    public_app, text
):
    app, models = public_app
    models.location = "pending" if text.startswith("A") else "unrelated"
    async with client(app, "s7-" + str(uuid4())) as sdk:
        old = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        change = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), mode="change", content=text)
        )
        assert change.location_status == models.location and not change.event_ids
        assert (
            await sdk.knowledge.get_maintenance_result(old.operation_id)
        ).source.version == 1


@pytest.mark.asyncio
async def test_source_drift_during_extraction_never_registers_ready(public_app):
    app, models = public_app
    account = "s7-" + str(uuid4())
    old_handler = models.transport.handler
    changed = False

    async def handler(request):
        nonlocal changed
        body = json.loads(request.content)
        if body["model"] == "extract" and not changed:
            changed = True
            async with app.state.repo.session(account) as db:
                uri = await db.fetchval(
                    "SELECT uri FROM contexts WHERE account_id=$1 AND uri LIKE 'ctx://%/knowledge/%'",
                    account,
                )
            async with client(app, account) as sdk:
                await sdk.context.update(
                    uri, expected_version=1, l2_content="new content during extraction"
                )
        return old_handler(request)

    models.transport.handler = handler
    async with client(app, account) as sdk:
        result = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
        )
        assert changed and result.status == "pending" and not result.maintenance_ready
        assert result.source.version == 2
        assert not result.watch_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("public_app", ["selective"], indirect=True)
async def test_selective_factory_and_reverse_batch_order(public_app):
    app, models = public_app
    account = "s7-" + str(uuid4())
    async with client(app, account) as sdk:
        result = await sdk.knowledge.ingest(
            KnowledgeIngestRequest(
                request_id=uuid4(),
                content="C: uses B in report|B: uses A for reimbursement|A: allowance 500",
            )
        )
        assert result.maintenance_ready and len(result.nodes) == 3
        async with app.state.repo.session(account) as db:
            graph = await db.fetchval(
                "SELECT payload FROM knowledge_artifacts WHERE id=$1", result.graph_id
            )
            assert len(graph["edges"]) == 2
        # Actual graph remains sealed by the core; public input was just reversed raw text.
        assert (
            app.state.knowledge_service.config.discovery.search.strategy == "selective"
        )


@pytest.mark.asyncio
async def test_legacy_memory_and_context_sdk_remain_usable(public_app):
    from contexthub_sdk import ContextLevel

    app, models = public_app
    async with client(app, "s7-" + str(uuid4())) as sdk:
        memory = await sdk.memory.add(content="An ordinary legacy note")
        assert any(m.uri == memory.uri for m in await sdk.memory.list())
        assert (
            await sdk.context.read(memory.uri, level=ContextLevel.L2)
        ).content == "An ordinary legacy note"
        await sdk.context.update(
            memory.uri, expected_version=1, l2_content="Changed legacy note"
        )
        assert (
            await sdk.context.read(memory.uri, level=ContextLevel.L2)
        ).content == "Changed legacy note"
        assert models.calls == []
