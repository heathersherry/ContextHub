from .helpers import extraction_response, forget_item
import asyncio
import json

import httpx
import pytest

from contexthub.memory.extractor import ExtractionFailed, parse_items
from contexthub.memory.jev_candidate_gate import RULE_HINT


def payload(text="Atlas owner is Li"):
    return json.dumps(extraction_response(rules=[], contents=[{"text": text, "topics": [], "entities": [], "temporal_scope": "current"}]))


@pytest.mark.parametrize(
    "value",
    [
        "",
        "[]",
        "{}",
        '{"items":[null]}',
        '{"items":{}}',
        '{"items":[{"text":"x","entities":[],"message_refs":[1]}]}',
        '{"items":[{"text":"x","entities":[],"type":"fact"}]}',
        '{"items":[],"instructions":[]}',
    ],
)
def test_obsolete_fields_and_unusable_outputs_rejected(value):
    with pytest.raises(ValueError):
        parse_items(value)


def test_prompt_and_conditional_hints():
    assert RULE_HINT.findall("If the owner changes, reports will switch recipient.")
    assert RULE_HINT.findall("if Atlas expands it would become larger")


@pytest.mark.asyncio
async def test_extraction_keeps_requests_without_saving_or_embedding_any_item(env):
    raw = extraction_response(rules=[], contents=[
            {"text": "Owner is Li", "topics": [], "entities": [], "temporal_scope": "current"},
        ], forget_requests=[forget_item("Forget nickname", order=2)])
    env.responses.append(raw)
    items = await env.pipeline.ingest(
        env.trace,
        1,
        [{"role": "user", "content": "Owner is Li. Forget my nickname."}],
        timestamp="raw time",
    )
    assert len(items) == 2 and not env.embedded
    saved = await env.store.get_session(env.trace, 1)
    assert saved["messages"][0]["content"] == "Owner is Li. Forget my nickname."
    assert [i["text"] for i in json.loads(saved["extraction_output"])["items"]] == [
        "Owner is Li",
        "Forget nickname",
    ]
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memories WHERE tenant_id=$1 AND namespace=$2",
                *env.trace.args[:2]
            )
            == 0
        )


@pytest.mark.asyncio
async def test_format_and_transport_retries_preserve_all_responses(env):
    env.responses.extend([503, "not json", "", payload()])
    await env.pipeline.ingest(
        env.trace,
        1,
        [{"role": "user", "content": "Atlas owner is Li"}],
        timeout_seconds=10,
    )
    async with env.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM execution_attempts WHERE tenant_id=$1 AND namespace=$2
            AND phase='finished' AND extraction_attempt IS NOT NULL ORDER BY extraction_attempt,attempt_no""",
            env.trace.tenant_id,
            env.trace.namespace,
        )
    assert len(rows) == 4
    assert [r["extraction_attempt"] for r in rows] == [1, 1, 2, 3]
    assert [r["attempt_no"] for r in rows] == [1, 2, 1, 1]
    assert [r["retry_reason"] for r in rows] == [
        "initial",
        "transport_error",
        "parse_error",
        "parse_error",
    ]
    assert [r["parse_result"] for r in rows] == [None, "unusable", "unusable", "usable"]
    assert [r["raw_output"] for r in rows][:3] == [None, "not json", ""]
    assert rows[0]["payload"]["usage"]["input_tokens"] is None
    assert all(r["payload"]["cost"]["amount"] is None for r in rows)
    assert sum(r["payload"]["usage"]["input_tokens"] or 0 for r in rows) == 60


@pytest.mark.asyncio
async def test_four_unusable_responses_stop_trajectory_but_not_other_episode(env):
    env.responses.extend(["bad1", "bad2", "bad3", "bad4"])
    with pytest.raises(ExtractionFailed):
        await env.pipeline.ingest(
            env.trace, 1, [{"role": "user", "content": "Hello"}], timeout_seconds=10
        )
    assert (await env.store.get_session(env.trace, 1))["extraction_output"] == "bad4"
    with pytest.raises(ValueError, match="previous session"):
        await env.pipeline.ingest(env.trace, 2, [], timeout_seconds=10)
    assert len(env.prompts) == 4 and env.embedded == []
    other = env.trace.model_copy(update={"namespace": env.trace.namespace + "-other"})
    env.responses.append('{"rules":[],"contents":[],"forget_requests":[]}')
    assert await env.pipeline.ingest(other, 1, [], timeout_seconds=10) == []
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM execution_attempts WHERE tenant_id=$1 AND namespace=$2 AND phase='finished'",
                env.trace.tenant_id,
                env.trace.namespace,
            )
            == 4
        )


@pytest.mark.asyncio
async def test_empty_batch_no_retry_and_only_current_session_in_prompt(env):
    env.responses.extend([payload("OLD SECRET"), '{"rules":[],"contents":[],"forget_requests":[]}'])
    await env.pipeline.ingest(
        env.trace, 1, [{"role": "user", "content": "OLD SECRET"}], timeout_seconds=10
    )
    assert (
        await env.pipeline.ingest(
            env.trace, 2, [{"role": "user", "content": "Hello"}], timeout_seconds=10
        )
        == []
    )
    assert len(env.prompts) == 2
    assert "OLD SECRET" not in env.prompts[1]["messages"][0]["content"]
    assert len(env.embedded) == 0


@pytest.mark.asyncio
async def test_transport_exhaustion_not_format_retry(env):
    env.responses.extend([httpx.ReadTimeout("fixture"), httpx.ReadTimeout("fixture")])
    with pytest.raises(httpx.ReadTimeout):
        await env.pipeline.ingest(env.trace, 1, [], timeout_seconds=10)
    assert len(env.prompts) == 2
    async with env.store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT extraction_attempt,attempt_no FROM execution_attempts WHERE tenant_id=$1 AND namespace=$2 AND phase='finished' ORDER BY attempt_no",
            env.trace.tenant_id,
            env.trace.namespace,
        )
    assert [(r[0], r[1]) for r in rows] == [(1, 1), (1, 2)]


@pytest.mark.asyncio
async def test_session_deadline_stops_without_extra_requests(env):
    async def slow(request):
        env.prompts.append("slow request")
        await asyncio.sleep(1)

    env.pipeline.extractor.client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow), base_url="https://fixture.invalid/v1"
    )
    with pytest.raises(TimeoutError):
        await env.pipeline.ingest(env.trace, 1, [], timeout_seconds=0.15)
    assert len(env.prompts) == 1
    async with env.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM execution_attempts WHERE tenant_id=$1 AND namespace=$2 AND phase='finished'",
            env.trace.tenant_id,
            env.trace.namespace,
        )
    assert row["payload"]["stop_reason"] == "cancelled"


@pytest.mark.parametrize('scope', [None, 'past', '', 1, []])
def test_content_requires_explicit_valid_temporal_scope(scope):
    with pytest.raises(ValueError):
        parse_items(json.dumps({'items': [{'text': 'A past fact', 'topics': [], 'entities': [], 'temporal_scope': scope}]}))


def test_content_cannot_omit_temporal_scope():
    with pytest.raises(ValueError):
        parse_items('{"items":[{"text":"A fact","entities":[]}]}')


def test_historical_scope_survives_parse():
    items, _ = parse_items('{"items":[{"text":"Lived in Oxford until 2020","topics":[],"entities":["Oxford"],"temporal_scope":"historical"}]}')
    assert items[0].temporal_scope == 'historical'
