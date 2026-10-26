from .helpers import extraction_response
from .helpers import rule_item
"""P1's loose cutoff is applied to both sides of the complete merged pool."""

import json
import math

import pytest

from contexthub.memory.contracts import DirectDecision, rule_queries, split_ref
from contexthub.memory.extractor import parse_items
from contexthub.memory.retriever import P1_MIN_SIMILARITY
from contexthub.memory.storage import SCOPE
from .helpers import add, bindings_response, input_of, link, pending, row, session


@pytest.mark.parametrize("text,condition,affected", [
    ("If my health changes, I will cycle.", "my health changes", "I will cycle."),
    ("If rain, snow or fog arrives, then move indoors, start at 3pm.",
     "rain, snow or fog arrives", "move indoors, start at 3pm."),
    ("If rain AND no canopy, then the venue and start time may change.",
     "rain AND no canopy", "the venue and start time may change."),
])
def test_rule_queries_preserve_complete_conditions_and_consequences(text, condition, affected):
    assert rule_queries(text) == {"condition": condition, "affected": affected}


@pytest.mark.parametrize("text", ["Health affects exercise", "If health changes", "If , then cycle", "If rain, ", "If rain, then"])
def test_rule_without_query_boundary_is_rejected_during_extraction(text):
    with pytest.raises(ValueError):
        parse_items(json.dumps(dict(items=[{"text": text, "entities": [], "topics": []}])), kind="rule")


def vector(x, y=0):
    return [x, y, math.sqrt(1 - x*x - y*y)]


def queries(env):
    env.vectors.update({
        "query: health changes": [1, 0, 0],
        "query: exercise becomes cycling": [0, 1, 0],
    })


async def audit(env):
    async with env.store.pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage='p1.candidates' ORDER BY step_id DESC LIMIT 1",
            *env.trace.args,
        )


@pytest.mark.asyncio
async def test_merge_refresh_deduplicate_then_filter_either_side(env):
    queries(env)
    c1 = await session(env, 1)
    rule = await add(env, c1, "If health changes, then exercise becomes cycling", type="rule")
    env.vectors["document: Exercise swimming"] = vector(0, .7)
    target = await add(env, c1, "Exercise swimming")
    env.vectors["document: Old superficially similar fact"] = vector(.9)
    old = await add(env, c1, "Old superficially similar fact")
    c2 = await session(env, 2)
    env.vectors["document: Current unrelated fact"] = vector(.1, .1)
    latest, _ = await env.executor.apply_direct(
        pending(c2, "Current unrelated fact"), DirectDecision(action="update", target_ref=old), c2
    )
    env.vectors["document: Lactose intolerance"] = vector(.2108)
    health = await add(env, c2, "Lactose intolerance")
    env.vectors["document: Hosting criteria"] = vector(.12, .1478)
    hosting = await add(env, c2, "Hosting criteria")
    deleted = await add(env, c2, "Deleted high similarity", status="deleted")
    unaccepted = await add(env, c2, "Pending high similarity", accepted=False)
    historical = await add(env, c2, "Historical high similarity", scope="historical")

    def select(payload):
        data = input_of(payload)
        assert data["rule"]["text"] == "If health changes, then exercise becomes cycling"
        assert {r["text"] for r in data["candidates"]} == {"Exercise swimming", "Lactose intolerance"}
        assert len(data["candidates"]) == 2
        return bindings_response(["Lactose intolerance"], ["Exercise swimming"])(payload)

    env.responses.append(select)
    initial = {key: await row(env, key) for key in [target, old, latest, deleted, unaccepted, historical]}
    await env.discovery.bind({rule: initial, health: {}, hosting: {}}, c2)
    saved = await row(env, rule)
    assert saved["condition_matter_ids"] == [split_ref(health)[0]]
    assert saved["affected_matter_ids"] == [split_ref(target)[0]]
    logged = await audit(env)
    assert logged["min_similarity"] == .15
    assert {r["ref"] for r in logged["candidates"]} == {target, latest, health, hosting}
    assert set(logged["selected_refs"]) == {target, health}
    assert env.embedded[-2:] == ["query: health changes", "query: exercise becomes cycling"]
    assert not env.responses


