import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.recorded_embedding import EmbeddingConfig, RecordedEmbeddingClient
from contexthub.llm.retry_policy import RetryPolicy
from contexthub.memory.calls import CallContext
from contexthub.memory.extractor import Extractor
from contexthub.memory.indexer import Indexer
from contexthub.memory.pipeline import MemoryPipeline
from contexthub.memory.retriever import Retriever
from contexthub.memory.schemas import IndexConfig, RetrievalConfig, Trace
from contexthub.memory.storage import MemoryStore

CONFIG = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs/memory-research/validation-config.json"
    ).read_text()
)


@pytest_asyncio.fixture
async def env(tmp_path):
    dsn = os.getenv("MEMORY_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "MEMORY_DATABASE_URL must name an initialized dedicated research database"
        )
    store = await MemoryStore.open(dsn, IndexConfig.model_validate(CONFIG["index"]))
    trace = Trace(
        run_id=os.getenv("MEMORY_VALIDATION_RUN_ID", "test-" + uuid4().hex),
        episode_id=uuid4().hex,
        strategy_id="local_base",
    )
    responses, prompts, embedded = [], [], []
    vectors = {}

    async def chat_handler(request):
        prompts.append(json.loads(request.content))
        result = responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, int):
            return httpx.Response(
                result, json={"error": {"message": "fixture transport failure"}}
            )
        return httpx.Response(
            200,
            json={
                "model": "fixture-chat",
                "choices": [{"message": {"content": result}}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    def embedding_handler(request):
        inputs = json.loads(request.content)["input"]
        embedded.extend(inputs)
        return httpx.Response(
            200,
            json={
                "model": "fixture-embedding-v1",
                "data": [
                    {"index": i, "embedding": vectors.get(text, [1.0, 0.0, 0.0])}
                    for i, text in enumerate(inputs)
                ],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    retry = RetryPolicy.model_validate(CONFIG["retry"])
    client = OpenAIChatClient(
        api_key="fixture",
        base_url="https://fixture.invalid/v1",
        model="fixture-chat",
        transport=httpx.MockTransport(chat_handler),
    )
    embedding = RecordedEmbeddingClient(
        EmbeddingConfig(
            model="fixture-embedding-v1",
            base_url="https://fixture.invalid/v1",
            dimensions=3,
            send_dimensions=True,
            max_batch=8,
            response_archive_directory=str(tmp_path),
            retry=retry,
        ),
        api_key="fixture",
        transport=httpx.MockTransport(embedding_handler),
    )
    indexer = Indexer(store, embedding)
    extractor = Extractor(client, retry=retry, max_tokens=1500)
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    context = CallContext(store, trace, 1, version)
    pipeline = MemoryPipeline(store, extractor, indexer, code_version=version)
    retriever = Retriever(
        store, indexer, RetrievalConfig.model_validate(CONFIG["retrieval"])
    )
    yield SimpleNamespace(
        store=store,
        trace=trace,
        context=context,
        pipeline=pipeline,
        indexer=indexer,
        retriever=retriever,
        responses=responses,
        prompts=prompts,
        embedded=embedded,
        vectors=vectors,
    )
    await client.close()
    await embedding.close()
    await store.close()
