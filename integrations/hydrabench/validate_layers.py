"""Validate three real source units in an empty, isolated PostgreSQL database.

No answering/judge model or embedding provider is called. Existing passage and
schema vectors are replayed only for identical text; vector self-queries check
plumbing, not relevance. The long document uses deterministic sectioning and
keyword discovery. Output is new evidence, never an overwritten benchmark run.
"""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from fastapi import FastAPI
from contexthub_sdk import ContextHubClient
from contexthub_deepagents import create_contexthub_tools

from contexthub.api.routers import search, tools, documents
from contexthub.db.repository import PgRepository
from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.context import CreateContextRequest, UpdateContextRequest
from contexthub.models.request import RequestContext
from contexthub.retrieval.long_doc.coordinator import LongDocRetrievalCoordinator
from contexthub.retrieval.long_doc.tree_retriever import TreeRetriever
from contexthub.retrieval.router import RetrievalRouter
from contexthub.retrieval.vector_strategy import vector_search
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.context_service import ContextService
from contexthub.services.document_ingester import LongDocumentIngester
from contexthub.services.indexer_service import IndexerService
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService
from contexthub.store.context_store import ContextStore


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def measure(payload):
    text = json.dumps(payload, ensure_ascii=False)
    return {"characters": len(text), "estimated_tokens_chars_div_4": round(len(text) / 4, 2)}


class ReplayEmbedding:
    def __init__(self, vectors):
        self.vectors = vectors
        self.inputs = []

    async def embed(self, text):
        self.inputs.append(digest(text))
        return self.vectors[text]  # Reject any text without an identical stored input.


class OfflineChat:
    async def complete(self, prompt, max_tokens=2000):
        return ""  # Exercise local tree selection; no generated model summaries.


async def fingerprint(db):
    return dict(await db.fetchrow("""
        SELECT count(*) AS contexts, count(l0_embedding) AS embedded,
               md5(string_agg(md5(concat_ws('|', uri, version::text,
                   l0_content, l1_content, l2_content)), '' ORDER BY uri)) AS content_fingerprint
        FROM contexts
    """))


