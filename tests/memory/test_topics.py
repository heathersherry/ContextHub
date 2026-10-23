"""Real PG topic retrieval, with controlled embedding/model responses."""

import json
from dataclasses import replace

import pytest

from contexthub.memory.contracts import DirectDecision, ref_of
from contexthub.memory.extractor import parse_items
from contexthub.memory.storage import SCOPE
from .helpers import add, input_of, item, pending, row, session
from .test_jev_direct import jev  # noqa: F401


def topic(env, name, vector):
    # Isolate vector fixtures in the database-wide label cache.
    label = name + "/" + env.trace.episode_id
    env.vectors["document: " + label] = vector
    return label


@pytest.mark.parametrize("topics", [None, "residence", [1], [""], ["  "]])
def test_topics_contract_rejects_invalid_labels(topics):
    with pytest.raises(ValueError, match="topics"):
        parse_items(json.dumps({"items": [item("Lives in A", topics=[]) | {"topics": topics}]}))


def test_topics_are_required_and_normalized_for_facts_and_rules():
    with pytest.raises(ValueError, match="topics"):
        parse_items('{"items":[{"text":"Lives in A","entities":[],"temporal_scope":"current"}]}')
    parsed, _ = parse_items(json.dumps({"items": [
        item("Lives in A", topics=[" residence ", "residence", " HOME "])
    ]}))
    assert parsed[0].topics == ["home", "residence"]
    rules, _ = parse_items(json.dumps({"items": [
        dict(text="If I move, then my gym changes.", entities=[], topics=["residence", "workout venue"])
    ]}), kind="rule")
    assert set(rules[0].topics) == {"residence", "workout venue"}


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 10])
async def test_topics_batch_cache_does_not_reembed_labels(env, count):
    c = await session(env, 1)
    labels = [topic(env, f"topic{i}", [1, 0, 0]) for i in range(count)]
    await env.indexer.index_topics(labels + labels, c)
    assert sorted(env.embedded) == sorted("document: " + t for t in labels)
    await env.indexer.index_topics(labels[::-1], c)
    assert len(env.embedded) == count
    async with env.store.pool.acquire() as conn:
        attempts = await conn.fetch(
            f"SELECT payload FROM execution_attempts WHERE {SCOPE} AND phase='finished'",
            *env.trace.args,
        )
    assert len(attempts) == (count + 7) // 8
    assert attempts[0]["payload"]["identity"]["stage"] == "topic_embedding"
    from integrations.memebench.memory_research.cli import totals
    assert set(totals([r["payload"] for r in attempts], {})) == {"embedding"}


@pytest.mark.asyncio
async def test_topic_search_bypasses_top_k_but_not_visibility_or_type(env):
    c = await session(env, 1)
    home = topic(env, "residence", [1, 0, 0])
    address = topic(env, "home address", [0.95, 0.1, 0])
    text = "The user lives in Cymbeline Shores."
    env.vectors["document: " + text] = [0, 1, 0]
    old = await add(env, c, text, topics=[home])
    deleted = await add(env, c, "Deleted home", topics=[home])
    await env.store.forget(env.trace, deleted.split(":")[0], "fixture")
    rejected = await add(env, c, "Unaccepted home", topics=[home], accepted=False)
    stale = await add(env, c, "Stale home", topics=[home], status="stale")
    rule = await add(env, c, "If I move, then commute changes.", type="rule", topics=[home])
    for i in range(4):
        await add(env, c, f"Move to Newtown distractor {i}")
    future_c = await session(env, 2)
    future = await add(env, future_c, "Future home", topics=[home])
    other = env.trace.model_copy(update={"episode_id": env.trace.episode_id + "-other"})
    await env.store.save_session(other, 1, None, [])
    foreign = await env.store.add_memory(
        other, 1, text="Other person's home", type="fact", topics=[home],
        entities=[], origin="extracted", sources=[1],
    )
    await env.indexer.index([foreign], replace(c, trace=other))
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET accepted=true,status='fresh',temporal_scope='current' WHERE {SCOPE}",
            *other.args,
        )
    hits = await env.retriever.search(
        "Move to Newtown", c, through_session=1, purpose="p2_match",
        accepted_only=True, memory_type="fact", limit=1, topics=[address],
    )
    keys = {ref_of(hit.memory) for hit in hits}
    assert old in keys and stale in keys  # maintenance may compare stale facts
    assert not keys.intersection({deleted, rejected, future, rule, f"{foreign[0]}:{foreign[1]}"})
    assert next(hit for hit in hits if ref_of(hit.memory) == old).channels == ("topic",)
    answers = await env.retriever.search(
        "Move to Newtown", c, through_session=1, purpose="answer",
        memory_type="fact", limit=1, topics=[address],
    )
    assert old in {ref_of(hit.memory) for hit in answers}
    assert stale not in {ref_of(hit.memory) for hit in answers}
    excluded = await env.retriever.search(
        "Move to Newtown", c, through_session=1, purpose="forget",
        topics=[address], exclude_refs=(old,),
    )
    assert old not in {ref_of(hit.memory) for hit in excluded}


