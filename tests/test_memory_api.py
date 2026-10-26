"""Actual router, dependencies, authentication and closed request schema."""

import httpx
import pytest
from contexthub.main import create_app
from contexthub.config import Settings


@pytest.mark.asyncio
async def test_real_router_auth_disabled_and_unknown():
    app = create_app()
    app.state.settings = Settings(_env_file=None, api_key="fixture")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (
            await client.post("/api/v1/memories/ingest", json={})
        ).status_code == 401
        client.headers.update(
            {"X-API-Key": "fixture", "X-Account-Id": "a", "X-Agent-Id": "b"}
        )
        response = await client.post(
            "/api/v1/memories/ingest",
            json={"namespace": "chat", "sequence": 1, "messages": [{"role": "user", "content": "ordinary text"}]},
        )
        assert (
            response.status_code == 503
            and response.json()["detail"] == "memory_disabled"
        )
        assert (
            await client.get("/api/v1/memories/no-such-operation")
        ).status_code == 404


def test_schema_has_no_internal_authority_or_tenant_fields():
    schema = create_app().openapi()
    for name in ("MemoryIngestRequest", "MemoryContextRequest"):
        fields = schema["components"]["schemas"][name]
        assert fields["additionalProperties"] is False
        assert (
            not {
                "account_id",
                "agent_id",
                "proof",
                "fresh",
                "verdict",
                "gold",
                "task_type",
                "hop",
            }
            & fields["properties"].keys()
        )
    assert len([p for p in schema["paths"] if p.startswith("/api/v1/memories/")]) == 3