async def validate(args):
    args.output.mkdir(parents=True, exist_ok=False)
    source_pool = await asyncpg.create_pool(os.environ["HYDRA_SOURCE_DATABASE_URL"], min_size=1, max_size=2,
                                           server_settings={"default_transaction_read_only": "on"})
    sample_pool = await asyncpg.create_pool(os.environ["HYDRA_SAMPLE_DATABASE_URL"], min_size=1, max_size=2)
    source_repo, repo = PgRepository(source_pool), PgRepository(sample_pool)
    client = None
    try:
        async with repo.session("acme") as db:
            database = await db.fetchval("SELECT current_database()")
            assert database.startswith("hydra_layers_sample_"), "Use a dedicated sample database."
            assert await db.fetchval("SELECT count(*) FROM contexts") == 0
            # LongDocumentIngester writes organization resources. Grant its test
            # author the root-team write membership only in this isolated DB.
            await db.execute("""INSERT INTO team_memberships (agent_id,team_id,access)
                SELECT 'query-agent',id,'read_write' FROM teams WHERE path=''
                ON CONFLICT (agent_id,team_id) DO NOTHING""")
        passages = [json.loads(line) for line in (args.corpus / "passages.jsonl").read_text().splitlines()]
        passage = next(row for row in passages if row["uri"].endswith("/35872624/0006"))
        uris = [passage["uri"], "ctx://datalake/hydra/formula_1/constructors"]
        async with source_repo.session("acme") as db:
            original_before = await fingerprint(db)
            old_rows = [dict(row) for row in await db.fetch("""
                SELECT uri,context_type,scope,owner_space,l0_content,l1_content,
                       l2_content,version,tags,l0_embedding::text AS vector
                FROM contexts WHERE uri = ANY($1) ORDER BY uri
            """, uris)]
        assert len(old_rows) == 2
        assert all(row["l0_content"] == row["l1_content"] == row["l2_content"] for row in old_rows)
        embedding = ReplayEmbedding({r["l0_content"]: json.loads(r["vector"]) for r in old_rows})
        acl, masking, audit = ACLService(), MaskingService(), AuditService()
        indexer = IndexerService(ContentGenerator(), embedding, embedding_dimensions=1536)
        lifecycle = LifecycleService(audit=audit, indexer=indexer)
        store = ContextStore(acl, masking, audit=audit, lifecycle=lifecycle)
        context_svc = ContextService(store, acl, indexer, audit=audit)
        coordinator = LongDocRetrievalCoordinator()
        coordinator.register_strategy("tree", TreeRetriever(OfflineChat()))
        retrieval = RetrievalService(RetrievalRouter.default(), NoOpEmbeddingClient(), acl,
                                     masking_service=masking, long_doc_coordinator=coordinator)
        app = FastAPI()
        for router in (search.router, tools.router, documents.router):
            app.include_router(router)
        for name, value in dict(repo=repo, retrieval_service=retrieval, context_store=store,
                                acl_service=acl, masking_service=masking, audit_service=audit,
                                skill_service=None, lifecycle_service=lifecycle).items():
            setattr(app.state, name, value)
        client = ContextHubClient("http://sample.test", "local", "acme", "query-agent")
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sample.test",
                                       headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"})
        native = {tool.name: tool for tool in create_contexthub_tools(client)}

        async def tool_message(query, kind):
            message = await native["contexthub_search"].ainvoke({
                "type": "tool_call", "id": "sample-search", "name": "contexthub_search",
                "args": {"query": query, "context_type": [kind], "top_k": 1, "level": "L1"},
            })
            assert message.status == "success"
            return json.loads(message.content)

        samples = []
        for old in old_rows:
            query = "constructors nationality" if old["context_type"] == "table_schema" else "Orion MMSEV 2 weeks"
            actor = "catalog_sync" if old["context_type"] == "table_schema" else "query-agent"
            ctx = RequestContext(account_id="acme", agent_id=actor, expected_version=1)
            async with repo.session("acme") as db:
                created = await context_svc.create(db, CreateContextRequest(**old), ctx)
            before = (await client.search(query, top_k=1)).model_dump(mode="json")
            # The old SDK omitted validity_status. This is the previous adapter's
            # exact body-field projection, not a historical model invocation.
            for hit in before["results"]:
                hit.pop("validity_status", None)
            assert before["results"][0]["uri"] == old["uri"]
            async with repo.session("acme") as db:
                changed = await context_svc.update(db, old["uri"], UpdateContextRequest(
                    l0_content=None, l1_content=None, l2_content=old["l2_content"]), ctx)
                saved = dict(await db.fetchrow("SELECT uri,l0_content,l1_content,l2_content,version FROM contexts WHERE id=$1", created.id))
                vector_hits = await vector_search(db, json.loads(old["vector"]), 1)
                assert vector_hits[0]["uri"] == old["uri"]
                assert vector_hits[0]["snippet"] == old["l2_content"]
            after = await tool_message(query, old["context_type"])
            hit = after["results"][0]
            assert hit["uri"] == old["uri"] and hit["version"] == changed.version == 2
            assert hit["content"] == old["l2_content"]
            assert saved["l0_content"] is None and saved["l1_content"] is None
            read = await native["contexthub_read"].ainvoke({"uri": old["uri"], "level": "L2"})
            assert read["content"] == old["l2_content"]
            samples.append({"kind": old["context_type"], "query": query,
                            "storage_before": {k: v for k, v in old.items() if k != "vector"},
                            "storage_after": saved, "agent_input_before": before, "agent_input_after": after,
                            "before_size": measure(before), "after_size": measure(after),
                            "raw_read_sha256": digest(read["content"]),
                            "vector_check": "Exact stored-text self-query; not a semantic retrieval-quality evaluation"})

        # A complete source article, independent of benchmark questions/labels.
        articles = [json.loads(line) for line in (args.corpus / "articles.jsonl").read_text().splitlines()]
        article = next(row for row in articles if row["wikipedia_title"] == "Microarchitecture")
        lines = ["# Microarchitecture", article["source_url"], "Wikipedia ID: " + article["wikipedia_id"]]
        for paragraph in article["text"][1:]:
            if paragraph.startswith("Section::::"):
                path = paragraph.removeprefix("Section::::").split(":")
                lines.append("#" * (len(path) + 1) + " " + path[-1])
            else:
                lines.append(paragraph)
        source = "\n\n".join(lines)
        article_path = args.output / "microarchitecture.md"
        article_path.write_text(source)
        uri = "ctx://resources/hydra-layer-validation/microarchitecture"
        ingester = LongDocumentIngester(OfflineChat(), NoOpEmbeddingClient(),
                                        acl, audit, doc_store_root=str(args.output / "documents"))
        async with repo.session("acme") as db:
            document = await ingester.ingest(db, uri, str(article_path), RequestContext(account_id="acme", agent_id="query-agent"),
                                              tags=["kilt-2019", "source-sha256:" + digest(source)], allow_llm_tree=False)
            saved = dict(await db.fetchrow("SELECT uri,l0_content,l1_content,l2_content,file_path,version,tags FROM contexts WHERE id=$1", document.context_id))
        assert saved["l2_content"] is None
        assert (Path(saved["file_path"]) / "extracted.txt").read_text() == source
        query = "Microarchitecture pipelining"
        before = (await client.search(query, top_k=1)).model_dump(mode="json")
        after = await tool_message(query, "resource")
        hit = after["results"][0]
        assert hit["uri"] == uri and hit["content_level"] == "snippet"
        assert hit["content"] in source
        section = await native["contexthub_read_section"].ainvoke({"context_id": str(document.context_id), "section_id": hit["section_id"]})
        assert section["content"] == source[section["start_offset"]:section["end_offset"]]
        raw = await native["contexthub_read"].ainvoke({"uri": uri, "level": "L2"})
        assert raw["content"] == source
        sections = await client.document.sections(str(document.context_id))
        assert all(0 <= s.start_offset < s.end_offset <= len(source) for s in sections)
        samples.append({"kind": "long_document", "query": query, "storage_before": saved, "storage_after": saved,
                        "agent_input_before": before, "agent_input_after": after, "section_read": section,
                        "before_size": measure(before), "after_size": measure(after),
                        "source_characters": len(source), "source_sha256": digest(source),
                        "section_count": len(sections), "sections": [s.model_dump(mode="json") for s in sections],
                        "limitation": "Product-path storage unchanged; deterministic heading sections and local tree selection, no model-quality claim or article embedding."})

        # Backfill must also discover raw-only rows; no new source input is sent.
        async with repo.session("acme") as db:
            await db.execute("UPDATE contexts SET l0_embedding=NULL WHERE uri=ANY($1)", uris)
            # Restrict backfill eligibility to the two replayable samples by
            # temporarily archiving the unembedded document in this test only.
            await db.execute("UPDATE contexts SET status='archived' WHERE id=$1", document.context_id)
            assert await indexer.backfill_embeddings(db) == 2
            await db.execute("UPDATE contexts SET status='active' WHERE id=$1", document.context_id)
        async with source_repo.session("acme") as db:
            original_after = await fingerprint(db)
        assert original_after == original_before
        report = {"time_hong_kong": datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(),
                  "database": database, "sample_contexts": 3, "provider_model_calls": 0, "provider_embedding_calls": 0,
                  "new_actual_model_usage": None,
                  "token_estimate_method": "Serialized JSON characters / 4; heuristic only, not the model tokenizer or provider usage.",
                  "source_database_before": original_before, "source_database_after": original_after,
                  "replayed_embedding_input_sha256": embedding.inputs, "samples": samples}
        (args.output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print(json.dumps({"database": database, "samples": len(samples), "provider_calls": 0,
                          "sizes": [{"kind": s["kind"], "before": s["before_size"], "after": s["after_size"]} for s in samples]}))
    finally:
        if client:
            await client.aclose()
        await sample_pool.close()
        await source_pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(validate(parser.parse_args()))
