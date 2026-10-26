from .helpers import extraction_response
"""Real SDK -> HTTP -> shared engine -> PostgreSQL, with deterministic models."""

import json
from dataclasses import replace

import httpx
import pytest
from contexthub.config import Settings
from contexthub.main import create_app
from contexthub.memory.engine import MemoryEngine
from contexthub.memory.factory import assemble_memory_engine
from contexthub.memory.contracts import split_ref
from contexthub.errors import ConflictError
from contexthub_sdk import ContextHubClient, MemoryIngestRequest, MemoryContextRequest
from contexthub_sdk.exceptions import ConflictError as ClientConflict
from integrations.memebench.memory_research.runner import MemoryAnswerer
from .helpers import (
    item,
    direct_response,
    add,
    session,
    bindings_response,
    rule_item,
    trigger_response,
    repair_response,
)


def engine_for(env):
    return assemble_memory_engine(
        env.store,
        env.executor.client,
        env.indexer.client,
        retrieval=env.retriever.config.model_copy(
            update={"hybrid_limit": 20, "text_k": 20, "vector_k": 20}
        ),
        retry=env.executor.retry,
        code_version="fixture",
    )


def client_for(env, engine, **scope):
    app = create_app()
    app.state.settings = Settings(_env_file=None, api_key="fixture")
    app.state.memory_engine = engine
    return ContextHubClient(
        "http://local",
        "fixture",
        scope.get("tenant_id", env.trace.tenant_id),
        scope.get("owner_id", env.trace.owner_id),
        transport=httpx.ASGITransport(app=app),
    )


@pytest.mark.asyncio
async def test_sdk_ingest_update_context_and_meme_share_selection(env):
    engine = engine_for(env)
    env.responses.extend(
        [
            extraction_response(rules=[], contents=[item("Owner Wang")]),
            {"action": "new", "target_ref": None},
            extraction_response(rules=[], contents=[item("Owner Li")]),
            direct_response("update", "Owner Wang"),
        ]
    )
    async with client_for(env, engine) as client:
        for seq, text in enumerate(["Owner Wang", "Owner Li"], 1):
            receipt = await client.memory.ingest(
                MemoryIngestRequest(
                    namespace=env.trace.namespace,
                    sequence=seq,
                    messages=[{"role": "user", "content": text}],
                )
            )
            assert (
                receipt.status == "complete"
                and receipt.sequence == seq
                and receipt.memory_refs
            )
        result = await client.memory.context(
            MemoryContextRequest(
                namespace=env.trace.namespace, query="Who is the owner?"
            )
        )
        assert [i.text for i in result.items] == ["Owner Li"]
        assert result.items[0].source_sessions == [2]
        assert json.loads(result.context)["memories"] == [
            {"text": "Owner Li", "type": "fact"}
        ]
        history = await client.memory.context(
            MemoryContextRequest(
                namespace=env.trace.namespace, query="owner", mode="history"
            )
        )
        assert {i.text for i in history.items} == {"Owner Wang", "Owner Li"}
        assert {tuple(i.source_sessions) for i in history.items} == {(1,), (2,)}
        limited = await client.memory.context(
            MemoryContextRequest(
                namespace=env.trace.namespace, query="owner", max_chars=40
            )
        )
        assert (
            len(limited.context) <= 40
            and limited.items == []
            and limited.budget_exhausted
        )
        with pytest.raises(ClientConflict):
            await client.memory.ingest(
                MemoryIngestRequest(
                    namespace=env.trace.namespace,
                    sequence=2,
                    messages=[{"role": "user", "content": "duplicate request"}],
                )
            )
        # Adapter adds only the official answer prompt. Public selection is identical.
        env.responses.append("Li")
        answerer = MemoryAnswerer(
            env.store,
            engine.pipeline.retriever,
            env.executor.client,
            env.executor.retry,
        )
        await answerer.answer(
            "Who is the owner?", replace(env.context, session_seq=2), timings={}
        )
        assert result.context in env.prompts[-1]["messages"][0]["content"]
    assert not env.responses


