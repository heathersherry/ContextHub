from .helpers import link
"""Slim model inputs retain the distinctions needed to execute maintenance."""

from .helpers import repair_target

import pytest

from contexthub.memory.contracts import DirectDecision, split_ref
from .helpers import (
    add,
    row,
    session,
    pending,
    input_of,
    direct_response,
    direct,
    condition_facts,
    repair_response,
)


@pytest.mark.asyncio
async def test_compact_direct_can_correct_old_version_without_replacing_current(env):
    c1 = await session(env, 1)
    old = await add(env, c1, "Owner Wang")
    c2 = await session(env, 2)
    current, _ = await env.executor.apply_direct(
        pending(c2, "Owner Li"), DirectDecision(action="update", target_ref=old), c2
    )

    def decide(payload):
        data = input_of(payload)
        assert set(data) == {"incoming", "candidates"}
        assert "entities" not in data["incoming"]
        records = {r["text"]: r for r in data["candidates"]}
        former, now = records["Owner Wang"], records["Owner Li"]
        assert not former["is_current"] and now["is_current"]
        assert former["matter"] == now["matter"]
        assert former["status"] == "stale" and now["status"] == "fresh"
        assert former["created_in_session"] == 1 and now["created_in_session"] == 2
        assert all(
            not {"accepted", "basis_refs", "entities", "version", "temporal_scope"}
            & set(r)
            for r in records.values()
        )
        return direct_response("correct", "Owner Wang")(payload)

    env.responses.append(decide)
    corrected, _ = await direct(
        env, pending(c2, "Correction: the former owner was Zhao"), c2
    )
    assert (await row(env, old))["invalidated"]
    assert (await row(env, corrected))["temporal_scope"] == "historical"
    latest = await env.store.current_rows(env.trace)
    assert (
        latest[0]["text"] == "Owner Li"
        and (await row(env, current))["status"] == "fresh"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,origin", [("stale", "extracted"), ("fresh", "derived")]
)
async def test_compact_direct_preserves_reconfirmation_as_update(env, status, origin):
    c1 = await session(env, 1)
    old = await add(env, c1, "Exercise swimming", status=status, origin=origin)
    c2 = await session(env, 2)

    def decide(payload):
        candidate = input_of(payload)["candidates"][0]
        assert candidate["is_current"]
        assert candidate["status"] == status and candidate["origin"] == origin
        return direct_response("update", "Exercise swimming")(payload)

    env.responses.append(decide)
    new, _ = await direct(env, pending(c2, "Exercise swimming"), c2)
    saved = await row(env, new)
    assert saved["version"] == 2 and saved["origin"] == "extracted"
    assert saved["status"] == "fresh" and new != old


@pytest.mark.asyncio
async def test_condition_groups_share_rules_and_identify_incorporated_new_values(env):
    c1 = await session(env, 1)
    owner = await add(env, c1, "Owner Wang")
    r1 = await add(
        env, c1, "If owner changes recipient and contact follow owner", type="rule"
    )
    r2 = await add(
        env, c1, "If reporting policy changes recipient changes", type="rule"
    )
    contact = await add(env, c1, "Contact Wang")
    c2 = await session(env, 2)
    new, changes = await env.executor.apply_direct(
        pending(c2, "Owner Li"), DirectDecision(action="update", target_ref=owner), c2
    )
    recipient = await add(
        env, c2, "Recipient Li", origin="derived", basis=[new, r1]
    )

    await link(env, r1, sources=[owner], targets=[contact, recipient])
    await link(env, r2, targets=[recipient])
    def decide(payload):
        data = input_of(payload)
        assert set(data) == {"changes", "checks"}
        assert data["changes"][0]["id"] == "C1"
        assert data["changes"][0]["operation"] == "update"
        assert len(data["checks"]) == 1
        groups = {c["rule"]["text"]: c["facts"] for c in data["checks"]}
        assert len(groups["If owner changes recipient and contact follow owner"]) == 2
        assert "If reporting policy changes recipient changes" not in groups
        recipients = [
            r for c in data["checks"] for r in c["facts"] if r["text"] == "Recipient Li"
        ]
        assert len({r["ref"] for r in recipients}) == 1
        assert all(r["incorporates_changes"] == ["C1"] for r in recipients)
        assert all(
            "basis_refs" not in r and "accepted" not in r for r in condition_facts(data)
        )
        target = next(r for r in condition_facts(data) if r["text"] == "Contact Wang")
        assert "incorporates_changes" not in target
        return dict(triggers=[dict(fact_ref=target["ref"], change_ids=["C1"])])

    env.responses.append(decide)
    await env.executor.check_conditions(changes, {new}, c2)
    assert (await row(env, contact))["status"] == "stale"
    assert (await row(env, recipient))["status"] == "fresh"


@pytest.mark.asyncio
async def test_compact_repair_omits_recording_times_and_saves_exact_selected_basis(env):
    c = await session(env, 1)
    rule_text = "If owner changes recipient follows owner"
    rule = await add(env, c, rule_text, type="rule")
    owner = await add(env, c, "Owner Li")
    target = await add(env, c, "Recipient Wang", status="stale", basis=[owner, rule], origin="derived")
    await link(env, rule, sources=[owner], targets=[target])
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET timestamp=$1 WHERE run_id=$2 AND memory_id=$3",
            "2026-10-12",
            env.trace.run_id,
            split_ref(owner)[0],
        )

    def decide(payload):
        data = input_of(payload)
        assert set(data) == {"materials", "changes", "retrieval_available"}
        assert "target" not in data
        assert repair_target(payload) == "Recipient Wang"
        assert payload["messages"][0]["content"].count('"Recipient Wang"') == 1
        assert all(
            not {
                "status",
                "accepted",
                "origin",
                "basis_refs",
                "entities",
                "version",
                "invalidated",
            }
            & set(r)
            for r in data["materials"]
        )
        premise = next(r for r in data["materials"] if r["text"] == "Owner Li")
        assert set(premise) == {"ref", "text", "type"}
        return repair_response("Recipient Li", ["Owner Li", rule_text])(payload)

    env.responses.append(decide)
    result = await env.executor.repair(await row(env, target), [], c, set())
    assert set(result["after"]["basis_refs"]) == {owner, rule}
    assert result["after"]["text"] == "Recipient Li"
    assert (await row(env, owner))["timestamp"] == "2026-10-12"
