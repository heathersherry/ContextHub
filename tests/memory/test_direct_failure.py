from .helpers import rule_item
from .helpers import link
"""A rejected direct item does not stop other items or later pipeline stages."""

import httpx
import pytest

from contexthub.memory.storage import SCOPE
from .helpers import add, direct, direct_response, input_of, item, pending, row, session


@pytest.mark.asyncio
async def test_cross_type_failure_continues_binding_conditions_and_later_session(env):
    c = await session(env, 1)
    rule_text = "If health changes, exercise changes."
    rule = await add(env, c, rule_text, type="rule")
    old = await add(env, c, "The user swims twice weekly.")
    await link(env, rule, targets=[old])
    original_rule = await row(env, rule)
    failed_text = "If the user's health changes, their exercise changes."
    seen = []
    direct_attempts = 0

    def response(payload):
        nonlocal direct_attempts
        data = input_of(payload)
        seen.append(data)
        if "messages" in data:
            if "rule_output" not in data:
                return {"items": [rule_item(rule_text)]}
            texts = (
                [failed_text, "The user has recovered."]
                if data["messages"][0]["content"].startswith("If")
                else ["The user enjoys painting."]
            )
            return {"items": [item(t) for t in texts]}
        if "incoming" in data:
            text = data["incoming"]["text"]
            if text == rule_text:
                return direct_response("duplicate", rule_text)(payload)
            if text == failed_text:
                direct_attempts += 1
                action = "duplicate" if direct_attempts == 1 else "update"
                return direct_response(action, rule_text)(payload)
            assert all(r["text"] != failed_text for r in data["candidates"])
            return dict(action="new", target_ref=None)
        if "rule" in data:
            assert all(r["text"] != failed_text for r in data["candidates"])
            return {
                "condition_refs": [r["ref"] for r in data["candidates"] if r["text"] == "The user has recovered."],
                "affected_refs": [
                    r["ref"]
                    for r in data["candidates"]
                    if r["text"] == "The user swims twice weekly."
                ]
            }
        assert "checks" in data
        assert all(
            change.get("after", {}).get("text") != failed_text
            for change in data["changes"]
        )
        return {"triggers": []}

    env.responses.extend([response] * 20)
    await env.full_pipeline.ingest(
        env.trace,
        2,
        [
            {
                "role": "user",
                "content": "If health changes, exercise changes. I have recovered.",
            }
        ],
    )
    await env.full_pipeline.ingest(
        env.trace, 3, [{"role": "user", "content": "I enjoy painting."}]
    )
    assert direct_attempts == 2
    assert any("rule" in d for d in seen)
    assert any("checks" in d for d in seen)
    current = await env.store.current_rows(env.trace)
    assert {r["text"] for r in current} == {
        rule_text,
        "The user swims twice weekly.",
        "The user has recovered.",
        "The user enjoys painting.",
    }
    saved_rule = await row(env, rule)
    assert saved_rule["text"] == original_rule["text"]
    assert saved_rule["affected_matter_ids"] == original_rule["affected_matter_ids"]
    assert len(saved_rule["condition_matter_ids"]) == 1
    assert (await row(env, old))["status"] == "fresh"
    async with env.store.pool.acquire() as conn:
        logs = await conn.fetch(
            f"SELECT stage, session_seq, payload FROM memory_steps WHERE {SCOPE} ORDER BY step_id",
            *env.trace.args,
        )
    skips = [r for r in logs if r["stage"] == "p2.direct_skipped"]
    assert len(skips) == 1
    payload = skips[0]["payload"]
    assert payload["incoming"]["text"] == failed_text
    assert payload["attempts"] == 2 and payload["action"] == "skip_item"
    assert [r["session_seq"] for r in logs if r["stage"] == "session.complete"] == [
        2,
        3,
    ]
    assert not any(r["stage"] == "session.failed" for r in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rule", "content"])
@pytest.mark.parametrize(
    "output", ["{bad", "", '{"action":"update","target_ref":"M999"}']
)
async def test_invalid_direct_outputs_skip_without_writes(env, kind, output):
    c = await session(env, 1)
    env.responses.extend([output, output])
    assert await direct(env, pending(c, "Unaccepted item", type=kind), c) == (None, [])
    assert len(env.prompts) == 2
    assert await env.store.current_rows(env.trace) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["configuration", "transport", "logging", "apply"])
async def test_direct_does_not_swallow_execution_errors(env, monkeypatch, fault):
    c = await session(env, 1)
    expected = ValueError
    if fault == "configuration":
        env.responses.append(ValueError("configuration failure"))
    elif fault == "transport":
        env.responses.append(401)
        expected = httpx.HTTPStatusError
    elif fault == "logging":
        env.responses.extend(["{bad", "{bad"])
        record = env.store.record

        async def failing_record(context, stage, payload, **kwargs):
            if stage == "p2.direct_skipped":
                raise ValueError("database log write failure")
            return await record(context, stage, payload, **kwargs)

        monkeypatch.setattr(env.store, "record", failing_record)
    else:
        env.responses.append(dict(action="new", target_ref=None))

        async def failing_apply(*args, **kwargs):
            raise ValueError("database write failure")

        monkeypatch.setattr(env.executor, "apply_direct", failing_apply)
    with pytest.raises(expected):
        await direct(env, pending(c, "A new fact"), c)
    async with env.store.pool.acquire() as conn:
        skipped = await conn.fetchval(
            f"SELECT count(*) FROM memory_steps WHERE {SCOPE} AND stage='p2.direct_skipped'",
            *env.trace.args,
        )
    assert skipped == 0
