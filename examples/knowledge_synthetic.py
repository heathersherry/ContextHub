"""Run only against an explicitly supplied, already migrated local fixture DB.

PYTHONPATH=src:sdk/src:. python -m examples.knowledge_synthetic --dsn ...
No external HTTP transport is installed. No schema creation or destructive cleanup.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import httpx
from contexthub.config import Settings
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.main import ApplicationDependencies, create_app
from contexthub_sdk import (
    ContextHubClient,
    KnowledgeIngestRequest,
    KnowledgeMaintainRequest,
)
from examples.knowledge_fixture import configuration, secrets, SyntheticModels


class OfflineChat:
    async def complete(self, *args, **kwargs):
        raise RuntimeError("legacy chat is not part of this example")

    async def close(self):
        pass


async def run(dsn):
    parsed = urlsplit(dsn)
    if (
        parsed.hostname not in ("localhost", "127.0.0.1")
        or parsed.port in (None, 5432)
        or not parsed.path.startswith("/contexthub_s2_s7")
    ):
        raise ValueError(
            "explicit local S7 fixture database on a non-default port required"
        )
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SHOW server_encoding") == "UTF8"
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "011"
        role = await conn.fetchrow(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
        assert not role["rolsuper"] and not role["rolbypassrls"]
    finally:
        await conn.close()
    models = SyntheticModels()
    with tempfile.TemporaryDirectory(prefix="knowledge-offline-") as directory:
        config = Path(directory) / "fixture.json"
        config.write_text(configuration().model_dump_json())
        app = create_app(
            Settings(
                _env_file=None,
                database_url=dsn,
                api_key="fixture-local",
                knowledge_enabled=True,
                knowledge_config_path=str(config),
                lifecycle_enabled=False,
                propagation_enabled=False,
            ),
            dependencies=ApplicationDependencies(
                embedding_factory=lambda s: NoOpEmbeddingClient(),
                chat_factory=lambda s: OfflineChat(),
                knowledge_transport=models.transport,
                knowledge_secrets=secrets(),
            ),
        )
        async with app.router.lifespan_context(app):
            async with ContextHubClient(
                url="http://local",
                api_key="fixture-local",
                account_id="example-" + str(uuid4()),
                agent_id="reader",
                transport=httpx.ASGITransport(app=app),
            ) as client:
                created = await client.knowledge.ingest(
                    KnowledgeIngestRequest(
                        request_id=uuid4(),
                        content="A: allowance 500|B: uses A for reimbursement|C: uses B in report",
                    )
                )
                assert created.maintenance_ready, created
                await client.context.update(
                    created.source.uri,
                    expected_version=1,
                    l2_content="A: allowance 400|B: uses A for reimbursement|C: uses B in report",
                )
                state = await client.knowledge.get_maintenance_result(
                    created.operation_id
                )
                result = await client.knowledge.maintain(
                    KnowledgeMaintainRequest(
                        request_id=uuid4(),
                        event_id=state.event_ids[-1],
                        graph_id=created.graph_id,
                    )
                )
                assert all(n.fresh for n in result.nodes), result
                count = len(models.calls)
                print(
                    (
                        await client.knowledge.get_maintenance_result(
                            result.operation_id
                        )
                    ).model_dump_json(indent=2)
                )
                assert len(models.calls) == count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    asyncio.run(run(parser.parse_args().dsn))
