"""Ended states are facts in the existing matter, not removal operations."""

import pytest
from pydantic import ValidationError

from contexthub.memory.contracts import DirectDecision, DirectPlan, split_ref
from .helpers import add, pending, row, session
from .test_jev_direct import jev, plan_response, process_item  # noqa: F401


def test_direct_invalidate_is_not_a_supported_operation():
    with pytest.raises(ValidationError):
        DirectDecision(action="invalidate", target_ref="M1")
    with pytest.raises(ValidationError):
        DirectPlan(operations=[dict(action="update", target_ref="M1", text=None, entities=[])])


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["inform", "correction"])
async def test_ending_saves_current_negative_state_preserves_history_and_stales_dependents(env, jev, intent):
    c1 = await session(env, 1)
    old = await add(env, c1, "Sarah leads the team.")
    derived = await add(env, c1, "The team report goes to Sarah.", origin="derived", basis=[old])
    unrelated = await add(env, c1, "Sarah mentors the design group.")
    c2 = await session(env, 2)
    ended = "Sarah no longer leads the team."
    jev[0].append(intent)
    env.responses.append(plan_response(("update", "Sarah leads the team.", ended)))
    keys, changes = await process_item(
        env, pending(c2, ended), {k: await row(env, k) for k in [old, unrelated]}, c2,
    )
    assert len(keys) == 1
    saved = await row(env, keys[0])
    assert saved["memory_id"] == split_ref(old)[0] and saved["version"] == 2
    assert saved["text"] == ended and saved["status"] == "fresh"
    assert saved["temporal_scope"] == "current" and not saved["invalidated"]
    previous = await row(env, old)
    assert previous["status"] == "stale" and not previous["invalidated"]
    dependent = await row(env, derived)
    assert dependent["status"] == "stale" and not dependent["invalidated"]
    assert (await row(env, unrelated))["status"] == "fresh"
    change = next(t for t in changes if t["operation"] == "update")
    assert change["before"]["text"] == previous["text"]
    assert change["after"]["text"] == ended
    current = await env.store.current_rows(env.trace)
    assert old not in {f"{r['memory_id']}:{r['version']}" for r in current}


@pytest.mark.asyncio
async def test_coexisting_hobby_stays_current_when_new_fact_is_added(env, jev):
    c1 = await session(env, 1)
    old = await add(env, c1, "The user enjoys woodworking.")
    c2 = await session(env, 2)
    jev[0].append("inform")
    env.responses.append(plan_response(("new", None, "The user enjoys golf.")))
    keys, changes = await process_item(env, pending(c2, "The user enjoys golf."), {old: await row(env, old)}, c2)
    assert (await row(env, old))["status"] == "fresh"
    assert (await row(env, keys[0]))["memory_id"] != split_ref(old)[0]
    assert [t["operation"] for t in changes] == ["new"]


@pytest.mark.asyncio
async def test_old_invalidate_output_is_rejected_before_any_memory_write(env, jev):
    c1 = await session(env, 1)
    old = await add(env, c1, "Sarah leads the team.")
    c2 = await session(env, 2)
    jev[0].append("inform")
    env.responses.extend([plan_response(("invalidate", "Sarah leads the team.", None))] * 2)
    assert await process_item(env, pending(c2, "Sarah no longer leads the team."), {old: await row(env, old)}, c2) == ([], [])
    assert (await row(env, old))["status"] == "fresh"
    assert len(await env.store.versions(env.trace, split_ref(old)[0])) == 1
