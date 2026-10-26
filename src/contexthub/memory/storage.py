"""Application memory storage in the contexthub_memory PostgreSQL schema."""

from pathlib import Path
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg

from contexthub.db.codecs import init_pg_connection
from .contracts import ref_of
from .schemas import IndexConfig, MemoryScope, normalize_entities, normalize_topics

SCOPE = "tenant_id=$1 AND namespace=$2 AND owner_id=$3"


class MemoryStore:
    def __init__(self, pool, config, *, prompts=None, lease_pool=None):
        from .prompts_config import PromptSet
        self.pool, self.config = pool, config
        self.prompts = prompts or PromptSet()
        self.lease_pool = lease_pool

    @classmethod
    async def open(cls, dsn: str, config: IndexConfig, *, initialize=False, prompts=None):
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=10, init=init_pg_connection,
            server_settings={"search_path": "contexthub_memory,public"},
        )
        leases = None
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('contexthub_memory_schema',0))")
                exists = await conn.fetchval("SELECT to_regclass('contexthub_memory.memory_config')")
                if not exists:
                    if await conn.fetchval("SELECT to_regclass('public.research_config')"):
                        raise ValueError("legacy research schema: run python -m contexthub.memory.migrate_research explicitly")
                    if not initialize:
                        raise ValueError("memory schema missing; initialize it explicitly")
                    await conn.execute("CREATE SCHEMA contexthub_memory")
                    schema = Path(__file__).with_name("schema.sql").read_text()
                    await conn.execute(schema.replace("__DIMENSIONS__", str(config.dimensions)))
                    await conn.execute("INSERT INTO memory_config VALUES(true,$1)", config.model_dump())
                saved = await conn.fetchval("SELECT config FROM memory_config WHERE singleton")
                if saved != config.model_dump():
                    raise ValueError("database index configuration mismatch")
            leases = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
            return cls(pool, config, prompts=prompts, lease_pool=leases)
        except BaseException:
            if leases is not None:
                await leases.close()
            await pool.close()
            raise

    async def close(self):
        if self.lease_pool is not None:
            await self.lease_pool.close()
        await self.pool.close()

    @asynccontextmanager
    async def lease(self, scope):
        # A second pool prevents writers holding every work connection while
        # they await model calls. Try-lock rejects overlapping operations.
        from contexthub.errors import ConflictError
        async with self.lease_pool.acquire() as conn:
            key = "memory:" + scope.model_dump_json()
            locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", key)
            if not locked:
                raise ConflictError("memory_namespace_busy")
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)

    async def latest_session(self, scope):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(f"SELECT coalesce(max(session_seq),0) FROM session_records WHERE {SCOPE}", *scope.args)

    async def save_session(self, trace: MemoryScope, session_seq, timestamp, messages):
        if session_seq < 1:
            raise ValueError("positive session sequence required")
        numbered = [
            dict(message_no=i, role=m["role"], content=m["content"],
                 **({"timestamp": m["timestamp"]} if "timestamp" in m else {}))
            for i, m in enumerate(messages, 1)
        ]
        if any(
            m["role"] not in ("user", "assistant", "system", "tool")
            or not isinstance(m["content"], str)
            for m in numbered
        ):
            raise ValueError("invalid session message")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                "session:" + trace.model_dump_json(),
            )
            previous = await conn.fetchval(
                f"SELECT max(session_seq) FROM session_records WHERE {SCOPE}",
                *trace.args,
            )
            if session_seq != (previous or 0) + 1:
                raise ValueError("sessions must be ingested once, in order")
            if previous is not None:
                failed = await conn.fetchval(
                    f"""SELECT 1 FROM memory_steps failed WHERE {SCOPE}
                    AND stage='session.failed' AND NOT EXISTS (
                        SELECT 1 FROM memory_steps completed
                        WHERE completed.tenant_id=failed.tenant_id
                        AND completed.namespace=failed.namespace
                        AND completed.owner_id=failed.owner_id
                        AND completed.session_seq=failed.session_seq
                        AND completed.stage='session.complete'
                        AND completed.step_id>failed.step_id
                    ) LIMIT 1""",
                    *trace.args,
                )
                if failed:
                    raise ValueError(
                        "previous maintenance failed; stop this trajectory"
                    )
                output = await conn.fetchval(
                    f"SELECT extraction_output FROM session_records WHERE {SCOPE} AND session_seq=$4",
                    *trace.args,
                    previous,
                )
                from .extractor import parse_items

                try:
                    parse_items(output, kind=None)
                except (ValueError, TypeError):
                    raise ValueError(
                        "previous session extraction failed; stop this trajectory"
                    ) from None
            await conn.execute(
                "INSERT INTO session_records(tenant_id,namespace,owner_id,session_seq,timestamp,messages) VALUES($1,$2,$3,$4,$5,$6)",
                *trace.args,
                session_seq,
                timestamp,
                numbered,
            )
        return numbered

    async def save_output(self, trace, session_seq, output):
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE session_records SET extraction_output=$5 WHERE {SCOPE} AND session_seq=$4",
                *trace.args,
                session_seq,
                output,
            )

    async def get_session(self, trace, session_seq):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"SELECT * FROM session_records WHERE {SCOPE} AND session_seq=$4",
                *trace.args,
                session_seq,
            )

    async def latest_repair_evidence(self, trace, key):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage IN "
                "('p2.condition_evidence','p2.repair_evidence') "
                "AND payload->>'target'=$4 ORDER BY step_id DESC LIMIT 1",
                *trace.args, key,
            )

    async def _insert(
        self,
        conn,
        trace,
        session_seq,
        text,
        type,
        entities,
        topics,
        origin,
        memory_id,
        version,
        sources,
    ):
        timestamp = await conn.fetchval(
            f"SELECT timestamp FROM session_records WHERE {SCOPE} AND session_seq=$4",
            *trace.args,
            session_seq,
        )
        await conn.execute(
            """INSERT INTO memories(tenant_id,namespace,owner_id,memory_id,version,
            text,type,origin,status,created_in_session,timestamp,entities,topics)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,'unresolved',$9,$10,$11,$12)""",
            *trace.args,
            memory_id,
            version,
            text,
            type,
            origin,
            session_seq,
            timestamp,
            normalize_entities(entities),
            normalize_topics(topics),
        )
        if sources is not None:
            await conn.execute(
                "INSERT INTO memory_sources VALUES($1,$2,$3,$4,$5,$6)",
                *trace.args,
                memory_id,
                version,
                sorted(set(sources)),
            )
        return memory_id, version

    async def add_memory(
        self, trace, session_seq, *, text, type, entities, topics, origin, sources=None
    ):
        if origin == "derived" and sources is not None:
            raise ValueError("derived memories use basis_refs")
        async with self.pool.acquire() as conn, conn.transaction():
            return await self._insert(
                conn,
                trace,
                session_seq,
                text,
                type,
                entities,
                topics,
                origin,
                uuid4(),
                1,
                sources,
            )

    async def add_version(
        self,
        trace,
        memory_id,
        session_seq,
        *,
        text,
        type,
        entities,
        topics,
        origin,
        sources=None,
    ):
        if origin == "derived" and sources is not None:
            raise ValueError("derived memories use basis_refs")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                trace.model_dump_json() + str(memory_id),
            )
            old = await conn.fetchrow(
                f"SELECT * FROM memories WHERE {SCOPE} AND memory_id=$4 ORDER BY version DESC LIMIT 1 FOR UPDATE",
                *trace.args,
                memory_id,
            )
            if old is None or old["status"] == "deleted":
                raise ValueError("cannot version a missing or forgotten memory")
            if session_seq < old["created_in_session"]:
                raise ValueError("cannot create a version in an earlier session")
            return await self._insert(
                conn,
                trace,
                session_seq,
                text,
                type,
                entities,
                topics,
                origin,
                memory_id,
                old["version"] + 1,
                sources,
            )

    async def set_status(self, trace, memory_id, version, status, reason=None):
        if status not in ("unresolved", "fresh", "stale"):
            raise ValueError("use forget() for all-version deletion")
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE memories SET status=$6,status_reason=$7 WHERE {SCOPE} AND memory_id=$4 AND version=$5 AND status <> 'deleted'",
                *trace.args,
                memory_id,
                version,
                status,
                reason,
            )
            if result != "UPDATE 1":
                raise ValueError("missing or forgotten version")

    async def forget(self, trace, memory_id, reason):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                trace.model_dump_json() + str(memory_id),
            )
            await conn.execute(
                f"UPDATE memories SET status='deleted',status_reason=$5 WHERE {SCOPE} AND memory_id=$4",
                *trace.args,
                memory_id,
                reason,
            )

    async def versions(self, trace, memory_id):
        """Audit read only. Never use raw history as answer evidence."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                f"SELECT * FROM memories WHERE {SCOPE} AND memory_id=$4 ORDER BY version",
                *trace.args,
                memory_id,
            )

    async def sources(self, trace, memory_id, version):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"SELECT * FROM memory_sources WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                *trace.args,
                memory_id,
                version,
            )

    async def record(self, context, stage, payload, *, conn=None):
        import json

        payload = dict(payload, question_id=context.question_id, phase=context.phase)
        values = (
            *context.trace.args,
            context.session_seq,
            stage,
            json.loads(json.dumps(payload, default=str)),
        )
        sql = "INSERT INTO memory_steps(tenant_id,namespace,owner_id,session_seq,stage,payload) VALUES($1,$2,$3,$4,$5,$6)"
        if conn is not None:
            await conn.execute(sql, *values)
        else:
            async with self.pool.acquire() as db:
                await db.execute(sql, *values)

    async def read_ref(self, trace, ref, *, conn=None):
        from .contracts import split_ref

        sql = f"SELECT * FROM memories WHERE {SCOPE} AND memory_id=$4 AND version=$5"
        args = (*trace.args, *split_ref(ref))
        if conn is not None:
            return await conn.fetchrow(sql, *args)
        async with self.pool.acquire() as db:
            return await db.fetchrow(sql, *args)

    async def current_rows(self, trace, matter_ids=None):
        async with self.pool.acquire() as conn:
            return [
                dict(r)
                for r in await conn.fetch(
                    f"SELECT DISTINCT ON(memory_id) * FROM memories WHERE {SCOPE} AND temporal_scope='current' AND ($4::uuid[] IS NULL OR memory_id=ANY($4)) ORDER BY memory_id,version DESC",
                    *trace.args, None if matter_ids is None else list(matter_ids),
                )
            ]

    async def active_rules(self, trace, *, condition_ids=None, affected_ids=None, rule_ids=None):
        """Read current active rule versions; optional routes are ORed together."""
        scoped = any(x is not None for x in (condition_ids, affected_ids, rule_ids))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT r.* FROM memories r
                WHERE {SCOPE} AND type='rule' AND temporal_scope='current'
                AND status='fresh' AND accepted AND NOT invalidated
                AND ($4::boolean OR condition_matter_ids && $5::uuid[]
                     OR affected_matter_ids && $6::uuid[] OR memory_id=ANY($7::uuid[]))
                AND NOT EXISTS (
                    SELECT 1 FROM memories newer WHERE newer.tenant_id=r.tenant_id
                    AND newer.namespace=r.namespace AND newer.owner_id=r.owner_id
                    AND newer.memory_id=r.memory_id AND newer.temporal_scope='current'
                    AND newer.version>r.version
                ) ORDER BY memory_id""",
                *trace.args, not scoped, list(condition_ids or ()),
                list(affected_ids or ()), list(rule_ids or ()),
            )
        return {ref_of(r): dict(r) for r in rows}
