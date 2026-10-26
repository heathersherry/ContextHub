from .helpers import extraction_response
from .helpers import link
"""Invalid repair output is local; execution failures still propagate."""

from .helpers import repair_target

import httpx
import pytest

from contexthub.memory.storage import SCOPE
from .helpers import add, row, session, input_of, item, direct_response, repair_response


@pytest.mark.asyncio
async def test_failed_repair_allows_other_repairs_and_later_session(env):
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 20, "vector_k": 20, "hybrid_limit": 20}
    )
    c = await session(env, 1)
    rule_text = "If owner changes, primary and backup recipients follow the owner."
    rule = await add(env, c, rule_text, type="rule")
    owner = await add(env, c, "Owner Li")
    targets = [
        await add(env, c, text, status="stale", basis=[owner, rule], origin="derived")
        for text in ("Primary recipient Wang", "Backup recipient Wang")
    ]
    await link(env, rule, sources=[owner], targets=targets)
    # Current rows are ordered by UUID; fail the first repair, then succeed.
    failed, succeeding = sorted(targets)
    failed_text = (await row(env, failed))["text"]
    succeeding_text = (await row(env, succeeding))["text"]
    repaired_text = succeeding_text.replace("Wang", "Li")
    confirmed_text = failed_text.replace("Wang", "Chen")
    seen = []
    repair_targets = []

    def response(payload):
        data = input_of(payload)
        seen.append(data)
        if "messages" in data:
            current_text = data["messages"][0]["content"]
            return extraction_response(rules=[], contents=[]
                if current_text == "Nothing to add."
                else [item(confirmed_text)])
        if "materials" in data:
            repair_targets.append(repair_target(payload))
            if repair_target(payload) == failed_text:
                return dict(
                    can_repair=True,
                    retrieval_query=None,
                    text="Unsupported replacement",
                    entities=[],
                    basis_refs=[
                        m["ref"] for m in data["materials"] if m["type"] == "rule"
                    ],
                )
            assert repair_target(payload) == succeeding_text
            return repair_response(repaired_text, ["Owner Li", rule_text])(payload)
        if "checks" in data:
            return {"triggers": []}
        if "incoming" in data:
            return direct_response("update", failed_text)(payload)
        assert "rule" in data
        return {
            "target_refs": [
                r["ref"] for r in data["candidates"] if "recipient" in r["text"]
            ]
        }

    env.responses.extend([response] * 20)
    c2 = await session(env, 2)
    await env.full_pipeline.maintain_conditions([], set(), c2, [rule])
    assert repair_targets[:3] == [
        failed_text,
        failed_text,
        succeeding_text,
    ]
    assert (await row(env, failed))["status"] == "stale"
    current = await env.store.current_rows(env.trace)
    assert any(r["text"] == repaired_text and r["status"] == "fresh" for r in current)
    assert not any(r["text"] == "Unsupported replacement" for r in current)

    # A direct assertion in a later session may still replace the stale value.
    await env.full_pipeline.ingest(
        env.trace, 3, [{"role": "user", "content": confirmed_text}]
    )
    current = await env.store.current_rows(env.trace)
    assert any(r["text"] == confirmed_text and r["status"] == "fresh" for r in current)
    async with env.store.pool.acquire() as conn:
        complete = await conn.fetch(
            f"SELECT session_seq FROM memory_steps WHERE {SCOPE} AND stage='session.complete' ORDER BY session_seq",
            *env.trace.args,
        )
        failures = await conn.fetchval(
            f"SELECT count(*) FROM memory_steps WHERE {SCOPE} AND stage='session.failed'",
            *env.trace.args,
        )
    assert [r["session_seq"] for r in complete] == [3]
    assert failures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["configuration", "transport", "logging"])
async def test_repair_does_not_swallow_execution_errors(env, monkeypatch, fault):
    c = await session(env, 1)
    rule = await add(env, c, "If owner changes, recipient follows owner.", type="rule")
    await add(env, c, "Owner Li")
    target = await add(env, c, "Recipient Wang", status="stale")
    await link(env, rule, targets=[target])
    if fault == "configuration":
        env.responses.append(ValueError("configuration failure"))
        expected = ValueError
    elif fault == "transport":
        env.responses.append(401)
        expected = httpx.HTTPStatusError
    else:
        env.responses.append("{bad")
        record = env.store.record

        async def failing_record(context, stage, payload, **kwargs):
            if stage == "p2_repair.validation_error":
                raise ValueError("database log write failure")
            return await record(context, stage, payload, **kwargs)

        monkeypatch.setattr(env.store, "record", failing_record)
        expected = ValueError
    with pytest.raises(expected):
        await env.executor.repair(await row(env, target), [], c, set())
    assert len(env.prompts) == 1
    async with env.store.pool.acquire() as conn:
        skipped = await conn.fetchval(
            f"SELECT count(*) FROM memory_steps WHERE {SCOPE} AND stage='p2.repair_skipped'",
            *env.trace.args,
        )
    assert skipped == 0
    assert (await row(env, target))["status"] == "stale"
