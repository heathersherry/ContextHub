"""PG full text + exact cosine scan, RRF; no P1 identity or P2 policy decisions."""

from dataclasses import dataclass
from datetime import datetime

from .schemas import normalize_entities
from .storage import SCOPE


@dataclass(frozen=True)
class SearchHit:
    memory: dict
    score: float
    channels: tuple[str, ...]


def allowed_cte(purpose):
    if purpose == "answer":
        predicate = "status='fresh' AND type='fact'"
    elif purpose in ("p1", "p2"):
        predicate = "status IN ('fresh','stale','unresolved') AND type IN ('fact','rule','change')"
    else:
        raise ValueError(
            "purpose must be answer, p1 or p2; historical validity is not implemented"
        )
    return f"""WITH latest AS MATERIALIZED (
        SELECT DISTINCT ON(memory_id) * FROM memories
        WHERE {SCOPE} AND created_in_session <= $4
        ORDER BY memory_id,version DESC
    ), allowed AS MATERIALIZED (SELECT * FROM latest WHERE {predicate})"""


def fuse(text_rows, vector_rows, entity_rows, purpose, config):
    candidates = {}
    for channel, rows in (("text", text_rows), ("vector", vector_rows)):
        for rank, row in enumerate(rows, 1):
            key = row["memory_id"], row["version"]
            memory, score, channels = candidates.get(key, (dict(row), 0, []))
            candidates[key] = (
                memory,
                score + 1 / (config.rrf_c + rank),
                channels + [channel],
            )
    # Use dates only if the entire candidate set is reliably comparable.
    dates = {}
    if purpose == "answer":
        try:
            for key, (row, _, _) in candidates.items():
                dates[key] = datetime.fromisoformat(row["timestamp"])
            sorted(dates.values())  # mixed timezone awareness is not comparable
        except (ValueError, TypeError):
            dates = {}
    rows = list(candidates.items())
    rows.sort(key=lambda pair: (str(pair[0][0]), pair[0][1]))
    if purpose == "answer":
        rows.sort(
            key=lambda pair: (
                dates[pair[0]] if dates else pair[1][0]["created_in_session"]
            ),
            reverse=True,
        )
    rows.sort(key=lambda pair: pair[1][1], reverse=True)
    result = [
        SearchHit(row, score, tuple(channels))
        for _, (row, score, channels) in rows[: config.hybrid_limit]
    ]
    positions = {
        (h.memory["memory_id"], h.memory["version"]): i for i, h in enumerate(result)
    }
    for row in entity_rows:
        key = row["memory_id"], row["version"]
        if key in positions:
            i = positions[key]
            h = result[i]
            result[i] = SearchHit(h.memory, h.score, h.channels + ("entity",))
        else:
            positions[key] = len(result)
            result.append(
                SearchHit(
                    dict(row), candidates.get(key, (None, 0, None))[1], ("entity",)
                )
            )
    return result


class Retriever:
    def __init__(self, store, indexer, config):
        self.store, self.indexer, self.config = store, indexer, config

    async def current(self, trace, through_session, *, purpose):
        """Current contents available by cutoff; not a snapshot of past validity."""
        async with self.store.pool.acquire() as conn:
            return await conn.fetch(
                allowed_cte(purpose) + " SELECT * FROM allowed ORDER BY memory_id",
                *trace.args,
                through_session,
            )

    async def search(self, query, context, *, through_session, purpose, entities=()):
        cte = allowed_cte(purpose)
        if entities and purpose != "p1":
            raise ValueError(
                "entity supplementation is enabled only for p1 in this baseline"
            )
        if through_session < 1 or through_session > context.session_seq:
            raise ValueError("invalid retrieval session cutoff")
        vector = await self.indexer.query_vector(query, context)
        args = (*context.trace.args, through_session)
        config = self.config
        async with (
            self.store.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            # All three channels read the same snapshot. Scope/version/status come before LIMIT.
            text_rows = await conn.fetch(
                cte
                + """ SELECT * FROM allowed
                WHERE search_vector @@ plainto_tsquery($5::regconfig,$6)
                ORDER BY ts_rank_cd(search_vector,plainto_tsquery($5::regconfig,$6)) DESC,memory_id
                LIMIT $7""",
                *args,
                self.store.config.text_config,
                query,
                config.text_k,
            )
            vector_rows = await conn.fetch(
                cte
                + """ SELECT * FROM allowed WHERE memory_embedding IS NOT NULL
                ORDER BY memory_embedding <=> $5::vector,memory_id LIMIT $6""",
                *args,
                vector,
                config.vector_k,
            )
            entity_rows = []
            if entities and config.entity_k:
                entity_rows = await conn.fetch(
                    cte
                    + """ SELECT * FROM allowed WHERE entities && $5::text[]
                    ORDER BY memory_id LIMIT $6""",
                    *args,
                    normalize_entities(entities),
                    config.entity_k,
                )
        return fuse(text_rows, vector_rows, entity_rows, purpose, config)
