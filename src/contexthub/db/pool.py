import asyncpg

from contexthub.config import Settings


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.asyncpg_database_url,
        min_size=2,
        max_size=10,
    )
