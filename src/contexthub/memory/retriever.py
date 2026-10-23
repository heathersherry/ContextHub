"""PG full text + exact cosine scan, RRF; no P1 identity or P2 policy decisions."""

from dataclasses import dataclass, field
from datetime import datetime
import re

from .contracts import ref_of, rule_queries
from .direct_candidates import DIRECT_CANDIDATE_POLICY, GENERIC_LEXEMES, select_candidates
from .schemas import normalize_entities, normalize_topics
from .storage import SCOPE


# Deliberately loose P1-only cutoff; review against future recall/cost evidence.
P1_MIN_SIMILARITY = 0.15


@dataclass(frozen=True)
class SearchHit:
    memory: dict
    score: float
    channels: tuple[str, ...]


@dataclass
class SessionCandidates:
    """Each item's old hits plus a shared pool of this session's pending items.

    Pending items have no memory identity. Only their accepted results may become
    candidates for direct decisions or evidence for binding.
    """

    items: list
    stored: dict[int, set[str]]
    query_vectors: dict[int, str]
    accepted: dict[int, list[str]] = field(default_factory=dict)

    async def resolve(self, index, store, context):
        keys = self.stored[index] | {key for keys in self.accepted.values() for key in keys}
        rows = {}
        for key in sorted(keys):
            row = await store.read_ref(context.trace, key)
            if row["accepted"] and row["status"] != "deleted":
                rows[key] = dict(row)
        # Earlier direct operations may have replaced a cached candidate. Keep
        # history selectable for corrections and also supply its latest version.
        matters = {r["memory_id"] for r in rows.values()}
        for row in await store.current_rows(context.trace):
            if (
                row["memory_id"] in matters
                and row["accepted"]
                and row["status"] != "deleted"
            ):
                rows[ref_of(row)] = row
        return rows


HISTORY = __import__("re").compile(
    r"\b(over time|from earliest to latest|previously|formerly|used to|originally|in the past|history of|historical|before the|prior to)\b",
    __import__("re").I,
)

RULE_QUERY = re.compile(
    r"\b(?:polic(?:y|ies)|rules?|guidelines?)\b"
    r"|\b(?:under what|under which) (?:conditions?|circumstances?)\b"
    r"|\b(?:what|how|where|which|would|could|should)\b[^?]*\bif\b(?!\s+any\b)"
    r"|^\s*(?:if|suppose|assuming)\b"
    r"|如果|假如|假设|规则|政策|规定|制度|什么条件|哪些条件",
    re.I,
)


def answer_purpose(question):
    # Explicit policy/hypothetical cues opt into rules; otherwise current facts.
    if HISTORY.search(question):
        return "history"
    return "policy" if RULE_QUERY.search(question) else "answer"


def entity_pattern(entity):
    """Literal names/labels; URL paths must not match longer addresses."""
    if "://" in entity or "/" in entity or "." in entity:
        scheme = r"(?:https?://)?" if "://" not in entity else ""
        return re.compile(r"(?<![\w/.@-])" + scheme + re.escape(entity) + r"(?![\w/?#=&%+~:@-]|\.\w)")
    return re.compile(r"(?<!\w)" + re.escape(entity) + r"(?!\w)")


