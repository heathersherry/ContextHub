from .test_jev_direct import process_item
"""Direct-input context pruning with real PG and fixed model/vector fixtures."""

import math

import pytest

from contexthub.memory.contracts import DirectDecision, split_ref
from contexthub.memory.direct_candidates import select_candidates
from contexthub.memory.schemas import ExtractedItem
from contexthub.memory.storage import SCOPE
from .helpers import add, pending, row, session
from .test_jev_direct import jev  # noqa: F401


def score(ref, similarity, lexemes=(), entities=(), identifiers=()):
    return dict(ref=ref, similarity=similarity, lexemes=list(lexemes),
                entities=list(entities), identifiers=list(identifiers))


def test_boundaries_and_value_change_do_not_require_same_entity():
    corpus = [
        dict(memory_id="car", lexemes=["user", "drive", "sedan"]),
        dict(memory_id="shop", lexemes=["user", "kitchen"]),
    ]
    decisions, _ = select_candidates([
        score("paraphrase", 0.50),
        score("car", 0.4349, ["user", "drive", "sedan"]),
        score("unrelated", 0.4356, ["user", "kitchen"]),
        score("boundary", 0.35, ["drive"]),
        score("below", 0.349999, ["drive"]),
        score("name", 0.35, entities=["james"]),
        score("name_below", 0.349999, entities=["james"]),
        score("identifier", -0.2, identifiers=["ticket-17"]),
    ], corpus, ["user", "drive", "scooter", "recent"])
    assert {d["ref"] for d in decisions if d["selected"]} == {
        "paraphrase", "car", "boundary", "name", "identifier",
    }


def test_frequency_counts_matters_not_versions_and_generic_words_do_not_rescue():
    corpus = [
        dict(memory_id="car", lexemes=["user", "drive", "plan", "recent"]),
        dict(memory_id="car", lexemes=["user", "drive", "plan", "recent"]),
        dict(memory_id="shop", lexemes=["user", "plan"]),
        dict(memory_id="home", lexemes=["user", "plan"]),
        dict(memory_id="book", lexemes=["user", "read"]),
    ]
    decisions, stats = select_candidates([
        score("car", 0.4, ["drive", "plan"]),
        score("vague", 0.4, ["user", "plan", "recent"]),
    ], corpus, ["user", "drive", "plan", "recent"])
    assert stats["matters"] == 4
    assert decisions[0]["informative_keywords"] == ["drive"]
    assert decisions[0]["selected"] and not decisions[1]["selected"]


@pytest.mark.parametrize("subject", ["user", "team", "group"])
def test_generic_subject_cannot_rescue_candidates_even_when_rare(subject):
    corpus = [
        dict(memory_id="channel", lexemes=[subject, "channel"]),
        dict(memory_id="deadline", lexemes=[subject, "deadlin"]),
        dict(memory_id="recipe", lexemes=["recipe"]),
        dict(memory_id="travel", lexemes=["travel"]),
        dict(memory_id="book", lexemes=["book"]),
    ]
    decisions, _ = select_candidates([
        score("channel", 0.4, [subject, "channel"]),
        score("deadline", 0.4, [subject, "deadlin"]),
        score("strong", 0.5, [subject]),
        score("named", 0.4, [subject], entities=["cedar"]),
        score("identifier", 0.1, [subject], identifiers=["task-17"]),
    ], corpus, [subject, "deadlin"])
    assert {d["ref"] for d in decisions if d["selected"]} == {
        "deadline", "strong", "named", "identifier",
    }
    assert decisions[0]["informative_keywords"] == []
    assert decisions[1]["informative_keywords"] == ["deadlin"]


def vector(similarity):
    return [similarity, math.sqrt(1 - similarity * similarity), 0]


async def saved_audit(env):
    async with env.store.pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage='p2_new.candidates' ORDER BY step_id DESC LIMIT 1",
            *env.trace.args,
        )


@pytest.mark.asyncio
async def test_reuse_query_vector_zero_candidates_and_shared_pool_survives(env, jev):  # noqa: F811 - imported pytest fixture
    c = await session(env, 1)
    text = "The user has been thinking about getting new kitchen utensils."
    env.vectors["document: " + text] = vector(0.4356)
    old = await add(env, c, text)
    c2 = await session(env, 2)
    incoming = "The user's hobby is pottery."
    batch = await env.retriever.prepare([ExtractedItem(incoming, "content", [], "current", topics=[])], c2)
    assert old in batch.stored[0]
    before = len(env.embedded)
    jev[0].append("new")

    accepted, changes, protected = await env.full_pipeline.process_direct(batch, c2)
    assert len(accepted) == 1 and len(changes) == 1 and not protected
    assert (await row(env, accepted[0]))["text"] == incoming
    assert env.embedded[before:] == ["document: " + incoming]
    assert not env.prompts  # No Luna call; the accepted fact still needs indexing.
    assert old in await batch.resolve(0, env.store, c2)
    audit = await saved_audit(env)
    assert audit["selected_refs"] == []
    assert audit["candidates"][0]["reason"] == "insufficient_support"


