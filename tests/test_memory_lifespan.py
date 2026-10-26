import pytest
from contexthub.main import create_app, ApplicationDependencies
from contexthub.config import Settings

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "invalid", "credentials"])
async def test_invalid_enablement_precedes_clients_and_database(tmp_path, monkeypatch, bad):
    def fail(*a, **k):
        raise AssertionError("external boundary constructed")
    monkeypatch.setattr("contexthub.main.asyncpg.create_pool", fail)
    path = tmp_path / "config.json"
    if bad == "credentials":
        from contexthub.memory.factory import MemoryConfig
        monkeypatch.setattr(MemoryConfig, "load", lambda path: object())
        monkeypatch.delenv("MEMORY_CHAT_API_KEY", raising=False)
        monkeypatch.delenv("MEMORY_EMBEDDING_API_KEY", raising=False)
    path.write_text('{"chat_model":"incomplete"}')
    app = create_app(Settings(_env_file=None, memory_enabled=True,
                              memory_config_path=None if bad == "missing" else str(path)),
                     dependencies=ApplicationDependencies(embedding_factory=fail, chat_factory=fail))
    with pytest.raises(RuntimeError, match="^memory_configuration_invalid$"):
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
