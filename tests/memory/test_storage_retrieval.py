from dataclasses import replace
from uuid import UUID

import pytest

from contexthub.memory.retriever import fuse
from contexthub.memory.schemas import (
    RetrievalConfig,
    normalize_entities,
    vector_literal,
)
from contexthub.memory.storage import MemoryStore


async def session(env, seq, timestamp=None):
    await env.store.save_session(env.trace, seq, timestamp, [])
    await env.store.save_output(env.trace, seq, '{"items":[]}')


async def memory(
    env,
    text,
    *,
    seq=1,
    type="fact",
    status="fresh",
    entities=(),
    vector=(1.0, 0.0, 0.0),
    origin="extracted",
):
    ref = await env.store.add_memory(
        env.trace,
        seq,
        text=text,
        type=type,
        entities=entities,
        origin=origin,
        sources=[seq] if origin == "extracted" else None,
        topics=[],
    )
    env.vectors["document: " + text] = list(vector)
    await env.indexer.index([ref], replace(env.context, session_seq=seq))
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET temporal_scope='current',accepted=$6 WHERE tenant_id=$1 AND namespace=$2 AND owner_id=$3 AND memory_id=$4 AND version=$5",
            *env.trace.args,
            *ref,
            status == "fresh",
        )
    if status != "unresolved":
        await env.store.set_status(env.trace, *ref, status)
    return ref


@pytest.mark.asyncio
async def test_latest_before_status_no_old_fresh_fallback_and_state_only_indexing(env):
    await session(env, 1, "T1")
    old = await memory(env, "Atlas owner is Wang", entities=[" ATLAS ", "Wang"])
    await session(env, 2, "T2")
    new = await env.store.add_version(
        env.trace,
        old[0],
        2,
        text="Atlas owner is Li",
        type="fact",
        entities=["Atlas", "Li"],
        origin="extracted",
        sources=[2],
        topics=[],
    )
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET temporal_scope='current',accepted=true WHERE tenant_id=$1 AND namespace=$2 AND owner_id=$3 AND memory_id=$4",
            *env.trace.args,
            new[0],
        )
    await env.indexer.index([new], replace(env.context, session_seq=2))
    assert await env.retriever.current(env.trace, 2, purpose="answer") == []
    assert len(await env.retriever.current(env.trace, 1, purpose="answer")) == 1
    assert (await env.retriever.current(env.trace, 2, purpose="p1"))[0]["version"] == 2
    assert (
        await env.retriever.search(
            "owner",
            replace(env.context, session_seq=2),
            through_session=2,
            purpose="answer",
        )
        == []
    )
    before = (await env.store.versions(env.trace, old[0]))[-1]
    count = len(env.embedded)
    await env.store.set_status(env.trace, *new, "fresh")
    await env.indexer.index([new], replace(env.context, session_seq=2))
    after = (await env.store.versions(env.trace, old[0]))[-1]
    assert len(env.embedded) == count
    assert (
        after["memory_embedding"] == before["memory_embedding"]
        and after["search_vector"] == before["search_vector"]
    )
    assert after["created_in_session"] == 2 and after["timestamp"] == "T2"
    hits = await env.retriever.search(
        "owner",
        replace(env.context, session_seq=2),
        through_session=2,
        purpose="answer",
    )
    assert len(hits) == 1 and hits[0].memory["text"] == "Atlas owner is Li"
    await env.store.set_status(env.trace, *new, "stale", "needs review")
    uncertain = await env.retriever.current(env.trace, 2, purpose="answer")
    assert uncertain == []  # Never fall back to an older fresh version.
    assert await env.retriever.current(env.trace, 2, purpose="p2") == []
    await env.store.forget(env.trace, old[0], "user requested forgetting")
    assert len(await env.store.versions(env.trace, old[0])) == 2
    assert all(
        r["status"] == "deleted" for r in await env.store.versions(env.trace, old[0])
    )
    for purpose in ("answer", "p1", "p2"):
        assert await env.retriever.current(env.trace, 1, purpose=purpose) == []
        assert (
            await env.retriever.search(
                "Atlas",
                replace(env.context, session_seq=2),
                through_session=2,
                purpose=purpose,
                entities=["Atlas"] if purpose == "p1" else [],
            )
            == []
        )
    with pytest.raises(ValueError, match="forgotten"):
        await env.store.set_status(env.trace, *old, "fresh")
    with pytest.raises(ValueError):
        await env.store.add_version(
            env.trace,
            old[0],
            2,
            text="resurrected",
            type="fact",
            entities=[],
            origin="extracted",
            topics=[],
        )


