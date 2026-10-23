"""Rule-owned sets route local propagation; no positional pairing or global repair."""

import pytest

from contexthub.memory.contracts import DirectDecision, split_ref
from contexthub.memory.model_io import OutputValidationFailed
from contexthub.memory.storage import SCOPE
from .helpers import (
    add,
    bindings_response,
    input_of,
    link,
    pending,
    repair_response,
    row,
    session,
    trigger_response,
)


async def update(env, context, key, text):
    return await env.executor.apply_direct(
        pending(context, text),
        DirectDecision(action="update", target_ref=key),
        context,
    )


@pytest.mark.asyncio
async def test_two_source_three_target_sets_and_fact_update_does_not_rebind(env):
    c = await session(env, 1)
    facts = {
        text: await add(env, c, text)
        for text in (
            "Weather dry",
            "Canopy absent",
            "Venue garden",
            "Start 10am",
            "Equipment outside",
            "Hobby painting",
        )
    }
    rule = await add(
        env,
        c,
        "If it rains AND there is no canopy, move indoors, start at 3pm and move equipment inside.",
        type="rule",
    )
    inputs = {key: await row(env, key) for key in facts.values()}
    env.responses.append(
        bindings_response(
            ["Weather dry", "Canopy absent"],
            ["Venue garden", "Start 10am", "Equipment outside"],
        )
    )
    assert await env.discovery.bind({rule: inputs}, c) == [rule]
    saved = await row(env, rule)
    assert set(saved["condition_matter_ids"]) == {
        split_ref(facts[t])[0] for t in ("Weather dry", "Canopy absent")
    }
    assert set(saved["affected_matter_ids"]) == {
        split_ref(facts[t])[0]
        for t in ("Venue garden", "Start 10am", "Equipment outside")
    }
    c2 = await session(env, 2)
    new, _ = await update(env, c2, facts["Weather dry"], "Weather rainy")
    assert await env.discovery.bind({new: {rule: saved}}, c2) == []
    assert await row(env, rule) == saved
    assert len(env.prompts) == 1
    assert (await row(env, new))["condition_matter_ids"] == []