@pytest.mark.asyncio
async def test_any_topic_pair_rescues_new_candidate_and_model_still_decides(env, jev):  # noqa: F811
    c = await session(env, 1)
    home = topic(env, "residence", [1, 0, 0])
    address = topic(env, "home address", [0.95, 0.1, 0])
    travel = topic(env, "travel destination", [0, 1, 0])
    old_text = "The user lives in Cymbeline Shores."
    noise_text = "The user plans to visit Radical Gardens."
    for text in [old_text, noise_text]:
        env.vectors["document: " + text] = [0.1, 0.995, 0]
    old = await add(env, c, old_text, topics=[home])
    noise = await add(env, c, noise_text, topics=[travel])
    c2 = await session(env, 2)
    focus = pending(c2, "The user moved to Orinthal Park.", topics=[address])
    candidates = {key: await row(env, key) for key in (old, noise)}
    jev[0].append("new")

    def update(payload):
        data = input_of(payload)
        assert [r["text"] for r in data["candidates"]] == [old_text]
        return {"operations": [dict(
            action="update", target_ref=data["candidates"][0]["ref"],
            text=focus["text"], entities=["Orinthal Park"],
        )]}

    env.responses.append(update)
    keys, changes = await env.executor.process(focus, candidates, c2, query_vector="[1,0,0]")
    assert len(env.prompts) == 1 and changes[0]["operation"] == "update"
    assert (await row(env, old))["status"] == "stale"
    assert (await row(env, keys[0]))["topics"] == sorted([home, address])
    assert (await row(env, noise))["status"] == "fresh"
    async with env.store.pool.acquire() as conn:
        audit = await conn.fetchval(
            f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage='p2_new.candidates'",
            *env.trace.args,
        )
    assert audit["selected_refs"] == [old]
    evidence = next(d for d in audit["candidates"] if d["ref"] == old)
    assert evidence["reason"] == "topic_similarity"
    assert evidence["topic_match"]["query_topic"] == address
    assert evidence["topic_match"]["candidate_topic"] == home


@pytest.mark.asyncio
async def test_forget_and_correction_reuse_topic_channel_without_auto_removal(env):
    c = await session(env, 1)
    home = topic(env, "residence", [1, 0, 0])
    address = topic(env, "home address", [0.95, 0.1, 0])
    text = "The user lives in Cymbeline Shores."
    env.vectors["document: " + text] = [0, 1, 0]
    old = await add(env, c, text, topics=[home])
    focus = pending(c, "Forget my home address.", topics=[address])
    supplement = await env.retriever.forget_candidates(focus, c, query_vector="[1,0,0]")
    assert old in supplement
    assert (await row(env, old))["status"] == "fresh"  # retrieval is not deletion
    corrected = pending(c, "Correction: my residence is elsewhere.", topics=[address])
    candidates = await env.retriever.candidates(corrected, c, query_vector="[1,0,0]")
    assert old in candidates


@pytest.mark.asyncio
async def test_p1_topic_override_keeps_candidate_without_assigning_roles(env):
    c = await session(env, 1)
    home = topic(env, "residence", [1, 0, 0])
    food = topic(env, "food preference", [0, 1, 0])
    text = "The user lives in Cymbeline Shores."
    env.vectors["document: " + text] = [0, 1, 0]  # both role queries score zero
    fact = await add(env, c, text, topics=[home])
    rule = await add(
        env, c, "If the user moves, then the restaurant changes.",
        type="rule", topics=[food, home],
    )
    kept = await env.retriever.filter_rule_candidates(
        await row(env, rule), {fact: await row(env, fact)}, c,
    )
    assert set(kept) == {fact}
    stored = await row(env, rule)
    assert not stored["condition_matter_ids"] and not stored["affected_matter_ids"]
    assert not env.prompts


@pytest.mark.asyncio
async def test_threshold_is_strict_configurable_and_old_evidence_still_works(env):
    evidence = dict(topic_similarity=0.8)
    assert not env.retriever.topic_selected(evidence)
    assert env.retriever.topic_selected(dict(topic_similarity=0.80001))
    env.retriever.config = env.retriever.config.model_copy(update={"topic_similarity_threshold": 0.9})
    assert not env.retriever.topic_selected(dict(topic_similarity=0.85))
    c = await session(env, 1)
    home = topic(env, "residence", [1, 0, 0])
    food = topic(env, "food preference", [0, 1, 0])
    old = await add(env, c, "A strongly similar fact", topics=[food])
    focus = pending(c, "A different value", topics=[home])
    kept = await env.retriever.filter_direct_candidates(
        focus, {old: await row(env, old)}, c, current_refs={old}, query_vector="[1,0,0]",
    )
    assert old in kept  # topic mismatch must not veto existing strong-vector evidence


@pytest.mark.asyncio
async def test_topic_match_keeps_current_version_of_selected_historical_matter(env):
    c = await session(env, 1)
    label = topic(env, "residence", [1, 0, 0])
    old = await add(env, c, "Old residence", topics=[label])
    c2 = await session(env, 2)
    new, _ = await env.executor.apply_direct(
        pending(c2, "New residence", topics=[label]),
        DirectDecision(action="update", target_ref=old), c2,
    )
    assert (await row(env, new))["topics"] == [label]
    assert (await row(env, old))["topics"] == [label]
    focus = pending(c2, "Remember where I lived", topics=[label])
    kept = await env.retriever.filter_direct_candidates(
        focus, {old: await row(env, old), new: await row(env, new)}, c2,
        current_refs={new}, query_vector="[0,1,0]",
    )
    assert set(kept) == {old, new}
