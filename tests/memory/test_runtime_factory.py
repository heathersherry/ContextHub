"""Application lifespan uses the real configured engine and closes its resources."""

import json
import os
from pathlib import Path

import httpx
import pytest
from contexthub.main import create_app, ApplicationDependencies
from contexthub.config import Settings
from contexthub.memory.factory import MemoryConfig
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub_sdk import ContextHubClient, MemoryIngestRequest, MemoryContextRequest
from .conftest import CONFIG


def config_dict(tmp_path):
    value = json.loads(
        (
            Path(__file__).resolve().parents[2] / "examples/memory-config.json"
        ).read_text()
    )
    value["index"] = CONFIG["index"]
    value["embedding"].update(
        model=CONFIG["index"]["embedding_model"],
        dimensions=3,
        base_url="https://fixture.invalid/v1",
        response_archive_directory=str(tmp_path),
    )
    value["chat_base_url"] = "https://fixture.invalid/v1"
    value["chat_model"] = "fixture-chat"
    value["retry"] = value["embedding"]["retry"] = CONFIG["retry"]
    return value


def test_runtime_rejects_mismatched_index_and_implicit_jev_policy(tmp_path):
    data = config_dict(tmp_path)
    data["embedding"]["dimensions"] = 9
    with pytest.raises(ValueError, match="configurations differ"):
        MemoryConfig.model_validate(data)
    data = config_dict(tmp_path)
    data["p1_backend"] = "jev"
    with pytest.raises(ValueError, match="jev_retry"):
        MemoryConfig.model_validate(data)


@pytest.mark.asyncio
async def test_real_lifespan_sdk_and_cleanup(env, tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/embeddings"):
            inputs = json.loads(request.content)["input"]
            return httpx.Response(
                200,
                json={
                    "model": "fixture-embedding-v1",
                    "data": [
                        {"index": i, "embedding": [1.0, 0.0, 0.0]}
                        for i in range(len(inputs))
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "fixture-chat",
                "choices": [{"message": {"content": '{"rules":[],"contents":[],"forget_requests":[]}'}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    class Chat:
        async def close(self):
            pass

    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_dict(tmp_path)))
    app = create_app(
        Settings(
            _env_file=None,
            api_key="fixture",
            database_url=os.environ["MEMORY_DATABASE_URL"],
            memory_enabled=True,
            memory_config_path=str(path),
            lifecycle_enabled=False,
            propagation_enabled=False,
        ),
        dependencies=ApplicationDependencies(
            embedding_factory=lambda settings: NoOpEmbeddingClient(),
            chat_factory=lambda settings: Chat(),
            memory_chat_key="fixture",
            memory_embedding_key="fixture",
            memory_transport=httpx.MockTransport(handler),
        ),
    )
    async with app.router.lifespan_context(app):
        engine = app.state.memory_engine
        async with ContextHubClient(
            "http://local",
            "fixture",
            env.trace.tenant_id,
            "app",
            transport=httpx.ASGITransport(app=app),
        ) as client:
            receipt = await client.memory.ingest(
                MemoryIngestRequest(
                    namespace="chat",
                    sequence=1,
                    messages=[{"role": "user", "content": "hello"}],
                )
            )
            assert receipt.memory_refs == []
            result = await client.memory.context(
                MemoryContextRequest(namespace="chat", query="anything")
            )
            assert result.items == [] and result.sequence == 1
    assert engine.store.pool._closed and engine.store.lease_pool._closed
    assert engine.pipeline.extractor.client._client.is_closed
    assert engine.pipeline.retriever.indexer.client._client.is_closed
    assert "/v1/chat/completions" in calls