@pytest.mark.asyncio
async def test_missing_endpoint_backfill_preserves_known_sources_and_targets(env):
    c = await session(env, 1)
    source = await add(env, c, "Weather rainy")
    rule = await add(
        env,
        c,
        "If it rains and there is no canopy, move the event indoors.",
        type="rule",
    )
    env.responses.append(bindings_response(["Weather rainy"], []))
    await env.discovery.bind({rule: {source: await row(env, source)}}, c)
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                f"SELECT count(*) FROM memory_steps WHERE {SCOPE} AND stage='p1.unbound'",
                *env.trace.args,
            )
            == 1
        )
    c2 = await session(env, 2)
    canopy = await add(env, c2, "Canopy absent")
    target = await add(env, c2, "Venue garden")
    # A newly found source must not wipe a previously established endpoint.
    env.responses.append(bindings_response(["Canopy absent"], ["Venue garden"]))
    assert await env.discovery.bind(
        {key: {rule: await row(env, rule)} for key in (canopy, target)}, c2
    ) == [rule]
    saved = await row(env, rule)
    assert set(saved["condition_matter_ids"]) == {
        split_ref(source)[0],
        split_ref(canopy)[0],
    }
    assert saved["affected_matter_ids"] == [split_ref(target)[0]]
    assert len(await env.store.current_rows(env.trace)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown_source", "duplicate_target", "old_shape"])
async def test_invalid_binding_output_does_not_partially_write(env, fault):
    c = await session(env, 1)
    fact = await add(env, c, "Owner Wang")
    rule = await add(env, c, "If owner changes, recipient follows owner.", type="rule")

    def bad(payload):
        ref = input_of(payload)["candidates"][0]["ref"]
        if fault == "old_shape":
            return {"target_refs": [ref]}
        return {
            "condition_refs": ["M999"] if fault == "unknown_source" else [ref],
            "affected_refs": [ref, ref] if fault == "duplicate_target" else [],
        }

    env.responses.extend([bad, bad])
    with pytest.raises(OutputValidationFailed):
        await env.discovery.bind({rule: {fact: await row(env, fact)}}, c)
    saved = await row(env, rule)
    assert saved["condition_matter_ids"] == saved["affected_matter_ids"] == []


@pytest.mark.asyncio
async def test_rule_new_version_rejudges_both_ends_without_inheriting(env):
    c = await session(env, 1)
    source = await add(env, c, "Weather dry")
    target = await add(env, c, "Venue garden")
    rule = await add(env, c, "If weather changes, venue changes.", type="rule")
    await link(env, rule, sources=[source], targets=[target])
    c2 = await session(env, 2)
    new, changes = await env.executor.apply_direct(
        pending(c2, "If the venue changes, check the weather.", type="rule"),
        DirectDecision(action="update", target_ref=rule),
        c2,
    )
    assert (await row(env, new))["condition_matter_ids"] == []
    env.responses.append(bindings_response(["Venue garden"], ["Weather dry"]))
    await env.discovery.bind({new: {}}, c2)
    current = await row(env, new)
    assert current["condition_matter_ids"] == [split_ref(target)[0]]
    assert current["affected_matter_ids"] == [split_ref(source)[0]]
    assert set(await env.executor.routed_rules(changes, c2)) == {new}
    assert not await env.store.active_rules(
        env.trace, condition_ids=[split_ref(source)[0]]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_compound_condition_reads_all_sources_and_all_targets_but_not_other_rules(
    env, unknown
):
    c = await session(env, 1)
    weather = await add(env, c, "Weather dry")
    canopy = await add(env, c, "Canopy present", status="stale" if unknown else "fresh")
    targets = [await add(env, c, t) for t in ("Venue garden", "Start 10am")]
    rule = await add(
        env, c, "If rain AND no canopy, move indoors and start at 3pm.", type="rule"
    )
    await link(env, rule, sources=[weather, canopy], targets=targets)
    other = await add(env, c, "Host late")
    unrelated = await add(env, c, "If host is late, cancel catering.", type="rule")
    await link(env, unrelated, sources=[other], targets=targets)
    c2 = await session(env, 2)
    _, changes = await update(env, c2, weather, "Weather rainy")

    def inspect(payload):
        data = input_of(payload)
        assert len(data["checks"]) == 1
        check = data["checks"][0]
        assert {r["text"] for r in check["sources"]} == {
            "Weather rainy",
            None if unknown else "Canopy present",
        }
        assert {r["text"] for r in check["facts"]} == {"Venue garden", "Start 10am"}
        assert "Host late" not in str(data) and "cancel catering" not in str(data)
        # One true input does not prove the conjunction; unknown isn't "no canopy".
        return {"triggers": []}

    env.responses.append(inspect)
    assert await env.executor.check_conditions(changes, set(), c2) == []
    for key in targets:
        assert (await row(env, key))["status"] == "fresh"


@pytest.mark.asyncio
async def test_cross_rule_unrelated_change_cannot_be_used_as_trigger_evidence(env):
    c = await session(env, 1)
    a, b = await add(env, c, "Owner Wang"), await add(env, c, "Weather dry")
    x, y = await add(env, c, "Recipient Wang"), await add(env, c, "Venue garden")
    r1 = await add(env, c, "If owner changes, recipient follows owner.", type="rule")
    r2 = await add(env, c, "If weather changes, venue changes.", type="rule")
    await link(env, r1, sources=[a], targets=[x])
    await link(env, r2, sources=[b], targets=[y])
    c2 = await session(env, 2)
    _, ca = await update(env, c2, a, "Owner Li")
    _, cb = await update(env, c2, b, "Weather rainy")
    env.responses.extend([trigger_response(["Recipient Wang"], ["C2"])] * 2)
    with pytest.raises(OutputValidationFailed):
        await env.executor.check_conditions(ca + cb, set(), c2)
    assert (await row(env, x))["status"] == (await row(env, y))["status"] == "fresh"


@pytest.mark.asyncio
async def test_new_binding_wakes_existing_state_and_stale_target_without_global_scans(
    env, monkeypatch
):
    c = await session(env, 1)
    source = await add(env, c, "Health recovered")
    target = await add(env, c, "Exercise swimming", status="stale")
    unrelated = await add(env, c, "Unrelated stale preference", status="stale")
    rule = await add(
        env, c, "If health is recovered, exercise is cycling.", type="rule"
    )
    await link(env, rule, sources=[source], targets=[target])
    read = env.store.current_rows

    async def scoped(trace, matter_ids=None):
        assert matter_ids is not None, "P2 must never scan all current facts"
        assert split_ref(unrelated)[0] not in matter_ids
        return await read(trace, matter_ids)

    monkeypatch.setattr(env.store, "current_rows", scoped)
    await env.full_pipeline.maintain_conditions([], set(), c)
    assert not env.prompts
    env.responses.append(
        repair_response(
            "Exercise cycling",
            ["Health recovered", "If health is recovered, exercise is cycling."],
        )
    )
    await env.full_pipeline.maintain_conditions([], set(), c, [rule])
    assert not env.responses
    assert (await row(env, unrelated))["status"] == "stale"
    current = await read(env.trace, [split_ref(target)[0]])
    assert current[0]["text"] == "Exercise cycling"
    assert set(current[0]["basis_refs"]) == {source, rule}


@pytest.mark.asyncio
async def test_binding_wakes_fresh_target_with_source_state_not_a_fake_source_change(
    env,
):
    c = await session(env, 1)
    source = await add(env, c, "Weather rainy")
    target = await add(env, c, "Venue garden")
    rule = await add(env, c, "If rainy, venue is indoors.", type="rule")
    await link(env, rule, sources=[source], targets=[target])

    def decide(payload):
        data = input_of(payload)
        assert [t["operation"] for t in data["changes"]] == ["binding"]
        assert data["checks"][0]["sources"][0]["text"] == "Weather rainy"
        return trigger_response(["Venue garden"], ["C1"])(payload)

    env.responses.append(decide)
    assert await env.executor.check_conditions([], set(), c, binding_refs=[rule])
    assert (await row(env, target))["status"] == "stale"


@pytest.mark.asyncio
async def test_incorporation_hint_never_cites_filtered_unrelated_change(env):
    c = await session(env, 1)
    owner = await add(env, c, 'Owner Wang')
    other = await add(env, c, 'Hobby reading')
    rule = await add(env, c, 'If owner changes, recipient follows owner.', type='rule')
    c2 = await session(env, 2)
    _, relevant = await update(env, c2, owner, 'Owner Li')
    unrelated_ref, unrelated = await update(env, c2, other, 'Hobby painting')
    target = await add(env, c2, 'Recipient Wang', origin='derived', basis=[unrelated_ref, rule])
    await link(env, rule, sources=[owner], targets=[target])
    def inspect(payload):
        data = input_of(payload)
        assert [t['id'] for t in data['changes']] == ['C1']
        assert 'incorporates_changes' not in data['checks'][0]['facts'][0]
        return {'triggers': []}
    env.responses.append(inspect)
    await env.executor.check_conditions(relevant + unrelated, set(), c2)
