"""Current answers omit stale statements and IDs; history can still use valid old facts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from contexthub.memory.contracts import DirectDecision, ref_of
from contexthub.memory.prompts_config import PromptSet
from pathlib import Path
prompt = PromptSet(Path(__file__).resolve().parents[2] / "integrations/memebench/memory_research/prompts").text
from contexthub.memory.retriever import answer_purpose
from integrations.memebench.memory_research.runner import MemoryAnswerer
from contexthub.memory.context import answer_context
from .helpers import add, pending, row, session


@pytest.mark.asyncio
async def test_answer_omits_stale_statements_and_refs_but_keeps_internal_audit(env):
    c = await session(env, 1)
    old_text = "The user's regular appointment is weekly tutoring."
    appointment = await add(env, c, old_text, status="stale")
    current_text = "The user's social meetup is trivia on Wednesdays."
    meetup = await add(env, c, current_text)
    deleted_text = "A secret appointment with James."
    deleted = await add(env, c, deleted_text)
    await env.store.forget(env.trace, (await row(env, deleted))["memory_id"], "forget")
    invalid_text = "An incorrect appointment."
    invalid = await add(env, c, invalid_text, status="stale")
    async with env.store.pool.acquire() as conn:
        await env.executor.mutate(invalid, {"invalidated": True}, c, conn)
    stale_rule_text = "If health changes appointments change."
    stale_rule = await add(env, c, stale_rule_text, type="rule", status="stale")
    rule_text = "If the office closes, then meetings move online."
    rule = await add(env, c, rule_text, type="rule")

    def answer(payload):
        text = payload["messages"][0]["content"]
        data = json.JSONDecoder().raw_decode(text.split("Context:\n", 1)[1])[0]
        assert data == {
            "purpose": "answer",
            "memories": [{"text": current_text, "type": "fact"}],
        }
        assert all(value not in text for value in (
            old_text, deleted_text, invalid_text, stale_rule_text,
            appointment, deleted, invalid, stale_rule, meetup, rule, rule_text,
        ))
        assert "conditional_rules" not in data
        assert text == prompt("evaluation/answer").format(
            context=json.dumps(data, ensure_ascii=False),
            question="What regular appointments do I have?",
        )
        return "Trivia on Wednesdays."

    env.responses.append(answer)
    answerer = MemoryAnswerer(env.store, env.retriever, env.executor.client, env.executor.retry)
    await answerer.answer("What regular appointments do I have?", c, timings={})
    async with env.store.pool.acquire() as conn:
        audit = await conn.fetchval(
            "SELECT payload FROM memory_steps WHERE tenant_id=$1 AND namespace=$2 "
            "AND stage='answer.input' ORDER BY step_id DESC LIMIT 1",
            env.trace.tenant_id, env.trace.namespace,
        )
    assert {m["ref"] for m in audit["materials"]} == {meetup}
    assert audit["purpose"] == "answer"
    for purpose in ("answer", "p2"):
        assert appointment not in {
            ref_of(r) for r in await env.retriever.current(env.trace, 1, purpose=purpose)
        }
    assert (await row(env, appointment))["status"] == "stale"
    history = await env.retriever.current(env.trace, 1, purpose="history")
    assert appointment in {ref_of(r) for r in history}

    c2 = await session(env, 2)
    replacement, _ = await env.executor.apply_direct(
        pending(c2, "The user's regular appointment is Friday tutoring."),
        DirectDecision(action="update", target_ref=appointment), c2,
    )
    hits = await env.retriever.search("regular appointments", c2, through_session=2, purpose="answer")
    refs = {ref_of(h.memory) for h in hits}
    assert replacement in refs and appointment not in refs
    assert (await row(env, replacement))["status"] == "fresh"


def test_history_answer_keeps_temporal_evidence_without_internal_refs():
    source = {
        "ref": "internal-memory:2", "basis_refs": ["internal-rule:1"],
        "text": "Previously worked on Mondays.", "status": "stale",
        "temporal_scope": "historical", "created_in_session": 3, "timestamp": "2026-01-01",
    }
    context = answer_context([source], "history")
    assert context["memories"] == [{k:v for k,v in source.items() if k not in ("ref", "basis_refs")}]
    assert "internal-memory" not in json.dumps(context)
    assert "internal-rule" not in json.dumps(context)
    assert "time_note" in context
    assert source["ref"] == "internal-memory:2"


@pytest.mark.parametrize("question,purpose", [
    ("What's my favorite restaurant?", "answer"),
    ("What's my health condition?", "answer"),
    ("What allergies do I have, if any?", "answer"),
    ("What are our code quality standards? List everything you know.", "answer"),
    ("What is our code review policy?", "policy"),
    ("Under what conditions should we change the deployment?", "policy"),
    ("What would happen if our supplier changed?", "policy"),
    ("If the office closes, where should meetings be held?", "policy"),
    ("如果团队搬迁，会议地点会怎样变化？", "policy"),
    ("团队的代码审查规定是什么？", "policy"),
    ("我现在最喜欢哪家餐馆？", "answer"),
    ("List sprint deadlines from earliest to latest.", "history"),
])
def test_question_routes_current_facts_policy_and_history(question, purpose):
    assert answer_purpose(question) == purpose


@pytest.mark.asyncio
async def test_rule_excluded_before_top_k_and_policy_can_still_read_it(env):
    c = await session(env, 1)
    old_name = "Zefiro's Kitchen"
    rule_text = f"If I move, then my favorite restaurant, {old_name}, changes."
    rule = await add(env, c, rule_text, type="rule")
    await add(env, c, f"My favorite restaurant is {old_name}.", status="stale")
    fact_text = "My favorite restaurant is currently unknown after moving."
    fact = await add(env, c, fact_text)
    env.retriever.config = env.retriever.config.model_copy(update={
        "text_k": 1, "vector_k": 1, "hybrid_limit": 1, "entity_k": 0,
    })
    # Make the rule outrank the valid fact unless excluded before ranking.
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET memory_embedding='[0,1,0]'::vector WHERE memory_id=$1",
            (await row(env, fact))["memory_id"],
        )

    def current_answer(payload):
        text = payload["messages"][0]["content"]
        assert fact_text in text and old_name not in text
        assert "conditional_rules" not in text
        return "I don't have that information."

    def policy_answer(payload):
        text = payload["messages"][0]["content"]
        data = json.JSONDecoder().raw_decode(text.split("Context:\n", 1)[1])[0]
        assert data["purpose"] == "policy"
        assert data["conditional_rules"] == [{"text": rule_text}]
        assert text == prompt("evaluation/answer").format(
            context=json.dumps(data, ensure_ascii=False),
            question=f"Under what conditions does {old_name} change?",
        )
        return "The favorite restaurant may change."

    env.responses.extend([current_answer, policy_answer])
    answerer = MemoryAnswerer(env.store, env.retriever, env.executor.client, env.executor.retry)
    await answerer.answer("What's my favorite restaurant?", c, timings={})
    await answerer.answer(f"Under what conditions does {old_name} change?", c, timings={})
    assert (await row(env, rule))["status"] == "fresh"
    assert len(env.prompts) == 2  # no extra classifier call


@pytest.mark.asyncio
async def test_answer_uses_official_template_once():
    store = SimpleNamespace(record=AsyncMock())
    retriever = SimpleNamespace(search=AsyncMock(return_value=[]))
    client = SimpleNamespace(complete=AsyncMock(return_value="I don't have that information."))
    context = SimpleNamespace(session_seq=1, make=lambda *args, **kwargs: None)
    question = "What is the value of {setting}?"
    answerer = MemoryAnswerer(store, retriever, client, retry=None)

    await answerer.answer(question, context, timings={})

    expected = prompt("evaluation/answer").format(
        context='{"purpose": "answer", "memories": []}', question=question,
    )
    client.complete.assert_awaited_once_with(expected, max_tokens=500, call=None)
    assert store.record.await_args_list[0].args[2]["prompt"] == expected
