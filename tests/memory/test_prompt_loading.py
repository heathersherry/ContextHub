from .helpers import rule_item
from .helpers import bindings_response
"""Exercise frozen prompt loading through actual fixture HTTP requests."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from contexthub.memory import contracts
from integrations.memebench.memory_research.cli import freeze
from integrations.memebench.memory_research.official import load_official
from integrations.memebench.memory_research.runner import MemoryAnswerer
from .helpers import (
    add,
    bind,
    direct,
    direct_response,
    item,
    pending,
    repair_response,
    row,
    session,
    targets_response,
    trigger_response,
)

JUDGE_SOURCE = (
    "/Users/sherrylin/Documents/PythonProjects/public/MEME-public/code/eval/judge.py"
)


def frozen_prompts(tmp_path):
    output = tmp_path / "frozen"
    manifest = freeze(
        output,
        {"index": {"embedding_model": "text-embedding-3-small", "dimensions": 1536}},
        SimpleNamespace(
            mode="check",
            embedding_provider="openlux",
            memory_model="gpt-4.1-mini",
            episode="fixture",
            data="fixture",
            p1_backend="llm",
            p2_direct_backend="llm",
            rule_gate_backend="llm",
            p1_jev_threshold=0.5,
            topic_similarity_threshold=0.8,
        ),
    )
    for name, digest in manifest["prompt_sha256"].items():
        assert (
            hashlib.sha256((output / "prompts" / name).read_bytes()).hexdigest()
            == digest
        )
        assert (output / "prompts" / name).read_bytes() == (
            contracts.PROMPTS / name
        ).read_bytes()
    return output / "prompts"


@pytest.mark.asyncio
async def test_frozen_prompts_reach_extraction_maintenance_retry_and_answer(
    env, tmp_path, monkeypatch
):
    frozen = frozen_prompts(tmp_path)
    names = [
        "extractor",
        "extractor_facts",
        "p2_direct",
        "p2_forget_scope",
        "p1_conditions",
        "p2_conditions",
        "p2_repair",
        "output_retry",
        "evaluation/answer",
        "answer_guidance",
    ]
    expected = {}
    for name in names:
        # Test-only marker distinguishes the frozen copy from the repository file.
        path = frozen / (name + ".txt")
        expected[name] = path.read_text() + "\nFrozen prompt loading test.\n"
        path.write_text(expected[name])
    official = json.loads(
        (frozen / "evaluation" / "official_manifest.json").read_text()
    )
    official["answer_sha256"] = hashlib.sha256(
        expected["evaluation/answer"].encode()
    ).hexdigest()
    (frozen / "evaluation" / "official_manifest.json").write_text(json.dumps(official))
    monkeypatch.setattr(contracts, "PROMPTS", frozen)
    env.retriever.config = env.retriever.config.model_copy(
        update={"hybrid_limit": 20, "text_k": 20, "vector_k": 20}
    )
    rule_text = "If owner changes, then recipient follows owner"
    env.responses.extend(
        [
            {"items": [rule_item(rule_text)]},
            {"items": [item("Owner Wang")]},
            "{bad",
            direct_response("update", "Owner Wang"),
            bindings_response(["Owner Li"], ["Recipient Wang"]),
            trigger_response(["Recipient Wang"], ["C1"]),
            repair_response("Recipient Li", [rule_text, "Owner Li"]),
            "Li",
            direct_response("forget", "Owner Li"),
            targets_response(["Owner Li"]),
        ]
    )
    await env.pipeline.ingest(
        env.trace, 1, [{"role": "user", "content": rule_text}], timestamp="2026-10-12"
    )
    c1 = env.context
    rule = await add(env, c1, rule_text, type="rule")
    await add(env, c1, "Owner Wang")
    recipient = await add(env, c1, "Recipient Wang")
    c2 = await session(env, 2)
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE session_records SET timestamp=$1 WHERE run_id=$2 AND episode_id=$3 AND session_seq=2",
            "2026-10-13",
            *env.trace.args[:2],
        )
    new, changes = await direct(env, pending(c2, "Owner Li"), c2)
    await bind(env, [rule, new], c2)
    await env.executor.check_conditions(changes, {new}, c2)
    await env.executor.repair(await row(env, recipient), changes, c2, set())
    answerer = MemoryAnswerer(
        env.store, env.retriever, env.executor.client, env.executor.retry
    )
    assert await answerer.answer("Who is the recipient?", c2, timings={}) == "Li"
    await direct(env, pending(c2, "Forget the owner"), c2)
    sent = [r["messages"][0]["content"] for r in env.prompts]
    assert len(sent) == 10 and not env.responses
    for index, name in [
        (0, "extractor"),
        (1, "extractor_facts"),
        (2, "p2_direct"),
        (4, "p1_conditions"),
        (5, "p2_conditions"),
        (6, "p2_repair"),
        (8, "p2_direct"),
        (9, "p2_forget_scope"),
    ]:
        instructions = expected[name]
        if name == "p2_repair":
            instructions = instructions.replace("{target_statement}", '"Recipient Wang"')
        assert sent[index].startswith(instructions + "\n")
        if name in ("p2_repair", "p2_forget_scope", "p1_conditions", "p2_conditions"):
            assert '"timestamp"' not in sent[index].splitlines()[-1]
            assert '"created_in_session"' not in sent[index].splitlines()[-1]
        elif index >= 2:
            assert '"timestamp"' in sent[index].splitlines()[-1]
    assert (await row(env, rule))["timestamp"] == "2026-10-12"
    assert (await row(env, new))["timestamp"] == "2026-10-13"
    assert sent[3].startswith(expected["output_retry"])
    assert expected["p2_direct"] in sent[3]
    assert sent[7].endswith("\nFrozen prompt loading test.\n")


def test_official_judge_checks_active_frozen_files(tmp_path, monkeypatch):
    frozen = frozen_prompts(tmp_path)
    monkeypatch.setattr(contracts, "PROMPTS", frozen)
    load_official(JUDGE_SOURCE)
    path = frozen / "evaluation" / "judge_before_common_prompt.txt"
    path.write_text(path.read_text() + "\nmodified fixture")
    with pytest.raises(ValueError, match="official prompt was modified"):
        load_official(JUDGE_SOURCE)
