from .helpers import extraction_response
"""Unified extraction items are classified before any long-term memory write."""

import json
import pytest
from contexthub.llm.retry_policy import ResponseSchemaError
from contexthub.memory.contracts import split_ref
from .helpers import session, add, row, item, input_of, direct_response


@pytest.mark.asyncio
async def test_update_and_forget_use_same_p2_interface_and_only_session_sources(env):
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 20, "vector_k": 20, "hybrid_limit": 20, "entity_k": 10}
    )
    c1 = await session(env, 1)
    health = await add(env, c1, "Health lactose intolerance")
    partner = await add(env, c1, "Partner James")
    update = "Lactose intolerance resolved; the user now has high blood pressure."
    forget = "Forget that the user's partner is James."
    env.responses.append(extraction_response(rules=[], contents=[item(update), item(forget)]))

    def judge(payload):
        data = input_of(payload)
        assert set(data) == {"incoming", "candidates"}
        assert data["incoming"]["created_in_session"] == 2
        assert data["incoming"]["type"] == "content"
        assert "message_refs" not in json.dumps(
            data
        ) and "RAW_CHAT_ONLY_MARKER" not in json.dumps(data)
        return (
            direct_response("update", "Health lactose intolerance")(payload)
            if data["incoming"]["text"] == update
            else direct_response("forget", "Partner James")(payload)
        )

    env.responses.extend([judge, judge, lambda payload: {"target_refs": [input_of(payload)["primary_ref"]]}])
    refs = await env.full_pipeline.ingest(
        env.trace,
        2,
        [
            {"role": "user", "content": update},
            {"role": "assistant", "content": "RAW_CHAT_ONLY_MARKER"},
            {"role": "user", "content": forget},
        ],
    )
    assert len(refs) == 1 and refs[0][0] == split_ref(health)[0]
    assert (await row(env, health))["status"] == "stale"
    assert (await row(env, partner))["status"] == "deleted"
    source = await env.store.sources(env.trace, *refs[0])
    assert source["session_seq"] == [2] and "message_refs" not in source
    saved = await env.store.get_session(env.trace, 2)
    assert saved["messages"][1]["content"] == "RAW_CHAT_ONLY_MARKER"
    assert "document: " + forget not in env.embedded
    async with env.store.pool.acquire() as conn:
        memories = await conn.fetch(
            "SELECT text FROM memories WHERE tenant_id=$1 AND namespace=$2",
            *env.trace.args[:2]
        )
    assert len(memories) == 3 and forget not in [r["text"] for r in memories]
    assert not env.responses


@pytest.mark.asyncio
async def test_unmatched_forget_is_held_in_session_log_without_fact_or_document_embedding(
    env,
):
    text = "Forget my partner information."
    env.responses.extend(
        [extraction_response(rules=[], contents=[item(text)]), {"action": "hold", "target_ref": None}]
    )
    assert (
        await env.full_pipeline.ingest(
            env.trace, 1, [{"role": "user", "content": text}]
        )
        == []
    )
    assert not await env.store.current_rows(env.trace)
    assert not any(s.startswith("document: ") for s in env.embedded)
    assert text in (await env.store.get_session(env.trace, 1))["extraction_output"]
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memory_steps WHERE tenant_id=$1 AND namespace=$2 AND stage='p2.held'",
                *env.trace.args[:2]
            )
            == 1
        )


@pytest.mark.asyncio
async def test_duplicate_merges_sessions_without_new_memory_or_document_embedding(env):
    c = await session(env, 1)
    original = await add(env, c, "Owner Wang")
    count = len([s for s in env.embedded if s.startswith("document: ")])
    env.responses.extend(
        [extraction_response(rules=[], contents=[item("Owner Wang")]), direct_response("duplicate", "Owner Wang")]
    )
    refs = await env.full_pipeline.ingest(
        env.trace, 2, [{"role": "user", "content": "Owner Wang"}]
    )
    assert refs == [split_ref(original)]
    assert (await env.store.sources(env.trace, *refs[0]))["session_seq"] == [1, 2]
    assert len(await env.store.versions(env.trace, refs[0][0])) == 1
    assert len([s for s in env.embedded if s.startswith("document: ")]) == count


@pytest.mark.asyncio
async def test_failed_document_embedding_does_not_stale_old_or_save_new_memory(env):
    c = await session(env, 1)
    old = await add(env, c, "Owner Wang")
    env.vectors["document: Owner Li"] = [0, 0, 0]
    env.responses.extend(
        [extraction_response(rules=[], contents=[item("Owner Li")]), direct_response("update", "Owner Wang")]
    )
    with pytest.raises((ResponseSchemaError, ValueError)):
        await env.full_pipeline.ingest(
            env.trace, 2, [{"role": "user", "content": "Owner Li"}]
        )
    assert (await row(env, old))["status"] == "fresh"
    assert len(await env.store.versions(env.trace, split_ref(old)[0])) == 1
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memories WHERE tenant_id=$1 AND namespace=$2",
                *env.trace.args[:2]
            )
            == 1
        )
