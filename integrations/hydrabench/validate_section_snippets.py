"""Replay the single existing Microarchitecture query without model calls.

Read the saved before ToolMessage and current stored source/section ranges. Run
one SDK/DeepAgents search against the same sample database; roll back all search
bookkeeping writes. Do not ingest, embed, rebuild, or add corpus/query cases.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from fastapi import FastAPI
from contexthub_sdk import ContextHubClient
from contexthub_deepagents import create_contexthub_tools

from contexthub.api.routers import search
from contexthub.db.repository import ScopedRepo
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.retrieval.long_doc import KeywordRetriever, LongDocRetrievalCoordinator
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "docs/hydrabench/artifacts/20261105-document-layers-qwen38-27b/validation.json"
URI = "ctx://resources/hydra-layer-validation/microarchitecture"
QUERY = "1986 VAX 8800"
DATABASE = "hydra_layers_sample_20261105_qwen38_27b"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def size(payload: dict) -> dict:
    characters = len(json.dumps(payload, ensure_ascii=False))
    return {"characters": characters, "estimated_tokens_chars_div_4": characters / 4}


class RollbackRepository:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def session(self, account_id):
        async with self.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute("SELECT set_config('app.account_id', $1, true)", account_id)
                yield ScopedRepo(conn)
            finally:
                await tx.rollback()


async def snapshot(db):
    # Includes vectors, section metadata, counters and retrieval trace rows.
    result = {}
    for table in ("contexts", "document_sections", "retrieval_trace"):
        rows = await db.fetch(f"SELECT to_jsonb(t)::text AS data FROM {table} t")
        result[table] = {"rows": len(rows), "sha256": digest(
            "\n".join(sorted(row["data"] for row in rows)).encode())}
    return result


async def validate(output: Path):
    output.mkdir(parents=True, exist_ok=False)
    baseline_bytes = BASELINE.read_bytes()
    previous = next(item for item in json.loads(baseline_bytes)["samples"] if item["kind"] == "long_document")
    assert previous["query"] == QUERY
    before = previous["agent_input_after"]
    before_hit = before["results"][0]
    assert before_hit["uri"] == URI
    pool = await asyncpg.create_pool(os.environ["HYDRA_SAMPLE_DATABASE_URL"], min_size=1, max_size=2)
    repo = RollbackRepository(pool)
    client = None
    try:
        async with repo.session("acme") as db:
            assert await db.fetchval("SELECT current_database()") == DATABASE
            state_before = await snapshot(db)
            document = dict(await db.fetchrow("SELECT id,uri,version,file_path FROM contexts WHERE uri=$1", URI))
            section = dict(await db.fetchrow("""SELECT section_id,title,start_offset,end_offset
                FROM document_sections WHERE context_id=$1 AND title='Instruction pipelining.'""", document["id"]))
        source_file = Path(document["file_path"]) / "extracted.txt"
        source_bytes = source_file.read_bytes()
        source = source_bytes.decode()
        assert digest(source_bytes) == previous["source_sha256"]
        assert source.index(before_hit["content"]) == 10291
        assert document["version"] == before_hit["version"] == 1

        coordinator = LongDocRetrievalCoordinator()
        coordinator.register_strategy("keyword", KeywordRetriever())
        service = RetrievalService(RetrievalRouter.default(), NoOpEmbeddingClient(), ACLService(),
                                   masking_service=MaskingService(), long_doc_coordinator=coordinator)
        app = FastAPI()
        app.include_router(search.router)
        app.state.repo, app.state.retrieval_service = repo, service
        client = ContextHubClient("http://sample.test", "local", "acme", "query-agent")
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sample.test",
                                        headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"})
        tool = next(tool for tool in create_contexthub_tools(client) if tool.name == "contexthub_search")
        message = await tool.ainvoke({"type": "tool_call", "id": "section-boundary-search",
            "name": "contexthub_search", "args": {
                "query": QUERY, "context_type": ["resource"], "top_k": 1, "level": "L1"}})
        assert message.status == "success"
        after = json.loads(message.content)
        hit = after["results"][0]
        start, end = section["start_offset"], section["end_offset"]
        assert len(after["results"]) == 1
        assert hit["uri"] == URI and hit["version"] == 1
        assert hit["section_id"] == section["section_id"] == 10
        assert hit["content_level"] == "snippet" and hit["retrieval_strategy"] == "keyword"
        assert hit["content"] == source[start:end]
        assert hit["content"].startswith("### Instruction pipelining.\n")
        assert "### Cache." not in hit["content"] and "### Branch prediction." not in hit["content"]
        assert all(term in hit["content"] for term in QUERY.split())
        assert not {"l0_content", "l1_content", "l2_content", "snippet"} & hit.keys()
        async with repo.session("acme") as db:
            state_after = await snapshot(db)
        assert state_after == state_before
        assert BASELINE.read_bytes() == baseline_bytes
        assert source_file.read_bytes() == source_bytes

        for label, payload in (("before", before), ("after", after)):
            (output / f"agent-{label}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            (output / f"content-{label}.txt").write_text(payload["results"][0]["content"])
        report = {
            "time_hong_kong": datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(),
            "database": DATABASE, "uri": URI, "version": 1, "query": QUERY,
            "scope": "One existing query; prior saved ToolMessage versus current SDK/DeepAgents ToolMessage. No answering model invoked.",
            "provider_model_calls": 0, "provider_embedding_calls": 0, "new_actual_model_usage": None,
            "baseline_sha256": digest(baseline_bytes), "source_sha256": digest(source_bytes),
            "before": {"section_id": before_hit["section_id"], "offsets": [10291, 13636],
                       "content_characters": len(before_hit["content"]), "payload_size": size(before)},
            "after": {"section_id": hit["section_id"], "offsets": [start, end],
                      "content_characters": len(hit["content"]), "payload_size": size(after)},
            "state_before": state_before, "state_after": state_after,
            "note": "All search counter/trace writes rolled back. Contexts (including vectors), sections and source unchanged. Character/4 is a heuristic, not actual model usage or cost.",
        }
        (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"query": QUERY, "before": report["before"], "after": report["after"],
                          "provider_calls": 0, "database_unchanged": True}, ensure_ascii=False))
    finally:
        if client:
            await client.aclose()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(validate(parser.parse_args().output))