@pytest.mark.asyncio
async def test_no_top_k_and_cutoff_applies_to_new_and_previously_bound_candidates(env):
    queries(env)
    c = await session(env, 1)
    rule = await add(env, c, "If health changes, then exercise becomes cycling", type="rule")
    facts = {}
    for i, score in enumerate([.1499, .15, .1501, .16, .18, .21, .25]):
        text = f"Fact {i}"
        env.vectors["document: " + text] = vector(score)
        facts[text] = await add(env, c, text)
    env.vectors["document: Prior target"] = vector(.1, .1)
    prior = await add(env, c, "Prior target")
    await link(env, rule, targets=[prior])

    def select(payload):
        candidates = input_of(payload)["candidates"]
        assert {r["text"] for r in candidates} == set(facts) - {"Fact 0"}
        assert len(candidates) > env.retriever.config.hybrid_limit
        return {"condition_refs": [], "affected_refs": []}

    env.responses.append(select)
    await env.discovery.bind({rule: {}, **{key: {} for key in facts.values()}}, c)
    assert (await row(env, rule))["affected_matter_ids"] == [split_ref(prior)[0]]
    logged = await audit(env)
    assert len(logged["candidates"]) == 8
    assert len(logged["selected_refs"]) == 6
    assert P1_MIN_SIMILARITY == .15


@pytest.mark.asyncio
async def test_all_filtered_skips_binding_and_preserves_existing_endpoint(env):
    queries(env)
    c = await session(env, 1)
    rule = await add(env, c, "If health changes, then exercise becomes cycling", type="rule")
    env.vectors["document: Prior source"] = vector(.1, .1)
    source = await add(env, c, "Prior source")
    await link(env, rule, sources=[source])
    assert await env.discovery.bind({rule: {}}, c) == []
    assert not env.prompts
    assert (await row(env, rule))["condition_matter_ids"] == [split_ref(source)[0]]
    assert (await audit(env))["selected_refs"] == []


@pytest.mark.asyncio
async def test_invalid_rule_boundary_retries_existing_extraction_stage(env):
    env.responses.extend([
        extraction_response(rules=[dict(rule_item("Health affects exercise"), candidate_ref="R1")], contents=[]),
        extraction_response(rules=[dict(rule_item("If health changes, then exercise changes"), candidate_ref="R1")], contents=[]),
    ])
    items = await env.pipeline.ingest(env.trace, 1, [{"role": "user", "content": "If health changes, exercise changes"}])
    assert items[0].text == "If health changes, then exercise changes"
    assert len(env.prompts) == 2 and not env.responses


@pytest.mark.asyncio
async def test_rule_correction_preserves_parseable_query_boundary(env):
    c = await session(env, 1)
    rule = await add(env, c, "If health changes, then exercise changes", type="rule")

    def correction(text):
        def response(payload):
            # The retry appends validation feedback after the original JSON.
            prompt = payload["messages"][0]["content"].split("\nCorrect the previous output", 1)[0]
            data = json.loads(next(line for line in prompt.splitlines() if line.startswith('{"incoming"')))
            return {"operations": [{"action": "correct", "target_ref": data["candidates"][0]["ref"],
                                    "text": text, "entities": []}]}
        return response

    env.responses.extend([correction("Health affects exercise"),
                          correction("If health changes, then exercise becomes cycling")])
    plan = await env.executor.plan_direct("correction", pending(c, "Correct the exercise rule"),
                                          {rule: await row(env, rule)}, c)
    assert plan.operations[0].text == "If health changes, then exercise becomes cycling"
    assert len(env.prompts) == 2 and not env.responses
