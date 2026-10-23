"""Offline preview consumes the same rule-owned schema and target metadata."""

import json
import pytest

from scripts.memory_research.preview_repair import preview
from contexthub.memory.storage import SCOPE
from .helpers import add, link, repair_response, row, session


@pytest.mark.asyncio
async def test_preview_rebuilds_current_endpoint_repair_without_calls(env, tmp_path):
    c = await session(env, 1)
    source = await add(env, c, "Owner Li")
    target = await add(env, c, "Recipient Wang", status="stale")
    rule_text = "If owner changes, recipient follows owner."
    rule = await add(env, c, rule_text, type="rule")
    await link(env, rule, sources=[source], targets=[target])
    env.responses.append(repair_response("Recipient Li", ["Owner Li", rule_text]))
    await env.executor.repair(await row(env, target), [], c, set())
    async with env.store.pool.acquire() as conn:
        memories = await conn.fetch(
            f"SELECT * FROM memories WHERE {SCOPE}", *env.trace.args
        )
        steps = await conn.fetch(
            f"SELECT * FROM memory_steps WHERE {SCOPE} ORDER BY step_id",
            *env.trace.args,
        )
    run = tmp_path / "run"
    run.mkdir()
    (run / "memories.json").write_text(
        json.dumps([dict(r) for r in memories], default=str)
    )
    (run / "memory_steps.json").write_text(
        json.dumps([dict(r) for r in steps], default=str)
    )
    original = next(r for r in steps if r["stage"] == "p2_repair.input")
    output = tmp_path / "preview"
    count = len(env.prompts)
    await preview(run, original["step_id"], output)
    body = json.loads((output / "input.json").read_text())
    assert {r["text"] for r in body["materials"]} == {"Owner Li", rule_text}
    assert "target" not in body
    assert len(env.prompts) == count
    assert json.loads((output / "metadata.json").read_text())["model_calls"] == 0
