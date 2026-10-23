from .helpers import link
"""Condition decisions cite changes from the same call, before any mutation."""

import pytest

from contexthub.memory.contracts import DirectDecision
from contexthub.memory.model_io import OutputValidationFailed
from contexthub.memory.storage import SCOPE
from .helpers import add, condition_facts, input_of, pending, row, session


async def setup_changes(env):
    c1 = await session(env, 1)
    rule = await add(
        env, c1, "If owner or policy changes, recipients may change.", type="rule"
    )
    owner = await add(env, c1, "Owner Wang")
    targets = [
        await add(env, c1, text)
        for text in ("Primary recipient Wang", "Backup recipient Wang")
    ]
    c2 = await session(env, 2)
    _, policy_changes = await env.executor.apply_direct(
        pending(c2, "Delivery policy changed to express"),
        DirectDecision(action="new", target_ref=None),
        c2,
    )
    _, owner_changes = await env.executor.apply_direct(
        pending(c2, "Owner Li"),
        DirectDecision(action="update", target_ref=owner),
        c2,
    )
    await link(env, rule, sources=[owner, f"{policy_changes[0]['after']['memory_id']}:1"], targets=targets)
    return c2, targets, policy_changes + owner_changes


@pytest.mark.asyncio
async def test_selected_change_ids_survive_correction_and_are_logged(env):
    c, targets, changes = await setup_changes(env)
    seen = []

    def response(payload):
        data = input_of(payload)
        seen.append(data)
        return {
            "triggers": [
                dict(
                    fact_ref=r["ref"],
                    change_ids=["C99"] if len(seen) == 1 else ["C1", "C2"],
                )
                for r in condition_facts(data)
            ]
        }

    def corrected(payload):
        # Validation failure must not have applied any selected target yet.
        return response(payload)

    env.responses.extend([response, corrected])
    affected = await env.executor.check_conditions(changes, set(), c)
    assert len(affected) == 2 and seen[0] == seen[1]
    assert (
        seen[0]["changes"][0]["after"]["text"] == "Delivery policy changed to express"
    )
    assert seen[0]["changes"][1]["after"]["text"] == "Owner Li"
    for target in targets:
        assert (await row(env, target))["status"] == "stale"
    async with env.store.pool.acquire() as conn:
        logs = await conn.fetch(
            f"SELECT stage,payload FROM memory_steps WHERE {SCOPE} ORDER BY step_id",
            *env.trace.args,
        )
    decision = next(
        r["payload"]["result"] for r in logs if r["stage"] == "p2_conditions.output"
    )
    assert {t["fact_ref"] for t in decision["triggers"]} == set(targets)
    assert all(t["change_ids"] == ["C1", "C2"] for t in decision["triggers"])
    error_index = next(
        i for i, r in enumerate(logs) if r["stage"] == "p2_conditions.validation_error"
    )
    output_index = next(
        i for i, r in enumerate(logs) if r["stage"] == "p2_conditions.output"
    )
    assert not any(
        r["stage"] == "p2.memory_change" for r in logs[error_index:output_index]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing_changes",
        "empty_changes",
        "unknown_change",
        "fact_as_change",
        "duplicate_changes",
        "duplicate_fact",
        "change_as_fact",
        "unknown_fact",
        "old_format",
    ],
)
async def test_invalid_condition_evidence_never_marks_targets_stale(env, fault):
    c, targets, changes = await setup_changes(env)

    def bad(payload):
        ref = condition_facts(input_of(payload))[0]["ref"]
        trigger = dict(fact_ref=ref, change_ids=["C1"])
        if fault == "old_format":
            return {"stale_refs": [ref]}
        if fault == "missing_changes":
            del trigger["change_ids"]
        elif fault == "empty_changes":
            trigger["change_ids"] = []
        elif fault == "unknown_change":
            trigger["change_ids"] = ["C99"]
        elif fault == "fact_as_change":
            trigger["change_ids"] = [ref]
        elif fault == "duplicate_changes":
            trigger["change_ids"] = ["C1", "C1"]
        elif fault == "change_as_fact":
            trigger["fact_ref"] = "C1"
        elif fault == "unknown_fact":
            trigger["fact_ref"] = "M999"
        return {
            "triggers": [trigger, trigger] if fault == "duplicate_fact" else [trigger]
        }

    env.responses.extend([bad, bad])
    with pytest.raises(OutputValidationFailed):
        await env.executor.check_conditions(changes, set(), c)
    assert len(env.prompts) == 2
    for target in targets:
        assert (await row(env, target))["status"] == "fresh"


@pytest.mark.asyncio
async def test_empty_triggers_leave_bound_facts_fresh(env):
    c, targets, changes = await setup_changes(env)
    env.responses.append({"triggers": []})
    assert await env.executor.check_conditions(changes, set(), c) == []
    for target in targets:
        assert (await row(env, target))["status"] == "fresh"
