"""S7-R1: real SDK/ASGI/core cancellation, never a replacement service.

Uses the explicit isolated public_app fixture; redirect that fixture's DSN for a
new review instance, preserving its role/encoding/schema assertions.
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from contexthub_sdk import KnowledgeIngestRequest, KnowledgeMaintainRequest
import test_integration_knowledge_public_flow as public

public_app = public.public_app
client = public.client


def save(name, value):
    if os.environ.get("REVIEW_OUT"):
        p = Path(os.environ["REVIEW_OUT"]) / (name + ".json")
        assert not p.exists()
        p.write_text(json.dumps(value, indent=2, default=str))


async def prepared(sdk, *, content="A: allowance 500|B: uses A|C: uses B"):
    ingestion = await sdk.knowledge.ingest(
        KnowledgeIngestRequest(request_id=uuid4(), content=content)
    )
    assert ingestion.maintenance_ready
    await sdk.context.update(
        ingestion.source.uri,
        expected_version=1,
        l2_content=content.replace("500", "400"),
    )
    state = await sdk.knowledge.get_maintenance_result(ingestion.operation_id)
    return KnowledgeMaintainRequest(
        request_id=uuid4(), event_id=state.event_ids[-1], graph_id=state.graph_id
    )


def operation(account, request):
    return (
        uuid5(
            NAMESPACE_URL,
            f"knowledge-maintain:{account}:{request.event_id}:{request.graph_id}",
        )
        if isinstance(request, KnowledgeMaintainRequest)
        else uuid5(NAMESPACE_URL, f"knowledge:{account}:reader:{request.request_id}")
    )


async def public_snapshot(app, sdk, ident, account):
    result = await sdk.knowledge.get_maintenance_result(ident)
    async with app.state.repo.session(account) as db:
        records = [
            dict(r)
            for r in await db.fetch(
                "SELECT payload FROM execution_attempts WHERE execution_id=$1", ident
            )
        ]
        works = [
            dict(r) for r in await db.fetch("SELECT * FROM maintenance_work_items")
        ]
    return result, dict(
        result=result.model_dump(mode="json"), attempts=records, work_rows=works
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["extract", "base", "verifier"])
async def test_public_cancel_persists_terminal_state_without_replay(
    public_app, boundary
):
    app, models = public_app
    account = "cancel-" + str(uuid4())
    async with client(app, account) as sdk:
        request = (
            KnowledgeIngestRequest(request_id=uuid4(), content="A: allowance 500")
            if boundary == "extract"
            else await prepared(sdk)
        )
        entered = asyncio.Event()
        original = models.transport.handler

        async def handler(req):
            if json.loads(req.content)["model"] == boundary:
                entered.set()
                await asyncio.Event().wait()
            return original(req)

        models.transport.handler = handler
        call = sdk.knowledge.ingest if boundary == "extract" else sdk.knowledge.maintain
        task = asyncio.create_task(call(request))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        models.transport.handler = original
        ident = operation(account, request)
        result, evidence = await public_snapshot(app, sdk, ident, account)
        assert result.status == "error" and result.reason == "request_cancelled"
        assert all(
            w.status == "unresolved" and not w.fresh and not w.proof_id
            for w in result.works
        )
        public = [
            r["payload"]
            for r in evidence["attempts"]
            if r["payload"]["identity"]["operation"].endswith("-public")
        ]
        assert (
            len(
                [
                    r
                    for r in public
                    if r["phase"] == "finished"
                    and r["error"]["code"] == "request_cancelled"
                ]
            )
            == 1
        )
        # Missing provider usage remains missing, never a fabricated free call.
        for r in evidence["attempts"]:
            if r["payload"]["phase"] == "started":
                assert r["payload"]["usage"]["completeness"] == "missing"
        count = len(models.calls)
        repeat = (
            request
            if boundary == "extract"
            else request.model_copy(update={"request_id": uuid4()})
        )
        assert await call(repeat) == result
        assert await sdk.knowledge.get_maintenance_result(ident) == result
        assert len(models.calls) == count
        assert not app.state.knowledge_service._active_requests
        save("cancel-" + boundary, evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["updated", "unchanged"])
async def test_cancellation_preserves_committed_upstream_and_stops_rest(
    public_app, outcome
):
    app, models = public_app
    models.outcome = outcome
    account = "partial-" + str(uuid4())
    async with client(app, account) as sdk:
        request = await prepared(sdk)
        entered, seen = asyncio.Event(), 0
        original = models.transport.handler

        async def handler(req):
            nonlocal seen
            if json.loads(req.content)["model"] == "base":
                seen += 1
                if seen == 2:
                    entered.set()
                    await asyncio.Event().wait()
            return original(req)

        models.transport.handler = handler
        task = asyncio.create_task(sdk.knowledge.maintain(request))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ident = operation(account, request)
        result, evidence = await public_snapshot(app, sdk, ident, account)
        assert result.status == "error" and result.reason == "request_cancelled"
        committed = [w for w in result.works if w.status == outcome]
        assert len(committed) == 1 and committed[0].proof_id and committed[0].fresh
        assert all(w.status in (outcome, "unresolved") for w in result.works)
        assert any(w.reason == "request_cancelled" for w in result.works)
        count = len(models.calls)
        assert (
            await sdk.knowledge.maintain(
                request.model_copy(update={"request_id": uuid4()})
            )
            == result
        )
        assert len(models.calls) == count
        save("partial-" + outcome, evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_committed", [False, True])
async def test_cancel_after_commits_does_not_report_rollback(
    public_app, monkeypatch, completion_committed
):
    app, models = public_app
    account = "after-commit-" + str(uuid4())
    async with client(app, account) as sdk:
        request = await prepared(sdk)
        entered = asyncio.Event()
        service = app.state.knowledge_service
        finish = service._finish

        async def gate(*args, **kwargs):
            if completion_committed:
                await finish(*args, **kwargs)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(service, "_finish", gate)
        task = asyncio.create_task(sdk.knowledge.maintain(request))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result, evidence = await public_snapshot(
            app, sdk, operation(account, request), account
        )
        assert result.status == ("complete" if completion_committed else "error")
        assert (
            len([w for w in result.works if w.status == "updated" and w.proof_id]) == 3
        )
        assert all(n.fresh and n.version == 2 for n in result.nodes)
        count = len(models.calls)
        assert (
            await sdk.knowledge.maintain(
                request.model_copy(update={"request_id": uuid4()})
            )
            == result
        )
        assert len(models.calls) == count
        save("after-commit-" + str(completion_committed), evidence)


@pytest.mark.asyncio
async def test_cancel_duplicate_query_cannot_cancel_actual_owner(
    public_app, monkeypatch
):
    app, models = public_app
    account = "duplicate-" + str(uuid4())
    async with client(app, account) as sdk:
        request = await prepared(sdk)
        entered, release, querying = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = models.transport.handler

        async def handler(req):
            if json.loads(req.content)["model"] == "base":
                entered.set()
                await release.wait()
            return original(req)

        models.transport.handler = handler
        service = app.state.knowledge_service
        query = service.get_maintenance_result

        async def slow_query(*args, **kwargs):
            querying.set()
            await asyncio.Event().wait()

        owner = asyncio.create_task(sdk.knowledge.maintain(request))
        await asyncio.wait_for(entered.wait(), 5)
        monkeypatch.setattr(service, "get_maintenance_result", slow_query)
        duplicate = asyncio.create_task(
            sdk.knowledge.maintain(request.model_copy(update={"request_id": uuid4()}))
        )
        await asyncio.wait_for(querying.wait(), 5)
        duplicate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await duplicate
        monkeypatch.setattr(service, "get_maintenance_result", query)
        interim = await sdk.knowledge.get_maintenance_result(
            operation(account, request)
        )
        assert interim.status == "running"
        release.set()
        result = await asyncio.wait_for(owner, 5)
        assert result.status == "complete" and all(n.fresh for n in result.nodes)


@pytest.mark.asyncio
async def test_repeated_cancel_during_cleanup_is_drained(public_app, monkeypatch):
    app, models = public_app
    account = "repeat-cancel-" + str(uuid4())
    async with client(app, account) as sdk:
        request = await prepared(sdk)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(req):
            entered.set()
            await asyncio.Event().wait()

        models.transport.handler = handler
        service = app.state.knowledge_service
        stop = service.executor.stop_work

        async def slow_stop(*args, **kwargs):
            cleaning.set()
            await release.wait()
            return await stop(*args, **kwargs)

        monkeypatch.setattr(service.executor, "stop_work", slow_stop)
        task = asyncio.create_task(sdk.knowledge.maintain(request))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await sdk.knowledge.get_maintenance_result(operation(account, request))
        assert result.status == "error" and all(
            w.status == "unresolved" for w in result.works
        )


@pytest.mark.asyncio
async def test_service_close_cancels_then_drains_before_clients(public_app):
    app, models = public_app
    account = "close-" + str(uuid4())
    async with client(app, account) as sdk:
        request = await prepared(sdk)
        entered = asyncio.Event()

        async def handler(req):
            entered.set()
            await asyncio.Event().wait()

        models.transport.handler = handler
        task = asyncio.create_task(sdk.knowledge.maintain(request))
        await asyncio.wait_for(entered.wait(), 5)
        await app.state.knowledge_service.close()
        from contexthub_sdk import ServerError

        with pytest.raises(ServerError, match="knowledge_closing"):
            await task
        result = await sdk.knowledge.get_maintenance_result(operation(account, request))
        assert result.status == "error" and all(
            w.status == "unresolved" for w in result.works
        )
        assert app.state.knowledge_service.runner.base.client._client.is_closed
