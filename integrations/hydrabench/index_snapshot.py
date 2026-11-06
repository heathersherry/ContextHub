"""Build an isolated retrieval fixture with batched configured embeddings.

This imports frozen evaluation data through PgRepository with tenant RLS. It is
not an ingestion API benchmark. Search still uses the unmodified ContextHub API.
"""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from contexthub.config import Settings
from contexthub.db.pool import create_pool
from contexthub.db.repository import PgRepository
from contexthub.llm.openai_client import OpenAIEmbeddingClient

STORAGE_POLICY = "hydrabench-raw-v1"


def schema_records(database_root):
    records = []
    for path in sorted(database_root.glob("*/*.sqlite")):
        db_id = path.parent.name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            tables = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for name, ddl in tables:
            body = f"Database: {db_id}\nTable: {name}\n{ddl}\n"
            descriptor = path.parent / "database_description" / f"{name}.csv"
            encoding = None
            if descriptor.exists():
                raw = descriptor.read_bytes()
                try:
                    description = raw.decode("utf-8-sig")
                    encoding = "utf-8-sig"
                except UnicodeDecodeError:
                    # Four BIRD CSVs in this release use Windows punctuation bytes.
                    description = raw.decode("cp1252")
                    encoding = "cp1252"
                body += "\nOriginal BIRD column descriptions:\n" + description
            records.append({"uri": f"ctx://datalake/hydra/{db_id}/{name}",
                            "context_type": "table_schema", "scope": "datalake",
                            "owner_space": None, "content": body, "db_id": db_id,
                            "table": name, "database_sha256": digest,
                            "descriptor_encoding": encoding})
    return records


async def load(args):
    settings = Settings()
    records = [{**json.loads(line), "context_type": "resource", "scope": "team",
                "owner_space": "engineering"} for line in args.passages.read_text().splitlines()]
    records += schema_records(args.database_root)
    # Keep exact passages and complete DDL/column descriptions as L2 originals.
    # A table schema is one catalog unit, never sentence-chunked or summarized.
    records = [{**record, "storage_policy": STORAGE_POLICY} for record in records]
    snapshot = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    args.output.mkdir(parents=True, exist_ok=True)
    snapshot_path = args.output / "index-records.jsonl"
    if snapshot_path.exists() and snapshot_path.read_text() != snapshot:
        raise ValueError("Snapshot contents changed; use a fresh experiment database/output.")
    snapshot_path.write_text(snapshot)
    pool = await create_pool(settings)
    repo = PgRepository(pool)
    client = OpenAIEmbeddingClient(settings.openai_api_key, settings.openai_base_url,
                                   settings.embedding_model, settings.embedding_dimensions,
                                   settings.embedding_dimensions, timeout=60)
    started = time.monotonic()
    try:
        async with repo.session("acme") as db:
            existing = {r["uri"]: dict(r) for r in await db.fetch(
                "SELECT uri,l0_content,l1_content,l2_content,tags FROM contexts")}
        expected = {r["uri"]: r["content"] for r in records}
        if any(
            expected.get(uri) != row["l2_content"]
            or row["l0_content"] is not None or row["l1_content"] is not None
            or STORAGE_POLICY not in row["tags"]
            for uri, row in existing.items()
        ):
            raise ValueError("Existing index does not match the frozen snapshot.")
        pending = [r for r in records if r["uri"] not in existing]
        completed = len(existing)
        semaphore = asyncio.Semaphore(4)

        async def batch(group):
            nonlocal completed
            async with semaphore:
                vectors = await client.embed_batch([r["content"] for r in group])
                if any(v is None for v in vectors):
                    raise RuntimeError("Embedding batch failed; incomplete index must not be evaluated.")
                async with repo.session("acme") as db:
                    for record, vector in zip(group, vectors, strict=True):
                        await db.execute(
                            """INSERT INTO contexts
                            (uri,context_type,scope,owner_space,account_id,l0_content,l1_content,l2_content,tags,l0_embedding)
                            VALUES ($1,$2,$3,$4,'acme',NULL,NULL,$5,$6,$7::vector)""",
                            record["uri"], record["context_type"], record["scope"],
                            record["owner_space"], record["content"], ["hydrabench-expanded", STORAGE_POLICY],
                            json.dumps(vector),
                        )
                completed += len(group)
                if completed % 200 == 0 or completed == len(records):
                    print(f"Indexed {completed}/{len(records)} ({time.monotonic()-started:.1f}s)", flush=True)

        await asyncio.gather(*(batch(pending[i:i + 10]) for i in range(0, len(pending), 10)))
        async with repo.session("acme") as db:
            counts = [dict(r) for r in await db.fetch("SELECT context_type,count(*) AS count,count(l0_embedding) AS embedded FROM contexts GROUP BY context_type")]
        assert sum(r["count"] for r in counts) == len(records)
        assert all(r["count"] == r["embedded"] for r in counts)
        manifest = {"snapshot_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
                    "storage_policy": STORAGE_POLICY,
                    "embedding_input": "Exact source content, unchanged by storage placement",
                    "counts": counts, "embedding_model": settings.embedding_model,
                    "embedding_dimensions": settings.embedding_dimensions,
                    "rerank_strategy": settings.rerank_strategy,
                    "indexing_seconds": round(time.monotonic() - started, 3),
                    "import": "Frozen fixture, configured batch embedding client, tenant-scoped PgRepository; no retrieval changes"}
        (args.output / "index-manifest.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps(manifest), flush=True)
    finally:
        await client.close()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--passages", type=Path, required=True)
    parser.add_argument("--database-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(load(parser.parse_args()))
