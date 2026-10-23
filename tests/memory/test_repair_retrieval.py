from .helpers import link
"""Condition evidence first; a specific missing fact may request one small search."""

from .helpers import repair_target

import pytest

from contexthub.memory.contracts import DirectDecision
from .helpers import add, input_of, pending, repair_response, row, session, trigger_response


def unresolved(query=None):
    return dict(can_repair=False, text=None, entities=[], basis_refs=[], retrieval_query=query)


@pytest.mark.asyncio
async def test_selected_causes_current_versions_and_rules_need_no_search(env, monkeypatch):
    c1 = await session(env, 1)
    exercise = await add(env, c1, "Exercise swimming")
    vague = await add(env, c1, "If exercise changes, venue changes.", type="rule")
    specific = await add(env, c1, "If exercise changes, venue becomes Velthari.", type="rule")
    target = await add(env, c1, "Venue Crysthene")
    await add(env, c1, "Hobby pottery")
    await link(env, vague, sources=[exercise], targets=[target])
    await link(env, specific, sources=[exercise], targets=[target])
    c2 = await session(env, 2)
    _, unrelated = await env.executor.apply_direct(
        pending(c2, "Company offsite"), DirectDecision(action="new", target_ref=None), c2,
    )
    _, related = await env.executor.apply_direct(
        pending(c2, "Exercise cycling"), DirectDecision(action="update", target_ref=exercise), c2,
    )
    env.responses.append(trigger_response(["Venue Crysthene"], ["C2"]))
    await env.executor.check_conditions(unrelated + related, set(), c2)

    async def no_search(*args, **kwargs):
        pytest.fail("Selected changes supply the premises; no retrieval should occur")

    monkeypatch.setattr(env.retriever, "search", no_search)

    def insufficient(payload):
        data = input_of(payload)
        assert {m["text"] for m in data["materials"]} == {
            "Exercise cycling", "If exercise changes, venue changes.",
            "If exercise changes, venue becomes Velthari.",
        }
        assert "target" not in data
        assert repair_target(payload) == "Venue Crysthene"
        assert all(set(m) == {"ref", "text", "type"} for m in data["materials"])
        assert len(data["changes"]) == 1
        change = data["changes"][0]
        assert change["before"]["text"] == "Exercise swimming"
        assert change["before"]["status_now"] == "stale"
        assert change["after"]["text"] == "Exercise cycling"
        assert set(change["before"]) == {"text", "type", "status_now"}
        return unresolved()

    env.responses.append(insufficient)
    assert await env.executor.repair(await row(env, target), unrelated + related, c2, set()) is None
    # Starting a new session does not discard the selected before/after event.
    c3 = await session(env, 3)
    materials, evidence = await env.executor.repair_evidence(await row(env, target), [], c3)
    data, _ = await env.executor.repair_inputs(
        await row(env, target), materials, evidence, c3, retrieval_available=True,
    )
    assert data["changes"][0]["before"]["text"] == "Exercise swimming"
    assert data["changes"][0]["after"]["text"] == "Exercise cycling"
    count = len(env.prompts)
    assert await env.executor.repair(await row(env, target), [], c3, set()) is None
    assert len(env.prompts) == count
    assert not env.responses


@pytest.mark.asyncio
async def test_missing_evidence_search_is_focused_bounded_and_cached(env):
    c1 = await session(env, 1)
    rule_text = "If health improves, exercise becomes cycling."
    rule = await add(env, c1, rule_text, type="rule")
    target = await add(env, c1, "Exercise swimming", status="stale")
    health = await add(env, c1, "Health status unconfirmed")
    await link(env, rule, targets=[target])
    c2 = await session(env, 2)
    seen = []

    def request_evidence(payload):
        data = input_of(payload)
        seen.append(data)
        assert [m["text"] for m in data["materials"]] == [rule_text]
        assert data["retrieval_available"] is True
        return unresolved("Current health improvement or recovery")

    def not_yet(payload):
        data = input_of(payload)
        seen.append(data)
        assert data["retrieval_available"] is False
        assert {m["text"] for m in data["materials"]} == {rule_text, "Health status unconfirmed"}
        return unresolved()

    env.responses.extend([request_evidence, not_yet])
    assert await env.executor.repair(await row(env, target), [], c2, set()) is None
    # The evidence request is reused; the same retrieved evidence needs no new LLM call.
    assert await env.executor.repair(await row(env, target), [], c2, set()) is None
    assert len(env.prompts) == 2
    assert "Current health improvement or recovery" in env.embedded[-1]
    new_health = "Health has improved."
    _, changes = await env.executor.apply_direct(
        pending(c2, new_health), DirectDecision(action="update", target_ref=health), c2,
    )
    env.responses.append(repair_response("Exercise cycling", [rule_text, new_health]))
    result = await env.executor.repair(await row(env, target), changes, c2, set())
    assert result["after"]["text"] == "Exercise cycling"
    assert not env.responses


@pytest.mark.asyncio
async def test_search_only_supplies_five_current_facts_and_cannot_repeat(env):
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 20, "vector_k": 20, "hybrid_limit": 20}
    )
    c = await session(env, 1)
    rule = await add(env, c, "If owner changes, recipient follows owner.", type="rule")
    target = await add(env, c, "Recipient Wang", status="stale")
    await link(env, rule, targets=[target])
    for n in range(8):
        await add(env, c, f"Candidate owner {n}")
    env.responses.append(unresolved("Current owner"))

    def second(payload):
        data = input_of(payload)
        assert data["retrieval_available"] is False
        assert len([m for m in data["materials"] if m["type"] == "fact"]) == 5
        # Trying another retrieval is a validation error, not another search.
        return unresolved("Another owner search")

    env.responses.extend([second, unresolved()])
    assert await env.executor.repair(await row(env, target), [], c, set()) is None
    assert not env.responses
    async with env.store.pool.acquire() as conn:
        searches = await conn.fetchval(
            "SELECT count(*) FROM memory_steps WHERE run_id=$1 AND episode_id=$2 AND stage='p2.repair_retrieval'",
            env.trace.run_id, env.trace.episode_id,
        )
    assert searches == 1
