import sys
from pathlib import Path
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk/src"))
from contexthub_sdk import (
    ContextHubClient,
    MemoryIngestRequest,
    MemoryContextRequest,
    ConflictError,
    ServerError,
)
from contexthub.memory.public import (
    MemoryIngestRequest as ServerIngest,
    MemoryContextRequest as ServerMaintain,
    MemoryContextResult as ServerResult,
)
from contexthub_sdk.models import MemoryContextResult


def test_wire_models_match_without_importing_server():
    for server, sdk in (
        (ServerIngest, MemoryIngestRequest),
        (ServerMaintain, MemoryContextRequest),
        (ServerResult, MemoryContextResult),
    ):
        assert server.model_json_schema() == sdk.model_json_schema()
    package = Path(__file__).resolve().parents[1] / "sdk/src/contexthub_sdk"
    for p in package.glob("*.py"):
        assert "from contexthub." not in p.read_text()
        assert "integrations.memebench" not in p.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(409, ConflictError), (503, ServerError)])
async def test_no_implicit_retry_headers_error_mapping_and_close(status, error):
    calls = []

    def send(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "fixture"
        assert request.headers["X-Agent-Id"] == "reader"
        return httpx.Response(status, json={"detail": "knowledge_operation_conflict"})

    async with ContextHubClient(
        "http://local", "fixture", "a", "reader", transport=httpx.MockTransport(send)
    ) as client:
        with pytest.raises(error):
            await client.memory.ingest(
                MemoryIngestRequest(namespace="chat", sequence=1, messages=[{"role":"user", "content":"ordinary content"}])
            )
        assert len(calls) == 1
    assert client._http.is_closed
