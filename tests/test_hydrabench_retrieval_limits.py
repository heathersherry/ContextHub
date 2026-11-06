import sqlite3
from types import SimpleNamespace

import pytest

from integrations.hydrabench.retrieval_tools import RetrievalEpisode
from integrations.hydrabench.sqlite_tool import run_readonly_sql


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "toy.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE item(id INTEGER); INSERT INTO item VALUES(7); CREATE TABLE secret(id INTEGER); INSERT INTO secret VALUES(8);")
    return path


@pytest.mark.parametrize("sql", [
    "SELECT name FROM sqlite_master", "PRAGMA table_info(item)",
    "SELECT * FROM pragma_table_info('item')", "SELECT * FROM secret",
    "SELECT * FROM item JOIN secret ON 1=1",
])
def test_sql_cannot_bypass_retrieval(database, sql):
    assert "error" in run_readonly_sql(database, sql, allowed_tables=frozenset({"item"}))


def test_retrieved_tables_are_usable(database):
    assert run_readonly_sql(database, "SELECT * FROM item", allowed_tables=frozenset({"item"}))["rows"] == [[7]]
    assert "error" in run_readonly_sql(database, "SELECT * FROM item", allowed_tables=frozenset())


@pytest.mark.asyncio
async def test_cap_budget_and_bound_database(database):
    calls = []
    uri = "ctx://datalake/hydra/toy/item"
    async def search(**kwargs):
        calls.append(kwargs)
        return {"results": [{"uri": "ctx://datalake/hydra/other/secret"}, {"uri": uri}]}
    episode = RetrievalEpisode(None, database, "toy")
    episode.native["contexthub_search"] = SimpleNamespace(coroutine=search)
    tools = {t.name: t for t in episode.tools()}
    for _ in range(10):
        await tools["contexthub_search"].ainvoke({"query": "toy item", "context_type": "table_schema"})
    assert len(calls) == 10 and all(c["top_k"] == 3 for c in calls)
    assert episode.allowed_tables == {"item"}
    assert "error" in await tools["contexthub_search"].ainvoke({"query": "more", "context_type": "table_schema"})
    assert len(calls) == 10
    assert "error" in await tools["contexthub_read"].ainvoke({"uri": "ctx://datalake/hydra/toy/secret"})
    assert (await tools["run_sql"].ainvoke({"code_query": "SELECT * FROM item"}))["rows"] == [[7]]
    assert "error" in await tools["run_sql"].ainvoke({"code_query": "SELECT * FROM secret"})
