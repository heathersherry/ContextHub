import pytest
from contexthub.main import create_app, ApplicationDependencies
from contexthub.config import Settings
from s7_helpers import configuration, secrets


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "invalid", "credentials", "rules"])
async def test_invalid_enablement_precedes_every_client_and_database(
    tmp_path, monkeypatch, bad
):
    def fail(*a, **k):
        raise AssertionError("external boundary constructed")

    monkeypatch.setattr("contexthub.main.asyncpg.create_pool", fail)
    path = tmp_path / "config.json"
    path.write_text(
        configuration().model_dump_json()
        if bad != "invalid"
        else '{"chat_model":"legacy"}'
    )
    dep = ApplicationDependencies(
        embedding_factory=fail, chat_factory=fail, knowledge_secrets=secrets()
    )
    if bad == "credentials":
        dep.knowledge_secrets.verifier = ""
    if bad == "rules":
        dep.rules = (type("Rule", (), {"id": "unknown", "version": "1"})(),)
    app = create_app(
        Settings(
            _env_file=None,
            knowledge_enabled=True,
            knowledge_config_path=None if bad == "missing" else str(path),
        ),
        dependencies=dep,
    )
    with pytest.raises(RuntimeError, match="^knowledge_configuration_invalid$"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_partial_startup_closes_embedding_and_pool(monkeypatch):
    class Pool:
        closed = False

        async def close(self):
            self.closed = True

    class Embedding:
        closed = False

        async def close(self):
            self.closed = True

    pool, embedding = Pool(), Embedding()

    async def create(**kw):
        return pool

    def broken_chat(s):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("contexthub.main.asyncpg.create_pool", create)
    app = create_app(
        Settings(_env_file=None),
        dependencies=ApplicationDependencies(
            embedding_factory=lambda s: embedding, chat_factory=broken_chat
        ),
    )
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert embedding.closed and pool.closed