@pytest.mark.asyncio
async def test_filter_before_top_k_for_all_channels_and_trace_isolation(env):
    await session(env, 1)
    target = await memory(
        env, "owner owner owner permitted", entities=["Atlas"], vector=(0.5, 0.5, 0)
    )
    for status in ("unresolved", "stale"):
        await memory(
            env,
            "owner owner owner owner distractor " + status,
            status=status,
            entities=["Atlas"],
        )
    forgotten = await memory(env, "owner owner deleted", entities=["Atlas"])
    await env.store.forget(env.trace, forgotten[0], "forget")
    await memory(env, "owner rule", type="rule", vector=(-1, 0, 0))
    for field, value in [
        ("tenant_id", "other-" + env.trace.tenant_id),
        ("namespace", "other-" + env.trace.namespace),
        ("owner_id", "adaptive"),
    ]:
        other = env.trace.model_copy(update={field: value})
        await env.store.save_session(other, 1, None, [])
        ref = await env.store.add_memory(
            other,
            1,
            text="owner owner owner outsider",
            type="fact",
            entities=["Atlas"],
            origin="extracted",
            topics=[],
        )
        await env.store.set_status(other, *ref, "fresh")
        await env.indexer.index([ref], replace(env.context, trace=other))
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 1, "vector_k": 1, "hybrid_limit": 1, "entity_k": 1}
    )
    hits = await env.retriever.search(
        "owner", env.context, through_session=1, purpose="answer"
    )
    assert len(hits) == 1 and hits[0].memory["memory_id"] == target[0]
    assert set(hits[0].channels) == {"text", "vector"}
    assert hits[0].score == pytest.approx(2 / 61)
    candidates = await env.retriever.current(env.trace, 1, purpose="p2_match")
    assert {r["status"] for r in candidates} == {"fresh", "stale", "unresolved"}
    assert {r["type"] for r in candidates} == {"fact", "rule"}
    assert all(r["tenant_id"] == env.trace.tenant_id for r in candidates)


@pytest.mark.asyncio
async def test_exact_vector_text_and_entity_union_are_distinct(env):
    await session(env, 1)
    text_ref = await memory(env, "reports recipient", vector=(0, 1, 0))
    vector_ref = await memory(env, "semantic neighbor", vector=(1, 0, 0))
    entity_ref = await memory(
        env,
        "unrelated old weekly meeting",
        entities=[" ＡＴＬＡＳ "],
        vector=(-1, 0, 0),
    )
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 1, "vector_k": 1, "hybrid_limit": 2, "entity_k": 1}
    )
    hits = await env.retriever.search(
        "reports", env.context, through_session=1, purpose="p1", entities=["Atlas"]
    )
    assert {h.memory["memory_id"] for h in hits} == {
        text_ref[0],
        vector_ref[0],
        entity_ref[0],
    }
    by_id = {h.memory["memory_id"]: h for h in hits}
    assert by_id[text_ref[0]].channels == ("text",)
    assert by_id[vector_ref[0]].channels == ("vector",)
    assert by_id[entity_ref[0]].channels == ("entity",)
    assert env.embedded[-1] == "query: reports"
    async with env.store.pool.acquire() as conn:
        indexes = await conn.fetch(
            "SELECT indexdef FROM pg_indexes WHERE tablename='memories'"
        )
    assert not any("hnsw" in row[0] or "ivfflat" in row[0] for row in indexes)
    assert sum("USING gin" in row[0] for row in indexes) == 4
    answer_hits = await env.retriever.search(
        "reports", env.context, through_session=1, purpose="answer", entities=["Atlas"]
    )
    assert entity_ref[0] not in {h.memory["memory_id"] for h in answer_hits}


