"""Replay inconsistent Choice responses through the real client and ledger contract."""
from dataclasses import replace
import json

import httpx
import pytest

from contexthub.llm.jev_client import ChoiceProbabilityMismatch, JevClient, JEV_MODEL
from contexthub.llm.retry_policy import ResponseSchemaError
from contexthub.services.execution_ledger import validate_append
from .test_jev import Sink, call, result


def request_payload():
    return {
        "model": JEV_MODEL,
        "state": "unchanged rule candidate",
        "questions": {"R9": {"type": "choice", "criteria": {
            "rule": "Keep", "not_rule": "Discard", "unclear": "Keep for confirmation",
        }}},
    }


def choice(*, consistent=False):
    # The actual pl_050 / sw_004 failure, without modifying its probabilities.
    return {"type": "choice", "choice": "not_rule" if consistent else "rule",
            "confidence": 0.23,
            "probabilities": {"rule": 0.48, "not_rule": 0.49, "unclear": 0.03}}


async def exercise(responses, *, limit=3, payload=None):
    sink, sent, sleeps = Sink(), [], []
    payload = payload or request_payload()

    def handler(request):
        sent.append(json.loads(request.content))
        response = responses[len(sent) - 1]
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=result(response))

    async def sleep(seconds):
        sleeps.append(seconds)

    recorded = call(sink)
    recorded = replace(recorded, sleep=sleep, policy=recorded.policy.model_copy(update={
        "max_attempts": limit, "backoff_seconds": tuple(range(1, limit)),
    }))
    client = JevClient("fixture", transport=httpx.MockTransport(handler))
    answer, error = None, None
    try:
        answer = await client.evaluate(payload, call=recorded)
    except ResponseSchemaError as exc:
        error = exc
    finally:
        await client.close()
    previous = []
    for record in sink.records:
        assert validate_append(previous, record)
        previous.append(record)
    assert sent == [payload] * len(sent)
    assert len({record.identity.call_id for record in sink.records}) == 1
    assert not recorded.policy.is_retryable(ChoiceProbabilityMismatch("fixture"))
    return answer, error, sink.records, sent, sleeps


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [0, 1, 2])
async def test_stops_at_first_consistent_result_and_accounts_every_attempt(failures):
    invalid, valid = {"R9": choice()}, {"R9": choice(consistent=True)}
    answer, error, records, sent, sleeps = await exercise([invalid] * failures + [valid])
    assert error is None and answer == valid
    assert len(sent) == failures + 1 and sleeps == list(range(1, failures + 1))
    ends = records[1::2]
    assert [record.stop_reason for record in ends] == ["retry_scheduled"] * failures + ["success"]
    assert sum(record.usage.total_tokens for record in ends) == 110 * len(sent)
    assert sum(record.cost.amount for record in ends) == ends[0].cost.amount * len(sent)
    for record in ends[:-1]:
        assert record.error.code == "ChoiceProbabilityMismatch"
        assert record.error.retryable
        assert json.loads(record.raw_output)["answers"] == invalid
    assert json.loads(ends[-1].raw_output)["answers"] == valid


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 3, 5])
async def test_repeated_mismatch_stops_at_three_or_smaller_caller_budget(limit):
    invalid = {"R9": choice()}
    answer, error, records, sent, sleeps = await exercise([invalid] * 5, limit=limit)
    expected = min(limit, 3)
    assert answer is None and isinstance(error, ChoiceProbabilityMismatch)
    assert "R9" in str(error)
    assert len(sent) == expected and len(sleeps) == expected - 1
    assert records[-1].stop_reason == "retry_exhausted"
    assert sum(record.usage.total_tokens for record in records[1::2]) == 110 * expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch_first", [True, False])
async def test_other_schema_error_in_same_response_still_stops_immediately(mismatch_first):
    payload = request_payload()
    payload["questions"]["bad"] = payload["questions"]["R9"]
    malformed = {**choice(consistent=True), "confidence": True}
    answers = {"R9": choice(), "bad": malformed} if mismatch_first else {"bad": malformed, "R9": choice()}
    answer, error, records, sent, sleeps = await exercise([answers], payload=payload)
    assert answer is None and type(error) is ResponseSchemaError
    assert len(sent) == 1 and not sleeps
    assert records[-1].stop_reason == "non_retryable"
    assert not records[-1].error.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch_first", [True, False])
async def test_transport_errors_and_mismatch_share_budget(mismatch_first):
    mismatch = {"R9": choice()}
    overload = httpx.Response(529, json={"error": "overloaded"})
    responses = [mismatch, overload, mismatch] if mismatch_first else [overload, mismatch, mismatch]
    answer, error, records, sent, sleeps = await exercise(responses, limit=5)
    assert answer is None and isinstance(error, ChoiceProbabilityMismatch)
    assert len(sent) == 3 and sleeps == [1, 2]
    assert [r.stop_reason for r in records[1::2]] == ["retry_scheduled", "retry_scheduled", "retry_exhausted"]


@pytest.mark.asyncio
async def test_later_unrelated_schema_error_is_not_retried():
    answer, error, records, sent, sleeps = await exercise([{"R9": choice()}, {}])
    assert answer is None and type(error) is ResponseSchemaError
    assert len(sent) == 2 and sleeps == [1]
    assert records[-1].stop_reason == "non_retryable"
