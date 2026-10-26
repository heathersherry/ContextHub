"""Rebuild repair input from a current-schema run snapshot, without model calls."""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from contexthub.memory.contracts import ref_of
from contexthub.memory.prompts_config import PromptSet
from contexthub.memory.maintenance_executor import MaintenanceExecutor


class RecordedRepairState:
    """Scoped reads over an exported snapshot and its existing evidence log."""

    def __init__(self, rows, evidence):
        self.prompts = PromptSet()
        self.rows = rows
        self.evidence = evidence

    async def read_ref(self, trace, key):
        return self.rows[key]

    async def current_rows(self, trace, matter_ids=None):
        latest = {}
        for r in self.rows.values():
            if r["temporal_scope"] != "current" or (
                matter_ids is not None and r["memory_id"] not in matter_ids
            ):
                continue
            old = latest.get(r["memory_id"])
            if old is None or r["version"] > old["version"]:
                latest[r["memory_id"]] = r
        return [latest[k] for k in sorted(latest)]

    async def active_rules(self, trace, *, affected_ids):
        return {
            ref_of(r): r
            for r in await self.current_rows(trace)
            if r["type"] == "rule"
            and r["status"] == "fresh"
            and r["accepted"]
            and not r["invalidated"]
            and set(r["affected_matter_ids"]) & set(affected_ids)
        }

    async def latest_repair_evidence(self, trace, key):
        return self.evidence

    async def record(self, context, stage, payload):
        assert stage == "p2.repair_evidence"
        self.evidence = payload


def memory_row(row):
    return {
        **row,
        "memory_id": UUID(row["memory_id"]),
        **{
            field: [UUID(uid) for uid in row[field]]
            for field in ("condition_matter_ids", "affected_matter_ids")
        },
    }


async def preview(run, step_id, output):
    steps = sorted(
        json.loads((run / "memory_steps.json").read_text()), key=lambda r: r["step_id"]
    )
    original = next(r for r in steps if r["step_id"] == step_id)
    assert original["stage"] == "p2_repair.input"
    target_key = original["payload"]["target"]
    rows = {
        ref_of(r): memory_row(r)
        for r in json.loads((run / "memories.json").read_text())
    }
    # Undo later status/version and binding writes; no conversion of legacy schemas.
    for step in reversed(steps):
        if step["step_id"] < step_id:
            break
        if step["stage"] == "p2.memory_change":
            before = memory_row(step["payload"]["before"])
            rows[ref_of(before)] = before
        elif step["stage"] == "p1.output":
            for field, ids in step["payload"]["before"].items():
                rows[step["payload"]["rule"]][field] = [UUID(uid) for uid in ids]
    evidence = next(
        (
            s["payload"]
            for s in reversed(steps)
            if s["step_id"] < step_id
            and s["stage"] in ("p2.condition_evidence", "p2.repair_evidence")
            and s["payload"]["target"] == target_key
        ),
        None,
    )
    state = RecordedRepairState(rows, evidence)
    context = SimpleNamespace(trace=None, session_seq=original["session_seq"])
    executor = MaintenanceExecutor(state, None, None, None, retry=None)
    target = rows[target_key]
    materials, causes = await executor.repair_evidence(target, [], context)
    inputs, refs = await executor.repair_inputs(
        target, materials, causes, context, retrieval_available=True
    )
    rendered = executor.render_request("p2_repair", inputs)
    instructions, body = rendered.rsplit("\n", 1)
    sent_inputs = json.loads(body)
    output.mkdir(parents=True, exist_ok=True)
    (output / "input-exact.txt").write_text(rendered)
    (output / "input-readable.txt").write_text(
        instructions
        + "\n"
        + json.dumps(sent_inputs, ensure_ascii=False, indent=2)
        + "\n"
    )
    (output / "input.json").write_text(
        json.dumps(sent_inputs, ensure_ascii=False, indent=2) + "\n"
    )
    metadata = {
        "source_run": str(run.resolve()),
        "source_step": step_id,
        "kind": "offline reconstruction of initial repair evidence using current schema",
        "model_calls": 0,
        "retrieval_calls": 0,
        "reference_map": refs.decode_map,
        "facts": sum(m["type"] == "fact" for m in inputs["materials"]),
        "rules": sum(m["type"] == "rule" for m in inputs["materials"]),
        "changes": len(inputs["changes"]),
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--step", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(preview(args.run, args.step, args.output))
