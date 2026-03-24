from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

import asyncpg


class ScopedRepo:
    """Request-scoped 数据库执行器。所有 SQL 都必须通过它执行。"""

    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        return await self._conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        return await self._conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._conn.execute(sql, *args)


class PgRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @asynccontextmanager
    async def session(self, account_id: str) -> AsyncIterator[ScopedRepo]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # asyncpg does not support bind parameters in SET statements.
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)",
                    account_id,
                )
                yield ScopedRepo(conn)
