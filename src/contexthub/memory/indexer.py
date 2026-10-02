from .schemas import vector_literal
from .storage import SCOPE


class Indexer:
    def __init__(self, store, embedding_client, *, price=None):
        self.store, self.client, self.price = store, embedding_client, price
        if (
            embedding_client.config.model != store.config.embedding_model
            or embedding_client.config.dimensions != store.config.dimensions
        ):
            raise ValueError("embedding client does not match database configuration")

    async def _embed(self, texts, context, stage):
        call = context.make(
            stage, self.client.config, self.client.config.retry, price=self.price
        )
        return await self.client.embed_batch(tuple(texts), call=call)

    async def index(self, refs, context):
        config = self.store.config
        for memory_id, version in refs:
            async with self.store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT text,search_vector,memory_embedding FROM memories WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                    *context.trace.args,
                    memory_id,
                    version,
                )
            if row is None:
                raise ValueError("memory version missing")
            if row["search_vector"] is not None and row["memory_embedding"] is not None:
                continue
            (vector,) = await self._embed(
                [config.document_prefix + row["text"]], context, "indexing"
            )
            async with self.store.pool.acquire() as conn:
                await conn.execute(
                    f"""UPDATE memories SET search_vector=to_tsvector($6::regconfig,text),
                    memory_embedding=$7::vector WHERE {SCOPE} AND memory_id=$4 AND version=$5""",
                    *context.trace.args,
                    memory_id,
                    version,
                    config.text_config,
                    vector_literal(vector, config.dimensions),
                )

    async def query_vector(self, query, context):
        (vector,) = await self._embed(
            [self.store.config.query_prefix + query], context, "query_embedding"
        )
        return vector_literal(vector, self.store.config.dimensions)
