from .helpers import extraction_response
from .helpers import rule_item
from .helpers import bindings_response
from .helpers import link
"""Shared retrieval, same-session acceptance, and condition coverage on real PG."""

import pytest

from contexthub.memory.contracts import split_ref
from contexthub.memory.retriever import SearchHit
from contexthub.memory.storage import SCOPE
from .helpers import (
    add,
    direct_response,
    input_of,
    item,
    row,
    session,
    targets_response,
    trigger_response,
)


@pytest.mark.asyncio
async def test_initial_retrieval_then_split_filter_includes_same_session_facts(env):
    rule = "If health changes, then exercise becomes cycling"
    fact = "Exercise swimming"
    request = "Forget an unknown address"
    seen = []

    def decide(payload):
        data = input_of(payload)
        # All four query embeddings already happened before the first decision.
        assert len([s for s in env.embedded if s.startswith("query: ")]) == 4
        texts = {r["text"] for r in data["candidates"]}
        assert request not in texts
        incoming = data["incoming"]["text"]
        if incoming == rule:
            assert texts == set()  # The future swimming item is not yet a fact.
            result = dict(action="new", target_ref=None)
        elif incoming == request:
            assert texts == {rule, fact}
            result = dict(action="hold", target_ref=None)
        elif fact in seen:
            result = direct_response("duplicate", fact)(payload)
        else:
            assert texts == {rule}
            result = dict(action="new", target_ref=None)
        seen.append(incoming)
        return result

    env.responses.extend(
        [
            extraction_response(rules=[dict(rule_item(rule), candidate_ref="R1")], contents=[item(fact), item(request), item(fact)]),
            *([decide] * 4),
            bindings_response([], [fact]),
        ]
    )
    refs = await env.full_pipeline.ingest(
        env.trace, 1, [dict(role="user", content=rule)]
    )
    assert refs[1] == refs[2] and len(refs) == 3
    current = await env.store.current_rows(env.trace)
    saved_rule = next(r for r in current if r["type"] == "rule")
    saved_fact = next(r for r in current if r["type"] == "fact")
    assert len(current) == 2
    assert saved_rule["affected_matter_ids"] == [saved_fact["memory_id"]]
    assert not env.responses
    assert len([s for s in env.embedded if s.startswith("query: ")]) == 6
    async with env.store.pool.acquire() as conn:
        steps = await conn.fetch(
            f"SELECT stage FROM memory_steps WHERE {SCOPE} ORDER BY step_id",
            *env.trace.args,
        )
    stages = [r["stage"] for r in steps]
    assert stages.count("retrieval") == 4
    assert max(i for i, s in enumerate(stages) if s == "retrieval") < stages.index(
        "p2_direct.input"
    )


@pytest.mark.asyncio
async def test_incoming_rule_retrieves_both_types_and_reuses_fact_hits_for_binding(env):
    c = await session(env, 1)
    await add(env, c, "If exercise changes, then location changes", type="rule")
    fact = await add(env, c, "Exercise swimming")
    rule = "If health changes, then exercise becomes cycling"
    before = len(env.embedded)

    def decide(payload):
        data = input_of(payload)
        assert {r["type"] for r in data["candidates"]} == {"fact", "rule"}
        return dict(action="new", target_ref=None)

    env.responses.extend(
        [
            extraction_response(rules=[dict(rule_item(rule), candidate_ref="R1")], contents=[]),
            decide,
            bindings_response([], ["Exercise swimming"]),
            dict(triggers=[]),
        ]
    )
    refs = await env.full_pipeline.ingest(
        env.trace, 2, [dict(role="user", content=rule)]
    )
    assert (await row(env, f"{refs[0][0]}:{refs[0][1]}"))["affected_matter_ids"] == [split_ref(fact)[0]]
    assert len([s for s in env.embedded[before:] if s.startswith("query: ")]) == 3
    assert not env.responses


@pytest.mark.asyncio
async def test_new_fact_uses_old_rule_from_shared_retrieval(env):
    c = await session(env, 1)
    rule = await add(env, c, "If health changes, then exercise becomes cycling", type="rule")
    before = len(env.embedded)
    env.responses.extend(
        [
            extraction_response(rules=[], contents=[item("Exercise swimming")]),
            dict(action="new", target_ref=None),
            bindings_response([], ["Exercise swimming"]),
            dict(triggers=[]),
        ]
    )
    refs = await env.full_pipeline.ingest(
        env.trace, 2, [dict(role="user", content="Exercise swimming")]
    )
    assert (await row(env, rule))["affected_matter_ids"] == [refs[0][0]]
    assert len([s for s in env.embedded[before:] if s.startswith("query: ")]) == 3
    assert not env.responses


@pytest.mark.asyncio
async def test_cached_candidates_refresh_deleted_and_superseded_versions(env):
    c = await session(env, 1)
    owner = await add(env, c, "Owner Wang")
    partner = await add(env, c, "Partner James")
    contents = ["Owner Li", "Forget partner", "Owner Zhao", "Forget partner again"]

    def third(payload):
        data = input_of(payload)
        assert "Partner James" not in {r["text"] for r in data["candidates"]}
        current_texts = {r["text"] for r in data["candidates"] if r["is_current"]}
        assert current_texts == {"Owner Li"}
        old = next(r for r in data["candidates"] if r["text"] == "Owner Wang")
        assert old["status"] == "stale"
        return direct_response("update", "Owner Li")(payload)

    def fourth(payload):
        assert "Partner James" not in {
            r["text"] for r in input_of(payload)["candidates"]
        }
        return dict(action="hold", target_ref=None)

    env.responses.extend(
        [
            extraction_response(rules=[], contents=[item(t) for t in contents]),
            direct_response("update", "Owner Wang"),
            direct_response("forget", "Partner James"),
            targets_response(["Partner James"]),
            third,
            fourth,
        ]
    )
    await env.full_pipeline.ingest(
        env.trace, 2, [dict(role="user", content="Owner and partner changes")]
    )
    assert (await row(env, partner))["status"] == "deleted"
    versions = await env.store.versions(env.trace, split_ref(owner)[0])
    assert [r["text"] for r in versions] == ["Owner Wang", "Owner Li", "Owner Zhao"]
    assert not env.responses


@pytest.mark.asyncio
async def test_condition_check_covers_bound_fact_missing_from_retrieval(
    env, monkeypatch
):
    c = await session(env, 1)
    rule = await add(env, c, "If health changes, then exercise changes", type="rule")
    exercise = await add(env, c, "Exercise swimming")
    health = await add(env, c, "Health lactose intolerance")

    await link(env, rule, sources=[health], targets=[exercise])
    async def health_only(*args, **kwargs):
        current = await env.store.current_rows(env.trace)
        health_row = next(r for r in current if r["memory_id"] == split_ref(health)[0])
        return [SearchHit(health_row, 1.0, ("vector",))]

    monkeypatch.setattr(env.retriever, "search", health_only)

    def decide(payload):
        assert {r["text"] for r in input_of(payload)["candidates"]} == {
            "Health lactose intolerance"
        }
        return direct_response("update", "Health lactose intolerance")(payload)

    env.responses.extend(
        [
            extraction_response(rules=[], contents=[item("Health high blood pressure")]),
            decide,
            trigger_response(["Exercise swimming"], ["C1"]),
            dict(can_repair=False, text=None, entities=[], basis_refs=[], retrieval_query=None),
        ]
    )
    await env.full_pipeline.ingest(
        env.trace, 2, [dict(role="user", content="Health changed")]
    )
    assert (await row(env, exercise))["status"] == "stale"
    assert not env.responses
