"""Import frozen source passages and SQLite schemas into an empty test account."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path

from contexthub_sdk import ContextHubClient, ContextType, Scope


async def ingest(args):
    shared = dict(url=args.url, api_key=os.environ["CONTEXTHUB_API_KEY"],
                  account_id=args.account, timeout=120)
    records = [json.loads(line) for line in args.passages.read_text().splitlines() if line]
    semaphore = asyncio.Semaphore(4)
    completed = 0
    async with ContextHubClient(**shared, agent_id="query-agent") as client:
        async def add(record):
            nonlocal completed
            async with semaphore:
                body = record["content"]
                created = await client.context.create(
                    uri=record["uri"], context_type=ContextType.RESOURCE,
                    scope=Scope.TEAM, owner_space="engineering",
                    l2_content=body,
                    tags=["hydrabench-smoke", "hydrabench-raw-v1", "kilt-2019"])
                completed += 1
                if completed % 100 == 0:
                    print(f"Imported {completed}/{len(records)} passages", flush=True)
                return {"uri": created.uri, "id": str(created.id)}
        imported = await asyncio.gather(*(add(row) for row in records))
    async with ContextHubClient(**shared, agent_id="catalog_sync") as client:
        with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            schemas = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for name, ddl in schemas:
            descriptor = args.database.parent / "database_description" / f"{name}.csv"
            body = f"Database: {args.db_id}\nTable: {name}\n{ddl}\n"
            if descriptor.exists():
                body += "\nOriginal BIRD column descriptions:\n" + descriptor.read_text()
            uri = f"ctx://datalake/hydra/{args.db_id}/{name}"
            created = await client.context.create(
                uri=uri, context_type=ContextType.TABLE_SCHEMA, scope=Scope.DATALAKE,
                l2_content=body,
                tags=["hydrabench-smoke", "hydrabench-raw-v1", args.db_id])
            imported.append({"uri": created.uri, "id": str(created.id)})
    args.output.write_text(json.dumps({"passages": len(records), "tables": len(schemas),
                                      "contexts": imported}, indent=2))
    print(json.dumps({"passages": len(records), "tables": len(schemas)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18082")
    parser.add_argument("--account", default="acme")
    parser.add_argument("--db-id", default="toxicology")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--passages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(ingest(parser.parse_args()))


if __name__ == "__main__":
    main()
