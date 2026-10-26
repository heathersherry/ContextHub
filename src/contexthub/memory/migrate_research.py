"""Explicit, transactional upgrade of the former dedicated research database.

Run once, with all writers stopped:
    MEMORY_DATABASE_URL=... python -m contexthub.memory.migrate_research
No model requests; IDs, versions, evidence and recorded payloads are preserved.
"""

import asyncio
import os

import asyncpg


TABLES = (
    "session_records",
    "memories",
    "memory_sources",
    "topic_embeddings",
    "execution_attempts",
    "memory_steps",
)


async def migrate(dsn):
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            if await conn.fetchval("SELECT to_regnamespace('contexthub_memory')"):
                raise ValueError("destination memory schema already exists")
            if not await conn.fetchval("SELECT to_regclass('public.research_config')"):
                raise ValueError("not a legacy research database")
            if await conn.fetchval("SELECT to_regclass('public.contexts')"):
                raise ValueError("expected a dedicated research database")
            # Fixed identifiers only. Locks make the rename atomic; stop clients
            # beforehand because old processes cannot use the upgraded schema.
            await conn.execute(
                "LOCK TABLE "
                + ",".join("public." + t for t in ("research_config", *TABLES))
                + " IN ACCESS EXCLUSIVE MODE"
            )
            await conn.execute("CREATE SCHEMA contexthub_memory")
            for table in ("research_config", *TABLES):
                await conn.execute(
                    f"ALTER TABLE public.{table} SET SCHEMA contexthub_memory"
                )
            await conn.execute(
                "ALTER TABLE contexthub_memory.research_config RENAME TO memory_config"
            )
            for table in TABLES:
                if table == "topic_embeddings":
                    continue
                for old, new in (
                    ("run_id", "tenant_id"),
                    ("episode_id", "namespace"),
                    ("strategy_id", "owner_id"),
                ):
                    await conn.execute(
                        f"ALTER TABLE contexthub_memory.{table} RENAME COLUMN {old} TO {new}"
                    )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate(os.environ["MEMORY_DATABASE_URL"]))
