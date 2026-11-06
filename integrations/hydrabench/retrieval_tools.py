"""Enforce the retrieval experiment's limits around the native adapter tools."""

import asyncio
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from contexthub_deepagents import create_contexthub_tools
from contexthub_sdk import ContextLevel, ContextType
from langchain_core.tools import StructuredTool, ToolException

from .sqlite_tool import run_readonly_sql


class RetrievalEpisode:
    def __init__(self, client, database: Path, db_id: str):
        self.native = {tool.name: tool for tool in create_contexthub_tools(client)}
        self.database = database
        self.db_id = db_id
        self.seen_uris: set[str] = set()
        self.allowed_tables: set[str] = set()
        self.search_counts = {"resource": 0, "table_schema": 0}

    def tools(self):
        async def contexthub_search(
            query: str, context_type: Literal["resource", "table_schema"]
        ) -> dict:
            """Search ContextHub for text (resource) or table_schema. Returns top 3.

            Search the full catalog; include the bound database name when searching
            for tables. Maximum 10 calls of each type per question. Returned table
            schemas from the bound database become available for SQL execution.
            """
            if self.search_counts[context_type] >= 10:
                return {"error": f"The 10-call {context_type} search budget is exhausted."}
            self.search_counts[context_type] += 1
            try:
                result = await self.native["contexthub_search"].coroutine(
                    query=query, context_type=[ContextType(context_type)], top_k=3,
                    level=ContextLevel.L1,
                )
            except ToolException as exc:
                return {"error": str(exc)}
            if not isinstance(result, dict):
                return {"error": str(result)}
            for hit in result.get("results", []):
                uri = hit["uri"]
                self.seen_uris.add(uri)
                prefix = f"ctx://datalake/hydra/{self.db_id}/"
                if context_type == "table_schema" and uri.startswith(prefix):
                    self.allowed_tables.add(unquote(uri[len(prefix):]).casefold())
            return result

        async def contexthub_read(uri: str) -> dict:
            """Read full L2 content of a URI already returned by ContextHub search."""
            if uri not in self.seen_uris:
                return {"error": "Search for this source first; guessed URIs are not readable."}
            try:
                result = await self.native["contexthub_read"].coroutine(uri=uri, level=ContextLevel.L2)
            except ToolException as exc:
                return {"error": str(exc)}
            return result if isinstance(result, dict) else {"error": str(result)}

        async def run_sql(code_query: str) -> dict:
            """Execute one read-only statement in the bound SQLite database.

            Only tables returned by ContextHub schema search can be read. Schema
            catalogs and PRAGMA are blocked in this retrieval experiment. On an
            access error, search for the missing table schema before retrying.
            Ten-second timeout; at most 200 rows with an explicit truncation flag.
            """
            return await asyncio.to_thread(
                run_readonly_sql, self.database, code_query,
                allowed_tables=frozenset(self.allowed_tables),
            )

        return [StructuredTool.from_function(coroutine=tool) for tool in
                [contexthub_search, contexthub_read, run_sql]]