@pytest.mark.asyncio
async def test_http_rules_update_invalidate_and_repair(env):
    engine = engine_for(env)
    rule = "If owner changes, then recipient follows owner"
    env.responses.extend(
        [
            extraction_response(rules=[dict(rule_item(rule), candidate_ref="R1")], contents=[item("Owner Wang"), item("Recipient Wang")]),
            {"action": "new", "target_ref": None},
            {"action": "new", "target_ref": None},
            {"action": "new", "target_ref": None},
            bindings_response(["Owner Wang"], ["Recipient Wang"]),
            trigger_response([], []),
            extraction_response(rules=[], contents=[item("Owner Li")]),
            direct_response("update", "Owner Wang"),
            trigger_response(["Recipient Wang"], ["C1"]),
            repair_response("Recipient Li", [rule, "Owner Li"]),
        ]
    )
    async with client_for(env, engine) as client:
        for seq, text in enumerate(
            [rule + ". Owner Wang. Recipient Wang.", "Owner Li"], 1
        ):
            await client.memory.ingest(
                MemoryIngestRequest(
                    namespace=env.trace.namespace,
                    sequence=seq,
                    messages=[{"role": "user", "content": text}],
                )
            )
        response = await client.memory.context(
            MemoryContextRequest(namespace=env.trace.namespace, query="recipient")
        )
        assert "Recipient Wang" not in response.context
        derived = next(i for i in response.items if i.text == "Recipient Li")
        assert derived.origin == "derived" and derived.source_sessions == [1, 2]
        assert all(i.type == "fact" for i in response.items)
        policy = await client.memory.context(
            MemoryContextRequest(
                namespace=env.trace.namespace, query="owner", mode="policy"
            )
        )
        assert any(i.type == "rule" for i in policy.items)
    assert not env.responses


@pytest.mark.asyncio
async def test_scope_isolation_and_concurrent_namespace_locks(env):
    engine = engine_for(env)
    env.responses.extend(
        [extraction_response(rules=[], contents=[item("Private fact")]), {"action": "new", "target_ref": None}]
    )
    await engine.ingest(env.trace, 1, [{"role": "user", "content": "Private fact"}])
    for field in ("tenant_id", "owner_id", "namespace"):
        scope = env.trace.model_copy(update={field: "other"})
        assert (await engine.context(scope, "Private fact")).items == []
    async with env.store.lease(env.trace):
        with pytest.raises(ConflictError, match="memory_namespace_busy"):
            await engine.context(env.trace, "fact")
        other = env.trace.model_copy(update={"namespace": "other"})
        assert (await engine.context(other, "fact")).sequence == 0
    assert (await engine.context(env.trace, "fact")).items


@pytest.mark.asyncio
async def test_incomplete_ingestion_not_readable_or_silently_skipped(env):
    engine = engine_for(env)
    env.responses.append(RuntimeError("synthetic failure"))
    with pytest.raises(RuntimeError, match="synthetic"):
        await engine.ingest(env.trace, 1, [{"role": "user", "content": "fact"}])
    for action in (
        engine.context(env.trace, "fact"),
        engine.ingest(env.trace, 2, [{"role": "user", "content": "next"}]),
    ):
        with pytest.raises(ConflictError, match="memory_ingestion_incomplete"):
            await action
    # Process termination before session.failed is recorded is rejected too.
    other = env.trace.model_copy(update={"namespace": "crashed"})
    await env.store.save_session(other, 1, None, [])
    await env.store.save_output(other, 1, '{"items": []}')
    with pytest.raises(ConflictError, match="memory_ingestion_incomplete"):
        await engine.context(other, "fact")


@pytest.mark.asyncio
async def test_sources_are_bounded_and_deleted_evidence_not_exposed(env):
    c = await session(env, 1)
    ref = await add(env, c, "Original")
    builder = MemoryEngine(env.full_pipeline).contexts
    await session(env, 2)
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE memory_sources SET session_seq=ARRAY[1,2] WHERE tenant_id=$1 AND namespace=$2 AND owner_id=$3",
            *env.trace.args,
        )
    assert await builder.source_sessions(env.trace, ref, 1) == [1]
    await env.store.forget(env.trace, split_ref(ref)[0], "fixture deletion")
    assert await builder.source_sessions(env.trace, ref, 2) == []
