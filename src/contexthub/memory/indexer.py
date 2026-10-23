from .schemas import normalize_topics, vector_literal
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

    async def index_topics(self, topics, context):
        """Batch only unseen labels; queries and documents use the same prefix."""
        topics = normalize_topics(topics)
        if not topics:
            return
        async with self.store.pool.acquire() as conn:
            existing = await conn.fetch(
                "SELECT topic FROM topic_embeddings WHERE topic=ANY($1::text[])", topics,
            )
        missing = sorted(set(topics) - {row["topic"] for row in existing})
        if not missing:
            return
        batch_size = self.client.config.max_batch
        for offset in range(0, len(missing), batch_size):
            batch = missing[offset:offset + batch_size]
            vectors = await self._embed(
                [self.store.config.document_prefix + topic for topic in batch],
                context, "topic_embedding",
            )
            rows = [
                (topic, vector_literal(vector, self.store.config.dimensions))
                for topic, vector in zip(batch, vectors, strict=True)
            ]
            async with self.store.pool.acquire() as conn, conn.transaction():
                await conn.executemany(
                    "INSERT INTO topic_embeddings VALUES($1,$2::vector) ON CONFLICT(topic) DO NOTHING",
                    rows,
                )
                await self.store.record(context, "topics.indexed", {"topics": batch}, conn=conn)

    async def index(self, refs, context):
        config = self.store.config
        for memory_id, version in refs:
            async with self.store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT text,topics,search_vector,memory_embedding FROM memories WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                    *context.trace.args,
                    memory_id,
                    version,
                )
            if row is None:
                raise ValueError("memory version missing")
            await self.index_topics(row["topics"], context)
            if row["search_vector"] is not None and row["memory_embedding"] is not None:
                continue
            await self.store.record(
                context,
                "index.input",
                {
                    "ref": f"{memory_id}:{version}",
                    "text": row["text"],
                    "config": config.model_dump(),
                },
            )
            (vector,) = await self._embed(
                [config.document_prefix + row["text"]], context, "indexing"
            )
            async with self.store.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    f"""UPDATE memories SET search_vector=to_tsvector($6::regconfig,text),
                    memory_embedding=$7::vector WHERE {SCOPE} AND memory_id=$4 AND version=$5""",
                    *context.trace.args,
                    memory_id,
                    version,
                    config.text_config,
                    vector_literal(vector, config.dimensions),
                )

                await self.store.record(
                    context,
                    "index.saved",
                    {
                        "ref": f"{memory_id}:{version}",
                        "column": "memories.memory_embedding",
                    },
                    conn=conn,
                )

    async def query_vector(self, query, context):
        (vector,) = await self._embed(
            [self.store.config.query_prefix + query], context, "query_embedding"
        )
        return vector_literal(vector, self.store.config.dimensions)
