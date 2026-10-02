import asyncio
import json

import httpx
import pytest

from contexthub.memory.extractor import ExtractionFailed, PROMPT, RULE_HINT, parse_items


def payload(text="Atlas owner is Li", type="fact", entities=None, refs=None):
    return json.dumps(
        {
            "items": [
                {
                    "text": text,
                    "type": type,
                    "entities": entities or [],
                    "message_refs": [1] if refs is None else refs,
                }
            ]
        }
    )


def test_usable_partial_and_anomalies():
    data = json.loads(payload(type="unexpected", refs=[999, -4]))
    data["items"].append({"text": "cannot map"})
    items, warnings = parse_items(json.dumps(data))
    assert len(items) == 1 and items[0].type == "unexpected"
    assert items[0].message_refs == [999, -4] and len(warnings) == 2
    assert parse_items('{"items":[]}') == ([], [])
    for value in ("", "[]", "{}", '{"items":[null]}', '{"items":{}}'):
        with pytest.raises(ValueError):
            parse_items(value)


def test_prompt_and_conditional_hints():
    assert RULE_HINT.findall("If the owner changes, reports will switch recipient.")
    assert RULE_HINT.findall("if Atlas expands it would become larger")
    assert "ONLY the complete current session" in PROMPT
    assert "unresolved pronouns in text but NOT in entities" in PROMPT


@pytest.mark.asyncio
async def test_pipeline_persists_numbered_input_partial_output_and_initial_states(env):
    raw = json.loads(payload(type="odd-label", refs=[700, -1]))
    raw["items"] += json.loads(payload("Please forget my nickname", type="change"))[
        "items"
    ]
    raw["items"].append({"broken": True})
    env.responses.append(json.dumps(raw))
    refs = await env.pipeline.ingest(
        env.trace,
        1,
        [
            {"role": "assistant", "content": "Atlas owner is Li, okay?"},
            {
                "role": "user",
                "content": "Yes. If the owner changes reports will switch.",
            },
        ],
        timestamp="raw time",
        timeout_seconds=10,
    )
    assert len(refs) == 2 and len(env.prompts) == 1
    session = await env.store.get_session(env.trace, 1)
    assert session["messages"][1]["message_no"] == 2
    assert session["messages"][0]["role"] == "assistant"
    assert session["timestamp"] == "raw time" and session[
        "extraction_output"
    ] == json.dumps(raw)
    for id, version in refs:
        (row,) = await env.store.versions(env.trace, id)
        assert (row["version"], row["status"], row["origin"]) == (
            1,
            "unresolved",
            "extracted",
        )
        assert row["search_vector"] is not None and row["memory_embedding"] is not None
        assert row["timestamp"] == "raw time"
    source = await env.store.sources(env.trace, *refs[0])
    assert source["session_seq"] == [1] and source["message_refs"] == {"1": [700, -1]}
    change = await env.store.get_change(env.trace, refs[1][0])
    assert (
        change["operation"] is None
        and change["target_refs"] == []
        and change["applied_session_seq"] is None
    )
    assert await env.retriever.current(env.trace, 1, purpose="answer") == []
    prompt = env.prompts[0]["messages"][0]["content"]
    assert "possible_rules" in prompt and "reports will switch" in prompt
    assert len(env.embedded) == 2


@pytest.mark.asyncio
async def test_format_and_transport_retries_preserve_all_responses(env):
    env.responses.extend([503, "not json", "", payload(refs=[99])])
    await env.pipeline.ingest(
        env.trace,
        1,
        [{"role": "user", "content": "Atlas owner is Li"}],
        timeout_seconds=10,
    )
    async with env.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM execution_attempts WHERE run_id=$1 AND episode_id=$2
            AND phase='finished' AND extraction_attempt IS NOT NULL ORDER BY extraction_attempt,attempt_no""",
            env.trace.run_id,
            env.trace.episode_id,
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
    other = env.trace.model_copy(update={"episode_id": env.trace.episode_id + "-other"})
    env.responses.append('{"items":[]}')
    assert await env.pipeline.ingest(other, 1, [], timeout_seconds=10) == []
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM execution_attempts WHERE run_id=$1 AND episode_id=$2 AND phase='finished'",
                env.trace.run_id,
                env.trace.episode_id,
            )
            == 4
        )


@pytest.mark.asyncio
async def test_empty_batch_no_retry_and_only_current_session_in_prompt(env):
    env.responses.extend([payload("OLD SECRET"), '{"items":[]}'])
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
    assert len(env.embedded) == 1


@pytest.mark.asyncio
async def test_transport_exhaustion_not_format_retry(env):
    env.responses.extend([httpx.ReadTimeout("fixture"), httpx.ReadTimeout("fixture")])
    with pytest.raises(httpx.ReadTimeout):
        await env.pipeline.ingest(env.trace, 1, [], timeout_seconds=10)
    assert len(env.prompts) == 2
    async with env.store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT extraction_attempt,attempt_no FROM execution_attempts WHERE run_id=$1 AND episode_id=$2 AND phase='finished' ORDER BY attempt_no",
            env.trace.run_id,
            env.trace.episode_id,
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
            "SELECT payload FROM execution_attempts WHERE run_id=$1 AND episode_id=$2 AND phase='finished'",
            env.trace.run_id,
            env.trace.episode_id,
        )
    assert row["payload"]["stop_reason"] == "cancelled"
