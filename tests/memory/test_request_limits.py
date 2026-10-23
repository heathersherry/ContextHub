import asyncio
import json
import httpx
import pytest
from knowledge_helpers import MemorySink, identity, policy
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall, ResponseSchemaError
from integrations.memebench.memory_research.cli import memory_retry_policy


@pytest.mark.asyncio
@pytest.mark.parametrize("success_at", [2, 5, None])
async def test_memory_retry_escalates_both_deadlines_and_stops(monkeypatch, success_at):
    deadlines, requests, waits = [], [], []
    original_timeout = asyncio.timeout

    def timeout(seconds):
        deadlines.append(seconds)
        return original_timeout(seconds)

    def handler(request):
        requests.append(request)
        if len(requests) != success_at:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "timeout", timeout)
    sink = MemorySink()
    client = OpenAIChatClient(
        "fixture", model="gpt-6-luna", transport=httpx.MockTransport(handler)
    )
    call = RecordedCall(identity(), memory_retry_policy(), sink, sleep=sleep)
    try:
        if success_at is None:
            with pytest.raises(httpx.ReadTimeout):
                await client.complete("same request", call=call)
        else:
            assert await client.complete("same request", call=call) == "ok"
    finally:
        await client.close()
    count = success_at or 5
    expected = [60, 90, 120, 150, 180][:count]
    assert deadlines == expected
    assert [r.extensions["timeout"]["read"] for r in requests] == expected
    assert len({r.content for r in requests}) == 1
    assert waits == [10, 30, 30, 30][:count - 1]
    assert [r.attempt_no for r in sink.rows[1::2]] == list(range(1, count + 1))
    assert sink.rows[-1].stop_reason == ("success" if success_at else "retry_exhausted")


@pytest.mark.asyncio
async def test_omitted_limit_full_request_and_retry_after():
    requests = []
    waits = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                429,
                json={"error": {"code": "rate_limit"}},
                headers={"Retry-After": "45"},
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def sleep(value):
        waits.append(value)

    sink = MemorySink()
    client = OpenAIChatClient(
        "fixture", model="fixture", transport=httpx.MockTransport(handler)
    )
    try:
        result = await client.complete(
            "complete prompt",
            max_tokens=None,
            call=RecordedCall(
                identity(), policy(), sink, capture_raw_output=True, sleep=sleep
            ),
        )
    finally:
        await client.close()
    assert result == "ok" and waits == [45]
    assert all("max_tokens" not in r for r in requests)
    assert sink.rows[0].request_payload["messages"][0]["content"] == "complete prompt"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["balance", "truncated", "deadline"])
async def test_nonretryable_balance_truncation_and_total_deadline(failure):
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if failure == "balance":
            return httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
        if failure == "deadline":
            await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"valid":"but truncated"}'},
                    }
                ]
            },
        )

    sink = MemorySink()
    client = OpenAIChatClient(
        "fixture", model="fixture", transport=httpx.MockTransport(handler)
    )
    retry = policy().model_copy(
        update={"timeout_seconds": 0.01, "backoff_seconds": (0, 0)}
    )
    try:
        with pytest.raises((httpx.HTTPStatusError, ResponseSchemaError, TimeoutError)):
            await client.complete("x", call=RecordedCall(identity(), retry, sink))
    finally:
        await client.close()
    assert count == (3 if failure == "deadline" else 1)
