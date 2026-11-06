"""One read-only SQLite statement against the episode's fixed database."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def run_readonly_sql(
    database: Path, code_query: str, *, timeout: float = 10.0, max_rows: int = 200,
    allowed_tables: frozenset[str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               sqlite3.SQLITE_RECURSIVE}
    pragmas = {"table_info", "table_xinfo", "foreign_key_list", "index_list",
               "index_info", "index_xinfo"}

    def authorize(action, arg1, arg2, _db, _trigger):
        if allowed_tables is not None:
            if action == sqlite3.SQLITE_PRAGMA:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and arg1.casefold() not in allowed_tables:
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_OK if arg1.lower() in pragmas else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {
            "load_extension", "readfile", "writefile"
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=timeout)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(authorize)
        connection.set_progress_handler(lambda: time.monotonic() - started >= timeout, 1000)
        timer = threading.Timer(timeout, connection.interrupt)
        timer.daemon = True
        timer.start()
        try:
            cursor = connection.execute(code_query)
            rows = cursor.fetchmany(max_rows + 1)
            return {"columns": [x[0] for x in cursor.description or []],
                    "rows": [list(row) for row in rows[:max_rows]],
                    "truncated": len(rows) > max_rows,
                    "elapsed_seconds": round(time.monotonic() - started, 4)}
        except sqlite3.Error as exc:
            return {"error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 4)}
        finally:
            timer.cancel()
            timer.join()
    finally:
        connection.close()
