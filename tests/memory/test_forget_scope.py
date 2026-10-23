"""Forgetting covers independent disclosures, beyond the initial retrieval hits."""

import httpx
import pytest
from dataclasses import replace

from contexthub.memory.contracts import DirectDecision, ref_of
from contexthub.memory.storage import SCOPE
from contexthub.memory.retriever import entity_pattern
from contexthub.memory.schemas import normalize_entities
from integrations.memebench.memory_research.runner import MemoryAnswerer
from .helpers import (
    add,
    direct_response,
    input_of,
    item,
    pending,
    row,
    session,
    targets_response,
)

NAME = "The user's partner's name is James."
COOKING = "The user and James enjoy cooking together."
RECIPE = (
    "The user's favorite recipe to make with James is dairy-free pasta."
)
COLLEAGUE = "The user's colleague James handles invoices."


@pytest.mark.asyncio
async def test_forget_independent_disclosures_and_answer_context(env, monkeypatch):
    env.retriever.config = env.retriever.config.model_copy(
        update={"hybrid_limit": 20, "text_k": 20, "vector_k": 20}
    )
    c = await session(env, 1)
    name = await add(env, c, NAME)
    cooking = await add(env, c, COOKING)
    recipe = await add(env, c, RECIPE)
    colleague = await add(env, c, COLLEAGUE)
    preference = await add(env, c, "The user likes pasta.")
    derived = await add(
        env, c, "Their partner enjoys shared meals.", origin="derived", basis=[cooking]
    )

    # Only the primary fact is retrieved. Independent disclosures must still be checked.
    async def primary_only(focus, context, *, query_vector):
        return {name: await row(env, name)}

    monkeypatch.setattr(env.retriever, "candidates", primary_only)

    def scope(payload):
        data = input_of(payload)
        assert set(data["request"]) == {"text"}
        assert all(set(r) == {"ref", "text", "type", "matter"} for r in data["candidates"])
        assert {r["text"] for r in data["candidates"]} == {
            NAME,
            COOKING,
            RECIPE,
            COLLEAGUE,
            "The user likes pasta.",
            "Their partner enjoys shared meals.",
        }
        # All deletions wait for the complete validated selection.
        return targets_response([NAME, COOKING, RECIPE, COLLEAGUE])(payload)

    env.responses.extend(
        [
            {
                "items": [
                    dict(item("Forget all records mentioning James."), entities=["James"]),
                    item("The user likes painting."),
                ]
            },
            direct_response("forget", NAME),
            scope,
            direct_response("new"),
        ]
    )
    await env.full_pipeline.ingest(
        env.trace,
        2,
        [{"role": "user", "content": "Forget all records mentioning James. I like painting."}],
    )
    for ref in [name, cooking, recipe, colleague]:
        assert (await row(env, ref))["status"] == "deleted"
    assert (await row(env, derived))["invalidated"]
    assert (await row(env, preference))["status"] == "fresh"
    c2 = replace(env.context, session_seq=2)
    for purpose in ["answer", "history", "p2"]:
        usable = await env.retriever.current(env.trace, 2, purpose=purpose)
        assert not {name, cooking, recipe, colleague, derived} & {ref_of(r) for r in usable}

    def answer(payload):
        rendered = payload["messages"][0]["content"]
        assert all(text not in rendered for text in [NAME, COOKING, RECIPE, COLLEAGUE])
        return "I don't know."

    env.responses.append(answer)
    answerer = MemoryAnswerer(
        env.store, env.retriever, env.executor.client, env.executor.retry
    )
    assert (
        await answerer.answer("What is my partner's name?", c2, timings={})
        == "I don't know."
    )
    assert not env.responses


