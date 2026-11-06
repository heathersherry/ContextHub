import sqlite3

import pytest

from integrations.hydrabench.sqlite_tool import run_readonly_sql


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "sample.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE item (id INTEGER, name TEXT); INSERT INTO item VALUES (1, 'a'), (2, 'b');")
    return path


def test_query_schema_and_truncation(database):
    assert run_readonly_sql(database, "WITH x AS (SELECT * FROM item) SELECT name FROM x ORDER BY id")["rows"] == [["a"], ["b"]]
    assert run_readonly_sql(database, "PRAGMA table_info(item)")["rows"][0][1] == "id"
    assert run_readonly_sql(database, "SELECT name FROM sqlite_master WHERE type='table'")["rows"] == [["item"]]
    result = run_readonly_sql(database, "SELECT * FROM item", max_rows=1)
    assert result["truncated"] and len(result["rows"]) == 1


@pytest.mark.parametrize("sql", [
    "DELETE FROM item", "DROP TABLE item", "ATTACH DATABASE ':memory:' AS extra",
    "PRAGMA query_only=OFF", "PRAGMA user_version=8", "SELECT 1; DELETE FROM item",
    "SELECT load_extension('nope')",
])
def test_rejects_writes_and_escapes(database, sql):
    before = database.read_bytes()
    assert "error" in run_readonly_sql(database, sql)
    assert database.read_bytes() == before


def test_query_timeout(database):
    result = run_readonly_sql(database, "WITH RECURSIVE x(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM x) SELECT sum(n) FROM x", timeout=0.02)
    assert "interrupted" in result["error"]
    assert result["elapsed_seconds"] < 1