def allowed_cte(purpose, *, history_candidates=False):
    base = f"{SCOPE} AND created_in_session <= $4"
    current = f"SELECT DISTINCT ON(memory_id) * FROM memories WHERE {base} AND temporal_scope='current' ORDER BY memory_id,version DESC"
    usable = (
        "status='fresh' AND type IN ('fact','rule') AND accepted AND NOT invalidated"
    )
    if purpose in ("answer", "policy", "p2"):
        return f"WITH latest AS MATERIALIZED ({current}), allowed AS MATERIALIZED (SELECT * FROM latest WHERE {usable})"
    if purpose == "history":
        return f"WITH allowed AS MATERIALIZED (SELECT * FROM memories WHERE {base} AND accepted AND NOT invalidated AND status<>'deleted' AND type IN ('fact','rule'))"
    if purpose == "forget":
        return f"WITH allowed AS MATERIALIZED (SELECT * FROM memories WHERE {base} AND accepted AND status<>'deleted' AND type IN ('fact','rule'))"
    if purpose == "p1":
        return f"WITH latest AS MATERIALIZED ({current}), allowed AS MATERIALIZED (SELECT * FROM latest WHERE status<>'deleted' AND accepted)"
    if purpose == "p2_match":
        return f"""WITH latest AS MATERIALIZED ({current}), candidates AS MATERIALIZED (
            SELECT * FROM latest UNION
            SELECT * FROM memories WHERE {base} AND (temporal_scope IS NULL OR temporal_scope='historical' OR (accepted AND {str(history_candidates).lower()}))
        ), allowed AS MATERIALIZED (SELECT * FROM candidates WHERE status<>'deleted' AND type IN ('fact','rule'))"""
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
    if purpose in ("answer", "policy"):
        try:
            for key, (row, _, _) in candidates.items():
                dates[key] = datetime.fromisoformat(row["timestamp"])
            sorted(dates.values())  # mixed timezone awareness is not comparable
        except (ValueError, TypeError):
            dates = {}
    rows = list(candidates.items())
    rows.sort(key=lambda pair: (str(pair[0][0]), pair[0][1]))
    if purpose in ("answer", "policy"):
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

    async def topic_rows(self, conn, cte, table, args, topics):
        """Best label pair per eligible version, before any top-k truncation."""
        if not topics:
            return []
        return await conn.fetch(
            cte + f""" SELECT m.*,pair.query_topic,pair.candidate_topic,pair.topic_similarity
            FROM {table} m CROSS JOIN LATERAL (
              SELECT q.topic AS query_topic,t.topic AS candidate_topic,
                     1-(q.embedding <=> t.embedding) AS topic_similarity
              FROM topic_embeddings q CROSS JOIN topic_embeddings t
              WHERE q.topic=ANY(${len(args)+1}::text[]) AND t.topic=ANY(m.topics)
              ORDER BY q.embedding <=> t.embedding,q.topic,t.topic LIMIT 1
            ) pair ORDER BY pair.topic_similarity DESC,m.memory_id,m.version""",
            *args, topics,
        )

    async def candidate_topic_matches(self, focus, candidates, context):
        topics = normalize_topics(focus["topics"])
        await self.indexer.index_topics(topics, context)
        async with self.store.pool.acquire() as conn:
            rows = await self.topic_rows(
                conn,
                f"""WITH topic_candidates AS (
                    SELECT * FROM memories WHERE {SCOPE}
                    AND created_in_session <= $4 AND accepted AND status<>'deleted'
                    AND (memory_id::text || ':' || version::text)=ANY($5::text[])
                )""", "topic_candidates",
                (*context.trace.args, context.session_seq, list(candidates)), topics,
            )
        return {ref_of(row): self.topic_evidence(row) for row in rows}

    @staticmethod
    def topic_evidence(row):
        return {key: row[key] for key in ("query_topic", "candidate_topic", "topic_similarity")}

    def topic_selected(self, evidence):
        return bool(evidence) and evidence["topic_similarity"] > self.config.topic_similarity_threshold

    async def filter_rule_candidates(self, rule, candidates, context):
        """Two vector queries over the merged, matter-deduplicated P1 pool.

        No top-k here: same-session facts and previous endpoints get scored too.
        The retained facts still need both role judgments against the full rule.
        """
        queries = rule_queries(rule["text"])
        vectors = {
            role: await self.indexer.query_vector(query, context)
            for role, query in queries.items()
        }
        topic_matches = await self.candidate_topic_matches(rule, candidates, context)
        async with self.store.pool.acquire() as conn:
            scores = await conn.fetch(
                f"""SELECT memory_id,version,
                1 - (memory_embedding <=> $5::vector) AS condition_similarity,
                1 - (memory_embedding <=> $6::vector) AS affected_similarity
                FROM memories WHERE {SCOPE}
                AND (memory_id::text || ':' || version::text) = ANY($4::text[])
                ORDER BY memory_id,version""",
                *context.trace.args, list(candidates),
                vectors["condition"], vectors["affected"],
            )
        kept = {}
        audit = []
        for row in scores:
            key = ref_of(row)
            condition, affected = row["condition_similarity"], row["affected_similarity"]
            if condition is None or affected is None:
                raise ValueError("P1 candidate must be indexed before filtering")
            topic_match = topic_matches.get(key)
            selected = max(condition, affected) >= P1_MIN_SIMILARITY or self.topic_selected(topic_match)
            if selected:
                kept[key] = candidates[key]
            audit.append({
                "ref": key, "condition_similarity": condition,
                "affected_similarity": affected, "selected": selected,
                "topic_match": topic_match,
            })
        await self.store.record(context, "p1.candidates", {
            "rule": ref_of(rule), "queries": queries,
            "min_similarity": P1_MIN_SIMILARITY,
            "topic_similarity_threshold": self.config.topic_similarity_threshold,
            "candidates": audit, "selected_refs": list(kept),
        })
        return kept

    async def prepare(self, items, context):
        stored = {}
        query_vectors = {}
        await self.indexer.index_topics([topic for item in items for topic in item.topics], context)
        for index, item in enumerate(items):
            query_vectors[index] = await self.indexer.query_vector(item.text, context)
            hits = await self.candidates(
                item.__dict__, context, query_vector=query_vectors[index],
            )
            stored[index] = set(hits)
        batch = SessionCandidates(items=items, stored=stored, query_vectors=query_vectors)
        await self.store.record(
            context,
            "retrieval.prepared",
            {
                "stored": {str(i): sorted(refs) for i, refs in stored.items()},
                "session_items": list(range(len(items))),
            },
        )
        return batch

    async def candidates(self, focus, context, *, query_vector=None):
        """Shared ingestion candidates include both rules and facts."""
        hits = await self.search(
            focus["text"],
            context,
            through_session=context.session_seq,
            purpose="p2_match",
            entities=focus["entities"],
            accepted_only=True,
            exclude_id=focus.get("memory_id"),
            query_vector=query_vector,
            topics=focus["topics"],
        )
        rows = {ref_of(h.memory): dict(h.memory) for h in hits}
        ids = {r["memory_id"] for r in rows.values()}
        # A historical match must not hide the same matter's latest current version.
        for row in await self.store.current_rows(context.trace):
            if (
                row["memory_id"] in ids
                and row["accepted"]
                and row["status"] != "deleted"
            ):
                rows[ref_of(row)] = row
        return rows

    async def filter_direct_candidates(
        self, focus, candidates, context, *, current_refs, query_vector=None,
        memory_types=("fact",), log_stage="p2_new.candidates",
    ):
        """Shared filter for new facts and the semantic forget supplement."""
        if not candidates:
            await self.store.record(context, log_stage, {
                "query": focus["text"], "policy": DIRECT_CANDIDATE_POLICY,
                "candidates": [], "selected_refs": [],
            })
            return {}
        # The pipeline supplies the initial query vector. A standalone direct
        # call has no initial retrieval, so it obtains its query vector here.
        if query_vector is None:
            query_vector = await self.indexer.query_vector(focus["text"], context)
        async with (
            self.store.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            query_lexemes = await conn.fetchval(
                "SELECT tsvector_to_array(to_tsvector($1::regconfig,$2))",
                self.store.config.text_config, focus["text"],
            )
            corpus = await conn.fetch(
                f"""SELECT memory_id,tsvector_to_array(search_vector) AS lexemes
                FROM memories WHERE {SCOPE} AND created_in_session <= $4
                AND accepted AND status <> 'deleted' AND type=ANY($5::text[])""",
                *context.trace.args, context.session_seq, list(memory_types),
            )
            scored = await conn.fetch(
                f"""SELECT memory_id,version,tsvector_to_array(search_vector) AS lexemes,
                1 - (memory_embedding <=> $6::vector) AS similarity
                FROM memories WHERE {SCOPE} AND created_in_session <= $5
                AND accepted AND status <> 'deleted' AND type=ANY($7::text[])
                AND (memory_id::text || ':' || version::text) = ANY($4::text[])
                ORDER BY memory_id,version""",
                *context.trace.args, list(candidates), context.session_seq, query_vector,
                list(memory_types),
            )
        query_text = normalize_entities([focus["text"]])[0]
        names = {
            name: entity_pattern(name)
            for name in normalize_entities(focus["entities"])
            if name not in GENERIC_LEXEMES
        }
        names = {name: pattern for name, pattern in names.items() if pattern.search(query_text)}
        scores = []
        for row in scored:
            key = ref_of(row)
            text = normalize_entities([candidates[key]["text"]])[0]
            entities = [name for name, pattern in names.items() if pattern.search(text)]
            # Only explicit alphanumeric identifiers or URLs bypass the lower
            # threshold; a shared ordinary name alone does not.
            identifiers = [
                name for name in entities
                if any(c.isalpha() for c in name) and (
                    any(c.isdigit() for c in name) or "/" in name
                )
            ]
            scores.append(dict(row, ref=key, entities=entities, identifiers=identifiers))
        decisions, corpus_info = select_candidates(scores, corpus, query_lexemes)
        topic_matches = await self.candidate_topic_matches(focus, candidates, context)
        for decision in decisions:
            topic_match = topic_matches.get(decision["ref"])
            decision["topic_match"] = topic_match
            if self.topic_selected(topic_match) and not decision["selected"]:
                decision.update(selected=True, reason="topic_similarity")
        selected = {d["ref"] for d in decisions if d["selected"]}
        matters = {candidates[key]["memory_id"] for key in selected}
        # A selected past version must not make the current version of the
        # same matter unavailable for update/history decisions.
        for decision in decisions:
            key = decision["ref"]
            if not decision["selected"] and key in current_refs and candidates[key]["memory_id"] in matters:
                selected.add(key)
                decision.update(selected=True, reason="matched_matter_current")
        await self.store.record(context, log_stage, {
            "query": focus["text"], "policy": DIRECT_CANDIDATE_POLICY,
            "corpus": corpus_info, "candidates": decisions,
            "topic_similarity_threshold": self.config.topic_similarity_threshold,
            "selected_refs": sorted(selected),
        })
        return {key: row for key, row in candidates.items() if key in selected}

    async def current(self, trace, through_session, *, purpose):
        """Current contents available by cutoff; not a snapshot of past validity."""
        async with self.store.pool.acquire() as conn:
            return await conn.fetch(
                allowed_cte(purpose) + " SELECT * FROM allowed ORDER BY memory_id",
                *trace.args,
                through_session,
            )

    async def forget_candidates(self, focus, context, primary=None, *, query_vector=None):
        # Literal matches cover all eligible versions, independent of stored
        # entity tags, vectors or validity. The semantic supplement uses the
        # same evidence filter as new facts before the model chooses scope.
        request = normalize_entities([focus["text"]])[0]
        patterns = {
            entity: entity_pattern(entity)
            for entity in normalize_entities(focus["entities"])
        }
        patterns = {entity: pattern for entity, pattern in patterns.items() if pattern.search(request)}
        rows = await self.current(context.trace, context.session_seq, purpose="forget")
        exact = {
            ref_of(row): dict(row) for row in rows
            if any(pattern.search(normalize_entities([row["text"]])[0]) for pattern in patterns.values())
        }
        candidates = dict(exact)
        if primary:
            candidates.update({ref_of(row): dict(row) for row in rows if ref_of(row) == primary})
        if query_vector is None:
            query_vector = await self.indexer.query_vector(focus["text"], context)
        hits = await self.search(
            focus["text"], context, through_session=context.session_seq,
            purpose="forget", exclude_refs=tuple(candidates), query_vector=query_vector,
            topics=focus["topics"],
        )
        supplement = {ref_of(hit.memory): dict(hit.memory) for hit in hits}
        current = {
            ref_of(row): dict(row)
            for row in await self.current(context.trace, context.session_seq, purpose="p1")
        }
        matters = {row["memory_id"] for row in supplement.values()}
        supplement.update({
            key: row for key, row in current.items()
            if row["memory_id"] in matters and key not in candidates
        })
        supplement = await self.filter_direct_candidates(
            focus, supplement, context, current_refs=set(current),
            query_vector=query_vector, memory_types=("fact", "rule"),
            log_stage="p2_forget_scope.candidates",
        )
        candidates.update(supplement)
        await self.store.record(context, "retrieval.forget_candidates", {
            "query": focus["text"], "entities": list(patterns),
            "scanned_versions": len(rows), "exact_refs": sorted(exact),
            "semantic_refs": [ref_of(hit.memory) for hit in hits],
            "filtered_semantic_refs": sorted(supplement),
            "primary_ref": primary, "selected_refs": sorted(candidates),
        })
        return candidates

    async def search(
        self,
        query,
        context,
        *,
        through_session,
        purpose,
        entities=(),
        exclude_session=False,
        memory_type=None,
        accepted_only=False,
        exclude_id=None,
        limit=None,
        exclude_refs=(),
        query_vector=None,
        topics=(),
    ):
        cte = allowed_cte(purpose, history_candidates=purpose in ("p1", "p2_match"))
        if through_session < 1 or through_session > context.session_seq:
            raise ValueError("invalid retrieval session cutoff")
        topics = normalize_topics(list(topics))
        await self.indexer.index_topics(topics, context)
        vector = query_vector if query_vector is not None else await self.indexer.query_vector(query, context)
        args = (*context.trace.args, through_session)
        config = self.config
        if limit is not None:
            if limit < 1:
                raise ValueError("positive retrieval limit required")
            config = config.model_copy(update={
                "text_k": min(config.text_k, limit),
                "vector_k": min(config.vector_k, limit),
                "hybrid_limit": limit,
                "entity_k": 0,
            })
        if purpose in ("p1", "p2_match") and exclude_session:
            cte += ", old_allowed AS MATERIALIZED (SELECT * FROM allowed WHERE created_in_session < $4)"
            table = "old_allowed"
        else:
            table = "allowed"
        filters = []
        if memory_type is not None:
            if memory_type not in ("fact", "rule"):
                raise ValueError("invalid candidate type")
            filters.append("type='" + memory_type + "'")
        if accepted_only:
            filters.append("accepted")
        if exclude_id is not None:
            from uuid import UUID

            filters.append("memory_id <> '" + str(UUID(str(exclude_id))) + "'::uuid")
        if filters:
            cte += (
                ", filtered AS MATERIALIZED (SELECT * FROM "
                + table
                + " WHERE "
                + " AND ".join(filters)
                + ")"
            )
            table = "filtered"
        if exclude_refs:
            from uuid import UUID
            from .contracts import split_ref

            # References come from stored rows; parse before building SQL literals.
            excluded = [
                "'" + str(UUID(str(memory_id))) + ":" + str(int(version)) + "'"
                for memory_id, version in map(split_ref, exclude_refs)
            ]
            cte += (
                f", remaining AS MATERIALIZED (SELECT * FROM {table} WHERE "
                "(memory_id::text || ':' || version::text) NOT IN (" + ",".join(excluded) + "))"
            )
            table = "remaining"
        # "the" is an English stop word; "user" is a generic subject in extracted
        # facts and must not make unrelated memories match the text query.
        # Filter query lexemes only, so existing document indexes remain usable.
        tsquery = "to_tsquery($5::regconfig,coalesce((SELECT string_agg(quote_literal(w),' | ') FROM unnest(tsvector_to_array(to_tsvector($5::regconfig,$6))) AS w WHERE w <> 'user'),''))"
        async with (
            self.store.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            # All three channels read the same snapshot. Scope/version/status come before LIMIT.
            if purpose in ("answer", "policy", "history"):
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
            topic_rows = [
                row for row in await self.topic_rows(conn, cte, table, args, topics)
                if self.topic_selected(row)
            ]
        result = fuse(text_rows, vector_rows, entity_rows, purpose, config)
        positions = {ref_of(hit.memory): i for i, hit in enumerate(result)}
        for row in topic_rows:
            key = ref_of(row)
            if key in positions:
                i = positions[key]
                hit = result[i]
                result[i] = SearchHit(hit.memory, hit.score, hit.channels + ("topic",))
            else:
                positions[key] = len(result)
                result.append(SearchHit(dict(row), 0, ("topic",)))
        from .contracts import material

        await self.store.record(
            context,
            "retrieval",
            {
                "purpose": purpose,
                "query": query,
                "entities": list(entities),
                "topics": topics,
                "through_session": through_session,
                "filter": cte,
                "config": config.model_dump(),
                "candidate_counts": {
                    "topic": len(topic_rows),
                    **{
                    name: rows[0]["candidate_count"] if rows else 0
                    for name, rows in (
                        ("text", text_rows),
                        ("vector", vector_rows),
                        ("entity", entity_rows),
                    )
                    },
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
                "topic": [
                    dict(self.topic_evidence(row), memory=material(row)) for row in topic_rows
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
