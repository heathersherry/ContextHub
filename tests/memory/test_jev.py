from contexthub.memory.prompts_config import PromptSet
"""Jev wire contract, durable retries, and shared P1 persistence."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from uuid import uuid4

import httpx
import pytest

from contexthub.llm.jev_client import JevClient, JEV_MODEL
from contexthub.llm.retry_policy import RecordedCall, ResponseSchemaError
from contexthub.memory.jev_condition_binder import JevConditionBinder
from contexthub.models.execution import CallIdentity, PriceBasis
from contexthub.models.knowledge import canonical_hash
from integrations.memebench.memory_research.cli import (
    argument_parser,
    freeze,
    decision_retry_policy,
)
from .helpers import add, row, session


def payload():
    return {
        "model": JEV_MODEL,
        "state": "fixture",
        "questions": {"M1": {"type": "noul", "instructions": "Constrained?"}},
    }


def result(answers=None):
    return {
        "model": JEV_MODEL,
        "answers": answers
        if answers is not None
        else {"M1": {"type": "noul", "noul": 0.8}},
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


class Sink:
    def __init__(self):
        self.records = []

    async def append(self, record):
        self.records.append(record)


def call(sink):
    return RecordedCall(
        identity=CallIdentity(
            account_id="fixture",
            execution_id=uuid4(),
            call_id=uuid4(),
            strategy="local_base",
            stage="condition_binding",
            operation="fixture",
            model_role="p1",
            code_version="fixture",
            config_sha256=canonical_hash({}),
        ),
        policy=decision_retry_policy("jev"),
        recorder=sink,
        capture_raw_output=True,
        price=PriceBasis(
            source="fixture",
            observed_at=datetime.now(timezone.utc),
            currency="USD",
            region="global",
            billing_tier="standard",
            model=JEV_MODEL,
            unit_tokens=1000000,
            input_rate="0.042",
            output_rate="0",
        ),
    )


@pytest.mark.asyncio
async def test_retry_raw_usage_cost_and_no_secret():
    sink, requests, sleeps = Sink(), [], []

    def handler(request):
        requests.append(request)
        assert len(sink.records) == 2 * len(requests) - 1
        if len(requests) == 1:
            return httpx.Response(
                529, json={"error": "overloaded"}, headers={"Retry-After": "12"}
            )
        return httpx.Response(
            200, json=result(), headers={"x-request-id": "jev-request"}
        )

    async def sleep(seconds):
        sleeps.append(seconds)

    client = JevClient("secret-fixture", transport=httpx.MockTransport(handler))
    try:
        assert await client.evaluate(
            payload(), call=replace(call(sink), sleep=sleep)
        ) == {"M1": {"type": "noul", "noul": 0.8}}
    finally:
        await client.close()
    assert sleeps == [12]
    assert requests[0].url == "https://api.typesafe.ai/v1/systemone"
    assert json.loads(requests[0].content) == payload()
    assert sink.records[1].stop_reason == "retry_scheduled"
    final = sink.records[-1]
    assert final.actual_model == JEV_MODEL and final.request_id == "jev-request"
    assert final.usage.input_tokens == 100 and final.usage.total_tokens == 110
    assert str(final.cost.amount) == "0.0000042"
    assert json.loads(final.raw_output) == result()
    assert "secret-fixture" not in json.dumps(
        [r.model_dump(mode="json") for r in sink.records]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"M2": {"type": "noul", "noul": 0.8}},
        {"M1": {"type": "choice", "noul": 0.8}},
        {"M1": {"type": "noul", "noul": True}},
        {"M1": {"type": "noul", "noul": 1.1}},
    ],
)
async def test_invalid_answers_stop_without_retry_and_keep_usage(answers):
    sink = Sink()
    client = JevClient(
        "fixture",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=result(answers))
        ),
    )
    try:
        with pytest.raises(ResponseSchemaError):
            await client.evaluate(payload(), call=call(sink))
    finally:
        await client.close()
    assert len(sink.records) == 2
    assert sink.records[-1].usage.input_tokens == 100
    assert sink.records[-1].stop_reason == "non_retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [(401, {"error": "invalid key"}), (429, {"error": "insufficient_balance"})],
)
async def test_account_error_does_not_retry(status, body):
    sink = Sink()
    client = JevClient(
        "fixture",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.evaluate(payload(), call=call(sink))
    finally:
        await client.close()
    assert len(sink.records) == 2 and sink.records[-1].stop_reason == "non_retryable"


@pytest.mark.asyncio
async def test_cancel_and_ledger_failure_never_retry():
    sink = Sink()

    async def cancelled(_):
        raise asyncio.CancelledError()

    client = JevClient("fixture", transport=httpx.MockTransport(cancelled))
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.evaluate(payload(), call=call(sink))
    finally:
        await client.close()
    assert sink.records[-1].stop_reason == "cancelled" and len(sink.records) == 2

    async def broken(_):
        raise RuntimeError("ledger unavailable")

    sink.append = broken
    sent = []
    client = JevClient(
        "fixture", transport=httpx.MockTransport(lambda req: sent.append(req))
    )
    try:
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            await client.evaluate(payload(), call=call(sink))
    finally:
        await client.close()
    assert not sent


@pytest.mark.asyncio
async def test_jev_shared_binding_multi_none_and_frozen_prompt(
    env, tmp_path, monkeypatch
):
    c = await session(env, 1)
    # Two query scores filter the invoice out before Jev; its probability
    # cutoff remains 0.5, independently of the 0.15 retrieval cutoff.
    env.vectors["document: Invoices monthly"] = [0.0, 1.0, 0.0]
    rule = await add(
        env, c, "If supplier changes, delivery schedule changes", type="rule"
    )
    fact_refs = [
        await add(env, c, text)
        for text in [
            "Supplier Oak",
            "Deliveries Tuesday",
            "Deliveries Friday",
            "Invoices monthly",
        ]
    ]
    rows = {ref: await row(env, ref) for ref in [rule, *fact_refs]}
    path = tmp_path / "p1_jev.txt"
    path.write_text(PromptSet().text("p1_jev") + "\nFrozen marker")
    env.store.prompts = PromptSet(tmp_path)
    requests = []

    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        assert len(data["questions"]) == 6
        assert data["state"]["binding_policy"] == path.read_text()
        assert json.dumps(data).count("Frozen marker") == 1
        assert "Invoices monthly" not in {r["text"] for r in data["state"]["candidates"]}
        answers = {}
        for question, config in data["questions"].items():
            role, ref = question.split(":")
            candidate = next(r for r in data["state"]["candidates"] if r["ref"] == ref)
            assert config["instructions"].startswith("Apply state.binding_policy")
            assert f"Role: {role}." in config["instructions"]
            assert f"Evaluate candidate {ref}." in config["instructions"]
            matches = candidate['text'].startswith('Supplier' if role == 'condition' else 'Deliveries')
            probability = 0.5 if matches else 0.49
            answers[question] = {'type': 'noul', 'noul': probability if len(requests) == 1 else 0}
        return httpx.Response(200, json=result(answers))

    client = JevClient("fixture", transport=httpx.MockTransport(handler))
    binder = JevConditionBinder(env.store, client, env.retriever, retry=decision_retry_policy("jev"))
    assert binder.threshold == 0.5
    try:
        await binder.bind({rule: rows}, c)
        await binder.bind({rule: rows}, c)
        await binder.bind({rule: {}}, c)  # prior targets remain candidates
    finally:
        await client.close()
    rule_row = await row(env, rule)
    from contexthub.memory.contracts import split_ref
    assert rule_row['condition_matter_ids'] == [split_ref(fact_refs[0])[0]]
    assert set(rule_row['affected_matter_ids']) == {split_ref(r)[0] for r in fact_refs[1:3]}
    assert not env.prompts  # P1 did not call the original chat client
    async with env.store.pool.acquire() as conn:
        decisions = await conn.fetchval(
            "SELECT count(*) FROM memory_steps WHERE tenant_id=$1 AND namespace=$2 AND stage='p1.jev_decisions'",
            *env.trace.args[:2],
        )
    assert decisions == 3


def test_cli_defaults_and_frozen_metadata(tmp_path):
    parser = argument_parser()
    common = ["--config", "fixture", "--mode", "check", "--output", str(tmp_path)]
    default = parser.parse_args(common)
    assert default.p1_backend == "llm"
    args = parser.parse_args(
        common + ["--p1-backend", "jev", "--p1-jev-threshold", "0.7"]
    )
    manifest = freeze(tmp_path / "frozen", {"index": {"embedding_model": "text-embedding-3-small", "dimensions": 1536}}, args)
    assert manifest["model_parameters"]["p1"]["model"] == JEV_MODEL
    assert manifest["model_parameters"]["p1"]["threshold"] == 0.7
    assert manifest["p1_candidate_filter"]["min_similarity"] == 0.15
    assert manifest["model_parameters"]["p2"]["model"] == "gpt-4.1-mini"
    assert 529 in manifest["p1_retry"]["retry_status_codes"]
    assert manifest["model_output_attempts"]["p1"] == 1
    for value in ("nan", "inf", "-0.1", "1.1"):
        with pytest.raises(SystemExit):
            parser.parse_args(common + ["--p1-jev-threshold", value])