@pytest.mark.asyncio
async def test_scope_includes_history_and_rules_but_excludes_other_episodes(env):
    env.retriever.config = env.retriever.config.model_copy(
        update={"hybrid_limit": 20, "text_k": 20, "vector_k": 20}
    )
    c = await session(env, 1)
    name = await add(env, c, NAME)
    old = await add(env, c, COOKING)
    rule_text = "If dinner is late, the user's partner James orders pasta."
    rule = await add(env, c, rule_text, type="rule")
    c2 = await session(env, 2)
    new, _ = await env.executor.apply_direct(
        pending(c2, "The user now cooks alone."),
        DirectDecision(action="update", target_ref=old),
        c2,
    )
    other_trace = env.trace.model_copy(update={"episode_id": "other-episode"})
    other = replace(c, trace=other_trace)
    await env.store.save_session(
        other_trace, 1, None, [{"role": "user", "content": NAME}]
    )
    other_ref, _ = await env.executor.apply_direct(
        pending(other, NAME),
        DirectDecision(action="new", target_ref=None),
        other,
    )

    def scope(payload):
        data = input_of(payload)
        assert len(data["candidates"]) == 4
        rows = {r["text"]: r for r in data["candidates"]}
        assert rows[COOKING]["matter"] == rows["The user now cooks alone."]["matter"]
        assert rows[rule_text]["type"] == "rule"
        return targets_response([NAME, COOKING, rule_text])(payload)

    env.responses.extend([direct_response("forget", NAME), scope])
    await env.executor.direct(
        pending(c2, "Forget my partner's name."), {name: await row(env, name)}, c2
    )
    for ref in [name, old, new, rule]:
        assert (await row(env, ref))["status"] == "deleted"
    assert (await env.store.read_ref(other_trace, other_ref))["status"] == "fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing_primary", "unknown", "duplicate", "json"])
async def test_scope_failure_skips_without_partial_deletion_and_continues(env, bad):
    c = await session(env, 1)
    name = await add(env, c, NAME)
    cooking = await add(env, c, COOKING)

    def wrong(payload):
        data = input_of(payload)
        primary = data["primary_ref"]
        if bad == "json":
            return "{bad"
        return {
            "target_refs": {
                "missing_primary": [],
                "unknown": [primary, "M999"],
                "duplicate": [primary, primary],
            }[bad]
        }

    env.responses.extend(
        [
            {
                "items": [
                    item("Forget my partner's name."),
                    item("The user likes painting."),
                ]
            },
            direct_response("forget", NAME),
            wrong,
            wrong,
            direct_response("new"),
        ]
    )
    await env.full_pipeline.ingest(
        env.trace,
        2,
        [{"role": "user", "content": "Forget my partner's name. I like painting."}],
    )
    for ref in [name, cooking]:
        assert (await row(env, ref))["status"] == "fresh"
    assert "The user likes painting." in {
        r["text"] for r in await env.store.current_rows(env.trace)
    }
    async with env.store.pool.acquire() as conn:
        stages = await conn.fetch(
            f"SELECT stage FROM memory_steps WHERE {SCOPE} ORDER BY step_id",
            *env.trace.args,
        )
    stages = [r["stage"] for r in stages]
    assert stages.count("p2_forget_scope.validation_error") == 2
    assert stages.count("p2.direct_skipped") == 1
    assert "p2.removal_applied" not in stages
    assert "session.complete" in stages
    assert not env.responses


@pytest.mark.asyncio
async def test_scope_transport_failure_is_not_swallowed(env):
    c = await session(env, 1)
    name = await add(env, c, NAME)
    env.responses.extend([direct_response("forget", NAME), 401])
    with pytest.raises(httpx.HTTPStatusError):
        await env.executor.direct(
            pending(c, "Forget my partner's name."), {name: await row(env, name)}, c
        )
    assert (await row(env, name))["status"] == "fresh"


@pytest.mark.parametrize("entity,text,matched", [
    ("James", "With JAMES, yesterday.", True),
    ("James", "Jameson's notes", False),
    ("Aurora", "Aurora's documentation", True),
    ("Cedar Labs", "CEDAR   LABS handles billing.", True),
    ("Hong Kong", "The venue is in Hong Kong.", True),
    ("github.com/aurora-team/docs", "Use github.com/aurora-team/docs.", True),
    ("github.com/aurora-team/docs", "github.com/aurora-team/docs/archive", False),
    ("github.com/aurora-team/docs", "github.com/aurora-team/docs?rev=1", False),
    ("github.com/aurora-team/docs", "othergithub.com/aurora-team/docs", False),
    ("github.com/aurora-team/docs", "https://github.com/aurora-team/docs", True),
    ("github.com/aurora-team/docs", "https://other.github.com/aurora-team/docs", False),
    ("github.com/aurora-team/docs", "https://example.org/github.com/aurora-team/docs", False),
])
def test_literal_entity_boundaries(entity, text, matched):
    assert bool(entity_pattern(normalize_entities([entity])[0]).search(
        normalize_entities([text])[0]
    )) is matched


