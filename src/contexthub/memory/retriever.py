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


HISTORY = __import__("re").compile(
    r"\b(over time|from earliest to latest|previously|formerly|used to|originally|in the past|history of|historical|before the|prior to)\b",
    __import__("re").I,
)


def answer_purpose(question):
    return "history" if HISTORY.search(question) else "answer"


def allowed_cte(purpose, *, history_candidates=False):
    base = f"{SCOPE} AND created_in_session <= $4"
    current = f"SELECT DISTINCT ON(memory_id) * FROM memories WHERE {base} AND temporal_scope='current' ORDER BY memory_id,version DESC"
    usable = "status='fresh' AND type IN ('fact','rule') AND accepted AND NOT invalidated AND merged_into IS NULL"
    if purpose in ("answer", "p2"):
        return f"WITH latest AS MATERIALIZED ({current}), allowed AS MATERIALIZED (SELECT * FROM latest WHERE {usable})"
    if purpose == "history":
        return f"WITH allowed AS MATERIALIZED (SELECT * FROM memories WHERE {base} AND accepted AND NOT invalidated AND status<>'deleted' AND merged_into IS NULL AND type IN ('fact','rule'))"
    if purpose == "p1":
        return f"""WITH latest AS MATERIALIZED ({current}), candidates AS MATERIALIZED (
            SELECT * FROM latest UNION
            SELECT * FROM memories WHERE {base} AND (temporal_scope IS NULL OR temporal_scope='historical' OR (accepted AND {str(history_candidates).lower()}))
        ), allowed AS MATERIALIZED (SELECT * FROM candidates WHERE status<>'deleted' AND merged_into IS NULL AND type IN ('fact','rule','change'))"""
    raise ValueError("unknown retrieval purpose")


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

    async def search(
        self,
        query,
        context,
        *,
        through_session,
        purpose,
        entities=(),
        exclude_session=False,
    ):
        cte = allowed_cte(purpose, history_candidates=purpose == "p1")
        if through_session < 1 or through_session > context.session_seq:
            raise ValueError("invalid retrieval session cutoff")
        vector = await self.indexer.query_vector(query, context)
        args = (*context.trace.args, through_session)
        config = self.config
        if purpose == "p1" and exclude_session:
            cte += ", old_allowed AS MATERIALIZED (SELECT * FROM allowed WHERE created_in_session < $4)"
            table = "old_allowed"
        else:
            table = "allowed"
        # PostgreSQL lexemes are quoted before OR composition, including punctuation.
        tsquery = "to_tsquery($5::regconfig,coalesce((SELECT string_agg(quote_literal(w),' | ') FROM unnest(tsvector_to_array(to_tsvector($5::regconfig,$6))) AS w),''))"
        async with (
            self.store.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            # All three channels read the same snapshot. Scope/version/status come before LIMIT.
            if purpose in ("answer", "history"):
                import re

                names = await conn.fetch(
                    cte + " SELECT DISTINCT unnest(entities) AS name FROM allowed",
                    *args,
                )
                normalized = normalize_entities([query])[0] if query.strip() else ""
                entities = [
                    r["name"]
                    for r in names
                    if re.search(
                        r"(?<!\w)" + re.escape(r["name"]) + r"(?!\w)", normalized
                    )
                ]
            text_rows = await conn.fetch(
                cte
                + f" SELECT *,count(*) OVER() AS candidate_count,ts_rank_cd(search_vector,{tsquery}) AS text_score FROM {table} WHERE search_vector @@ {tsquery} ORDER BY ts_rank_cd(search_vector,{tsquery}) DESC,memory_id,version LIMIT $7",
                *args,
                self.store.config.text_config,
                query,
                config.text_k,
            )
            vector_rows = await conn.fetch(
                cte
                + f""" SELECT *,count(*) OVER() AS candidate_count,memory_embedding <=> $5::vector AS vector_distance FROM {table} WHERE memory_embedding IS NOT NULL
                ORDER BY memory_embedding <=> $5::vector,memory_id,version LIMIT $6""",
                *args,
                vector,
                config.vector_k,
            )
            hybrid = fuse(text_rows, vector_rows, [], purpose, config)
            entity_rows = []
            if entities and config.entity_k:
                selected = [
                    f"{h.memory['memory_id']}:{h.memory['version']}" for h in hybrid
                ]
                entity_rows = await conn.fetch(
                    cte
                    + f""" SELECT *,count(*) OVER() AS candidate_count,memory_embedding <=> $7::vector AS vector_distance FROM {table} WHERE entities && $5::text[]
                    AND NOT (memory_id::text || ':' || version::text = ANY($6::text[]))
                    AND memory_embedding IS NOT NULL
                    ORDER BY memory_embedding <=> $7::vector,memory_id,version LIMIT $8""",
                    *args,
                    normalize_entities(entities),
                    selected,
                    vector,
                    config.entity_k,
                )
        result = fuse(text_rows, vector_rows, entity_rows, purpose, config)
        from .contracts import material

        await self.store.record(
            context,
            "retrieval",
            {
                "purpose": purpose,
                "query": query,
                "entities": list(entities),
                "through_session": through_session,
                "filter": cte,
                "config": config.model_dump(),
                "candidate_counts": {
                    name: rows[0]["candidate_count"] if rows else 0
                    for name, rows in (
                        ("text", text_rows),
                        ("vector", vector_rows),
                        ("entity", entity_rows),
                    )
                },
                "text": [
                    {"rank": i, "score": r["text_score"], "memory": material(r)}
                    for i, r in enumerate(text_rows, 1)
                ],
                "vector": [
                    {"rank": i, "distance": r["vector_distance"], "memory": material(r)}
                    for i, r in enumerate(vector_rows, 1)
                ],
                "entity": [
                    {"rank": i, "distance": r["vector_distance"], "memory": material(r)}
                    for i, r in enumerate(entity_rows, 1)
                ],
                "selected": [
                    {
                        "memory": material(h.memory),
                        "rrf": h.score,
                        "channels": h.channels,
                    }
                    for h in result
                ],
            },
        )
        return result