@pytest.mark.asyncio
async def test_generic_user_is_not_a_full_text_keyword(env):
    await session(env, 1)
    relevant = await memory(env, "The user swims twice weekly")
    await memory(env, "The user bought a photo album", vector=(-1, 0, 0))
    await memory(env, "The user plans company events", vector=(0, 1, 0))
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 20, "vector_k": 1, "hybrid_limit": 20, "entity_k": 0}
    )
    for query in ("The user swims", "USER swimming", "the user's swimming"):
        hits = await env.retriever.search(
            query, env.context, through_session=1, purpose="p2"
        )
        assert [h.memory["memory_id"] for h in hits] == [relevant[0]]
        assert "text" in hits[0].channels
    # An empty keyword query is valid; vector retrieval still works independently.
    hits = await env.retriever.search(
        "the user", env.context, through_session=1, purpose="p2"
    )
    assert hits and all(h.channels == ("vector",) for h in hits)


@pytest.mark.asyncio
async def test_reopen_database_persists_and_checks_configuration(env):
    import os

    await session(env, 1)
    ref = await memory(env, "saved independently")
    reopened = await MemoryStore.open(
        os.environ["MEMORY_DATABASE_URL"], env.store.config
    )
    try:
        assert (await reopened.versions(env.trace, ref[0]))[0][
            "text"
        ] == "saved independently"
    finally:
        await reopened.close()
    with pytest.raises(ValueError, match="configuration mismatch"):
        await MemoryStore.open(
            os.environ["MEMORY_DATABASE_URL"],
            env.store.config.model_copy(update={"embedding_model": "different"}),
        )
    reopened = await MemoryStore.open(
        os.environ["MEMORY_DATABASE_URL"], env.store.config, initialize=True
    )
    assert (await reopened.versions(env.trace, ref[0]))[0]["text"] == "saved independently"
    await reopened.close()


def test_time_ties_only_for_answers_and_normalization():
    config = RetrievalConfig(text_k=2, vector_k=2, rrf_c=60, hybrid_limit=2, entity_k=1)
    old = dict(
        memory_id=UUID(int=1), version=1, timestamp="2026-01-01", created_in_session=2
    )
    new = dict(
        memory_id=UUID(int=2), version=1, timestamp="2026-02-01", created_in_session=1
    )
    for purpose, first in [("answer", new), ("p1", old), ("p2", old)]:
        assert fuse([old], [new], [], purpose, config)[0].memory == first
    new["timestamp"] = "not a date"
    assert fuse([old], [new], [], "answer", config)[0].memory == old
    new["timestamp"] = "2026-02-01T00:00:00+01:00"
    assert (
        fuse([old], [new], [], "answer", config)[0].memory == old
    )  # naive + aware: use sequence
    assert normalize_entities([" ＡＴＬＡＳ ", "Atlas", "Mr   Jordan"]) == [
        "atlas",
        "mr jordan",
    ]
    assert normalize_entities(["Mr Jordan"]) != normalize_entities(["Michael Jordan"])
    for v in ([0, 0, 0], [float("nan"), 1, 0], [1]):
        with pytest.raises(ValueError):
            vector_literal(v, 3)


@pytest.mark.asyncio
async def test_failed_session_requires_later_completion_in_same_session(env):
    await session(env, 1)
    await env.store.record(env.context, "session.complete", {})
    await env.store.record(env.context, "session.failed", {"error": "HTTPStatusError"})
    with pytest.raises(ValueError, match="previous maintenance failed"):
        await env.store.save_session(env.trace, 2, None, [])
    # A completion from another session cannot clear this failure.
    await env.store.record(replace(env.context, session_seq=2), "session.complete", {})
    with pytest.raises(ValueError, match="previous maintenance failed"):
        await env.store.save_session(env.trace, 2, None, [])
    await env.store.record(env.context, "session.complete", {"resumed": True})
    await session(env, 2)
    await env.store.record(env.context, "session.failed", {"error": "later failure"})
    with pytest.raises(ValueError, match="previous maintenance failed"):
        await env.store.save_session(env.trace, 3, None, [])