@pytest.mark.asyncio
async def test_literal_candidates_cover_all_versions_without_top_k_or_tags(env):
    c = await session(env, 1)
    exact = {await add(env, c, f"Aurora note {i}") for i in range(35)}
    history = await add(env, c, "Aurora old address", scope="historical", status="stale")
    wrong = await add(env, c, "Aurora incorrect owner", status="stale")
    rule = await add(env, c, "If Aurora moves, change its address", type="rule")
    exact.update([history, wrong, rule])
    excluded = {
        await add(env, c, "Aurora deleted note", status="deleted"),
        await add(env, c, "Aurora pending note", accepted=False),
    }
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET invalidated=true,memory_embedding=NULL WHERE {SCOPE} AND memory_id=$4",
            *env.trace.args, (await row(env, wrong))["memory_id"],
        )
    c2 = await session(env, 2)
    excluded.add(await add(env, c2, "Aurora future note"))
    # Exact matches are independent of both the stored entity tags (empty in add)
    # and the semantic top-k, including a version with no vector at all.
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 1, "vector_k": 1, "hybrid_limit": 1, "entity_k": 0}
    )
    focus = dict(pending(c, "Forget all Aurora information."), entities=["Aurora", "invented"])
    candidates = await env.retriever.forget_candidates(focus, c)
    assert set(candidates) == exact
    assert not set(candidates) & excluded
    async with env.store.pool.acquire() as conn:
        payload = await conn.fetchval(
            f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage='retrieval.forget_candidates' ORDER BY step_id DESC LIMIT 1",
            *env.trace.args,
        )
    assert payload["entities"] == ["aurora"]
    assert set(payload["exact_refs"]) == exact
    assert payload["semantic_refs"] == []


@pytest.mark.asyncio
async def test_semantic_supplement_has_its_own_budget_and_includes_implicit_history(env):
    c = await session(env, 1)
    exact = await add(env, c, "Aurora documentation is at docs.example.org")
    implicit_text = "The project's former documentation address was old.example.org"
    implicit = await add(env, c, implicit_text, scope="historical", status="stale")
    env.vectors["document: Pottery class on Friday"] = [-1.0, 0.0, 0.0]
    unrelated = await add(env, c, "Pottery class on Friday")
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 1, "vector_k": 1, "hybrid_limit": 1, "entity_k": 0}
    )
    focus = dict(pending(c, "Forget Aurora's documentation address"), entities=["Aurora"])
    assert set(await env.retriever.forget_candidates(focus, c)) == {exact, implicit}
    # With no explicit entity, retrieval still supplies an implicit mention.
    focus = pending(c, "Forget the project's former documentation address")
    candidates = await env.retriever.forget_candidates(focus, c, primary=exact)
    assert set(candidates) == {exact, implicit}
    assert unrelated not in candidates


@pytest.mark.asyncio
async def test_entity_match_does_not_delete_unselected_project_properties(env):
    c = await session(env, 1)
    address = await add(env, c, "Aurora documentation URL is github.com/aurora-team/docs")
    owner = await add(env, c, "Aurora owner is Li")
    budget = await add(env, c, "Aurora budget is 500")
    runbook = await add(env, c, "Aurora incident runbook is docs.aurora.internal/runbooks")
    c2 = await session(env, 2)
    focus = dict(pending(c2, "Forget Aurora's documentation URL"), entities=["Aurora"])

    def select_address(payload):
        data = input_of(payload)
        assert len(data["candidates"]) == 4
        return targets_response(["Aurora documentation URL is github.com/aurora-team/docs"])(payload)

    env.responses.extend([direct_response("forget", "Aurora documentation URL is github.com/aurora-team/docs"), select_address])
    await env.executor.direct(focus, {address: await row(env, address)}, c2)
    assert (await row(env, address))["status"] == "deleted"
    for key in (owner, budget, runbook):
        assert (await row(env, key))["status"] == "fresh"


