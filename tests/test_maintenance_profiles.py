"""Transport and deadline contracts, no database replacement."""

import json
from types import SimpleNamespace
import httpx
import pytest
from pydantic import ValidationError
from contexthub.propagation.candidate import ModelCall, GenerationResponse
from s6_helpers import model_config, clock_frame, response
from knowledge_helpers import MemorySink


@pytest.mark.asyncio
@pytest.mark.parametrize("status,count", [(429, 3), (503, 3), (400, 1)])
async def test_transient_at_most_three_other_errors_once(status, count):
    calls = []
    sink = MemorySink()

    def send(request):
        calls.append(request.content)
        return httpx.Response(status, json={})

    client = ModelCall(
        model_config(),
        api_key="synthetic",
        recorder=sink,
        transport=httpx.MockTransport(send),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.invoke(
                clock_frame(),
                SimpleNamespace(profile="adaptive", action="M1"),
                {},
                json.loads,
                "generator",
            )
    finally:
        await client.close()
    assert len(calls) == count and len(set(calls)) == 1
    assert len([r for r in sink.rows if r.phase == "finished"]) == count


@pytest.mark.asyncio
async def test_schema_failure_one_attempt_and_usage_kept():
    calls = []
    sink = MemorySink()

    def send(request):
        calls.append(request)
        return response({"PASS": True})

    client = ModelCall(
        model_config(),
        api_key="synthetic",
        recorder=sink,
        transport=httpx.MockTransport(send),
    )
    try:
        with pytest.raises(ValidationError):
            await client.invoke(
                clock_frame(),
                SimpleNamespace(profile="adaptive", action="M1"),
                {},
                GenerationResponse.model_validate_json,
                "generator",
            )
    finally:
        await client.close()
    assert len(calls) == 1 and sink.rows[-1].usage.input_tokens == 11


def test_exact_deadline_is_expired(monkeypatch):
    from contexthub.propagation import candidate
    from contexthub.models.knowledge import ContractError

    frame = clock_frame()
    monkeypatch.setattr(candidate, "now", lambda: frame.deadline_at)
    with pytest.raises(ContractError, match="deadline_exhausted"):
        candidate.remaining(frame)
