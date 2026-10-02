"""Initialize an already-created, empty dedicated research database."""

import argparse
import asyncio
import json
import os
from contexthub.memory.schemas import IndexConfig
from contexthub.memory.storage import MemoryStore


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = IndexConfig.model_validate(json.load(open(args.config))["index"])
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