@pytest.mark.asyncio
async def test_relationship_forget_can_remove_shared_activity_clues_and_all_versions(env):
    c = await session(env, 1)
    name = await add(env, c, NAME)
    cooking = await add(env, c, COOKING)
    colleague_text = "A different person, the user's colleague James, handles invoices."
    colleague = await add(env, c, colleague_text)
    preference_text = "The user likes pasta."
    preference = await add(env, c, preference_text)
    c2 = await session(env, 2)
    merged_text = COOKING + " " + RECIPE
    merged, _ = await env.executor.apply_direct(
        pending(c2, merged_text), DirectDecision(action="update", target_ref=cooking), c2,
    )
    focus = dict(pending(c2, "Forget that my partner is James."), entities=["James"])

    def scope(payload):
        prompt = payload["messages"][0]["content"]
        assert "Do not retain these identifying clues merely because a record omits the relationship label" in prompt
        candidates = {r["text"] for r in input_of(payload)["candidates"]}
        assert {NAME, COOKING, merged_text, colleague_text} <= candidates
        # Fixed model output tests application of the expanded scope, not real-model accuracy.
        return targets_response([NAME, merged_text])(payload)

    env.responses.extend([direct_response("forget", NAME), scope])
    await env.executor.direct(focus, {name: await row(env, name)}, c2)
    for key in (name, cooking, merged):
        assert (await row(env, key))["status"] == "deleted"
    for key in (colleague, preference):
        assert (await row(env, key))["status"] == "fresh"
    assert not env.responses


@pytest.mark.asyncio
async def test_supplement_reuses_filter_for_rules_history_and_current_without_extra_embedding(env):
    from .test_direct_candidate_filter import vector
    env.retriever.config = env.retriever.config.model_copy(
        update={"hybrid_limit": 20, "text_k": 20, "vector_k": 20},
    )
    c = await session(env, 1)
    texts = {
        'exact': 'Aurora owner is Li.',
        'history': 'The project formerly used courier Oak.',
        'current': 'The project uses courier Cedar.',
        'rule': 'If dispatch changes, then revise the delivery plan.',
        'noise': 'Pottery class on Friday.',
    }
    for key, similarity in [('exact', 0.1), ('history', 0.8), ('current', 0.1), ('rule', 0.8), ('noise', 0.4)]:
        env.vectors['document: ' + texts[key]] = vector(similarity)
    exact = await add(env, c, texts['exact'])
    history = await add(env, c, texts['history'])
    rule = await add(env, c, texts['rule'], type='rule')
    noise = await add(env, c, texts['noise'])
    c2 = await session(env, 2)
    current, _ = await env.executor.apply_direct(
        pending(c2, texts['current']), DirectDecision(action='update', target_ref=history), c2,
    )
    before = len(env.embedded)
    focus = dict(pending(c2, 'Forget Aurora delivery information.'), entities=['Aurora'])
    selected = await env.retriever.forget_candidates(focus, c2, query_vector='[1,0,0]')
    assert set(selected) == {exact, history, current, rule}
    assert noise not in selected
    assert len(env.embedded) == before
    async with env.store.pool.acquire() as conn:
        audit = await conn.fetchval(
            f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage='p2_forget_scope.candidates' ORDER BY step_id DESC LIMIT 1", *env.trace.args,
        )
    decisions = {d['ref']: d for d in audit['candidates']}
    assert exact not in decisions  # Literal matches bypass the semantic filter.
    assert decisions[current]['reason'] == 'matched_matter_current'
    assert decisions[noise]['reason'] == 'insufficient_support'


@pytest.mark.asyncio
async def test_forget_supplement_may_be_empty(env):
    c = await session(env, 1)
    text = 'Pottery class on Friday.'
    env.vectors['document: ' + text] = [0, 1, 0]
    await add(env, c, text)
    before = len(env.embedded)
    assert await env.retriever.forget_candidates(
        pending(c, 'Forget the old invoice.'), c, query_vector='[1,0,0]',
    ) == {}
    assert len(env.embedded) == before