@pytest.mark.asyncio
async def test_real_stemming_preserves_sedan_to_scooter_and_scopes_statistics(env):
    c = await session(env, 1)
    sedan = "The user drives a Zyvanta Sedan."
    kitchen = "The user has been thinking about buying kitchen utensils."
    env.vectors["document: " + sedan] = vector(0.4349)
    env.vectors["document: " + kitchen] = vector(0.4356)
    car = await add(env, c, sedan)
    shop = await add(env, c, kitchen)
    c2 = await session(env, 2)
    await add(env, c2, "The user drives tomorrow's vehicle.")
    focus = pending(c, "The user drives a Xylorim Scooter now; they recently switched to it again.")
    candidates = {key: await row(env, key) for key in (car, shop)}
    selected = await env.retriever.filter_direct_candidates(
        focus, candidates, c, current_refs={car, shop}, query_vector="[1,0,0]",
    )
    assert set(selected) == {car}
    audit = await saved_audit(env)
    assert audit["corpus"]["matters"] == 2  # no future-session vocabulary leakage
    car_audit = next(d for d in audit["candidates"] if d["ref"] == car)
    assert "drive" in car_audit["informative_keywords"]


@pytest.mark.asyncio
async def test_literal_names_and_identifiers_require_query_and_document_boundaries(env):
    c = await session(env, 1)
    texts = [
        "Ticket AB-123 is open.", "Ticket AB-1234 is open.",
        "James works here.", "Jameson works here.",
        "Ticket date is 2026/10/20.",
        "Ticket link is https://example.org/task.",
        "Ticket link is https://example.org/task/another.",
    ]
    keys = []
    for text in texts:
        env.vectors["document: " + text] = vector(0.1 if text.startswith("Ticket") else 0.4)
        keys.append(await add(env, c, text))
    candidates = {key: await row(env, key) for key in keys}
    focus = dict(
        pending(c, "Ticket AB-123 belongs to James, dated 2026/10/20, at https://example.org/task."),
        entities=["AB-123", "James", "Jameson", "2026/10/20", "https://example.org/task"],
    )
    selected = await env.retriever.filter_direct_candidates(
        focus, candidates, c, current_refs=set(keys), query_vector="[1,0,0]",
    )
    assert set(selected) == {keys[0], keys[2], keys[5]}


@pytest.mark.asyncio
async def test_selected_historical_candidate_keeps_its_current_version(env):
    c = await session(env, 1)
    old_text, new_text = "Old pottery activity.", "Different current activity."
    env.vectors["document: " + old_text] = vector(0.8)
    old = await add(env, c, old_text)
    c2 = await session(env, 2)
    updated, _ = await env.executor.apply_direct(
        pending(c2, new_text), DirectDecision(action="update", target_ref=old), c2,
    )
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET memory_embedding=$6::vector WHERE {SCOPE} AND memory_id=$4 AND version=$5",
            *env.trace.args, *split_ref(updated), str(vector(0.1)),
        )
    candidates = {key: await row(env, key) for key in (old, updated)}
    selected = await env.retriever.filter_direct_candidates(
        pending(c2, "Pottery."), candidates, c2, current_refs={updated}, query_vector="[1,0,0]",
    )
    assert set(selected) == {old, updated}
    audit = await saved_audit(env)
    assert next(d["reason"] for d in audit["candidates"] if d["ref"] == updated) == "matched_matter_current"


@pytest.mark.asyncio
async def test_same_session_accepted_results_are_filtered_too(env, jev):  # noqa: F811 - imported pytest fixture
    c = await session(env, 1)
    batch = await env.retriever.prepare([
        ExtractedItem("The user drives a Sedan.", "content", [], "current", topics=[]),
        ExtractedItem("The user's hobby is pottery.", "content", [], "current", topics=[]),
    ], c)
    old_text = "The user drives a Sedan."
    env.vectors["document: " + old_text] = vector(0.2)
    old = await add(env, c, old_text)
    batch.accepted[0] = [old]
    candidates = await batch.resolve(1, env.store, c)
    assert old in candidates
    jev[0].append("new")
    accepted, _ = await process_item(env,
        pending(c, batch.items[1].text), candidates, c, query_vector=batch.query_vectors[1],
    )
    assert len(accepted) == 1 and not env.prompts
    assert old in await batch.resolve(1, env.store, c)
