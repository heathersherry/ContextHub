"""Actual router, dependencies, authentication and closed request schema."""

import httpx
import pytest
from uuid import uuid4
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
            await client.post("/api/v1/knowledge/ingest", json={})
        ).status_code == 401
        client.headers.update(
            {"X-API-Key": "fixture", "X-Account-Id": "a", "X-Agent-Id": "b"}
        )
        response = await client.post(
            "/api/v1/knowledge/ingest",
            json={"request_id": str(uuid4()), "content": "ordinary text"},
        )
        assert (
            response.status_code == 503
            and response.json()["detail"] == "knowledge_disabled"
        )
        assert (
            await client.get("/api/v1/knowledge/no-such-operation")
        ).status_code == 404


def test_schema_has_no_internal_authority_or_tenant_fields():
    schema = create_app().openapi()
    for name in ("KnowledgeIngestRequest", "KnowledgeMaintainRequest"):
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
    assert len([p for p in schema["paths"] if p.startswith("/api/v1/knowledge/")]) == 3
