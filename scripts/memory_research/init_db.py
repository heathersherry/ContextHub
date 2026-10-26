"""Initialize the memory schema in an already-created PostgreSQL database."""

import argparse
import asyncio
import json
import os
from contexthub.memory.index_cli import add_embedding_index_arguments, resolve_index_config
from contexthub.memory.storage import MemoryStore


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    add_embedding_index_arguments(parser)
    args = parser.parse_args()
    config = resolve_index_config(json.load(open(args.config)), args)
    store = await MemoryStore.open(
        os.environ["MEMORY_DATABASE_URL"], config, initialize=True
    )
    async with store.pool.acquire() as conn:
        print(
            json.dumps(
                {
                    "database": await conn.fetchval("SELECT current_database()"),
                    "implementation": "contexthub.memory",
                    "index": config.model_dump(),
                }
            )
        )
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
