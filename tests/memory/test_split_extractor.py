from .helpers import rule_item
"""The two model calls have independent duties and one downstream contract."""

import json

import pytest

from contexthub.memory.extractor import ExtractionFailed
from .helpers import item
from .helpers import input_of


@pytest.mark.asyncio
@pytest.mark.parametrize("has_rule", [False, True])
async def test_fact_phase_reads_complete_chat_and_merge_preserves_rules(env, has_rule):
    rules = {
        "items": ([rule_item("If employer changes, then insurance changes")] if has_rule else []),
    }
    facts = {
        "items": [item("Employer changed to Acme")],
    }

    def second(payload):
        data = input_of(payload)
        assert data["rule_output"] == rules
        assert len(data["messages"]) == 2
        assert data["messages"][1]["content"] == "My employer changed to Acme."
        return facts

    env.responses.extend([rules, second])
    await env.pipeline.ingest(
        env.trace,
        1,
        [
            {
                "role": "user",
                "content": (
                    "If employer changes, then insurance changes."
                    if has_rule
                    else "I will ask if they have stock."
                ),
            },
            {"role": "user", "content": "My employer changed to Acme."},
        ],
    )
    saved = json.loads((await env.store.get_session(env.trace, 1))["extraction_output"])
    assert saved == {
        "items": [dict(i, type="rule") for i in rules["items"]]
        + [dict(i, type="content") for i in facts["items"]],
    }
    assert len(env.prompts) == 2 and not env.responses


@pytest.mark.asyncio
async def test_failed_fact_phase_does_not_repeat_successful_rules_or_save_memories(env):
    rules = {
        "items": [rule_item("If employer changes, then insurance changes")],
    }
    env.responses.extend([rules, "bad", "bad", "bad", "bad"])
    with pytest.raises(ExtractionFailed, match="facts"):
        await env.pipeline.ingest(
            env.trace,
            1,
            [{"role": "user", "content": "If employer changes, then insurance changes."}],
        )
    assert len(env.prompts) == 5 and not env.responses and not env.embedded
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memories WHERE run_id=$1 AND episode_id=$2",
                env.trace.run_id,
                env.trace.episode_id,
            )
            == 0
        )
        stages = await conn.fetch(
            "SELECT stage FROM memory_steps WHERE run_id=$1 AND episode_id=$2",
            env.trace.run_id,
            env.trace.episode_id,
        )
    assert sum(r["stage"] == "extraction.rules.input" for r in stages) == 1
    assert sum(r["stage"] == "extraction.facts.validation_error" for r in stages) == 4
    assert not any(r["stage"] == "extraction.parsed" for r in stages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_field", ["candidate_results", "rule_candidates", "reason"]
)
async def test_rule_phase_rejects_legacy_fields_and_retries_before_content(
    env, legacy_field
):
    rule = rule_item("If employer changes, then insurance changes")
    bad = {"items": [dict(rule)]}
    if legacy_field == "candidate_results":
        bad[legacy_field] = []
    else:
        bad["items"][0][legacy_field] = (
            ["R1"] if legacy_field == "rule_candidates" else "old explanation"
        )
    good = {"items": [rule]}

    def corrected(payload):
        assert "Correct the previous output:" in payload["messages"][0]["content"]
        return good

    env.responses.extend([bad, corrected, {"items": [item("Employer Acme")]}])
    items = await env.pipeline.ingest(
        env.trace,
        1,
        [
            {
                "role": "user",
                "content": "If employer changes, then insurance changes. Employer Acme.",
            }
        ],
    )
    assert [i.type for i in items] == ["rule", "content"]
    assert len(env.prompts) == 3 and not env.responses


@pytest.mark.asyncio
async def test_rule_input_has_only_full_session_and_no_candidate_list(env):
    def rules(payload):
        data = input_of(payload)
        assert set(data) == {"messages"}
        assert [m["message_no"] for m in data["messages"]] == [1, 2]
        assert data["messages"][0]["content"] == "My employer determines insurance."
        assert data["messages"][1]["content"] == "If that changes, insurance changes."
        return {
            "items": [rule_item("If the user's employer changes, their insurance changes.")]
        }

    env.responses.extend([rules, {"items": []}])
    items = await env.pipeline.ingest(
        env.trace,
        1,
        [
            {"role": "user", "content": "My employer determines insurance."},
            {"role": "user", "content": "If that changes, insurance changes."},
        ],
    )
    assert len(items) == 1 and items[0].type == "rule"
    assert set(items[0].__dict__) == {"text", "entities", "type", "temporal_scope", "topics"}
    assert not env.responses


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["A gift arrived.", "No conditions."])
async def test_no_standalone_if_skips_only_rule_call(env, content):
    def facts(payload):
        data = input_of(payload)
        assert data["rule_output"] == {"items": []}
        return {"items": []}

    env.responses.append(facts)
    assert (
        await env.pipeline.ingest(env.trace, 1, [{"role": "user", "content": content}])
        == []
    )
    assert len(env.prompts) == 1 and not env.responses
