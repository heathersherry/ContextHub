"""Only explicit research databases; never run product migrations here."""

from pathlib import Path
from uuid import uuid4

import asyncpg

from contexthub.db.codecs import init_pg_connection
from .schemas import IndexConfig, Trace, normalize_entities

SCOPE = "run_id=$1 AND episode_id=$2 AND strategy_id=$3"


class MemoryStore:
    def __init__(self, pool, config):
        self.pool = pool
        self.config = config

    @classmethod
    async def open(cls, dsn: str, config: IndexConfig, *, initialize=False):
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=5, init=init_pg_connection
        )
        try:
            async with pool.acquire() as conn:
                database = await conn.fetchval("SELECT current_database()")
                if not database.startswith("contexthub_memory_"):
                    raise ValueError("dedicated contexthub_memory_* database required")
                async with conn.transaction():
                    if initialize:
                        # Initialization is deliberate and only valid for an empty database.
                        if await conn.fetchval(
                            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
                        ):
                            raise ValueError(
                                "initialization requires an empty research database"
                            )
                        await conn.fetchval("SELECT $1::regconfig", config.text_config)
                        schema = Path(__file__).with_name("schema.sql").read_text()
                        await conn.execute(
                            schema.replace("__DIMENSIONS__", str(config.dimensions))
                        )
                        await conn.execute(
                            "INSERT INTO research_config VALUES(true,$1)",
                            config.model_dump(),
                        )
                    saved = await conn.fetchval(
                        "SELECT config FROM research_config WHERE singleton"
                    )
                    if saved != config.model_dump():
                        raise ValueError("database index configuration mismatch")
            return cls(pool, config)
        except BaseException:
            await pool.close()
            raise

    async def close(self):
        await self.pool.close()

    async def save_session(self, trace: Trace, session_seq, timestamp, messages):
        if session_seq < 1:
            raise ValueError("positive session sequence required")
        numbered = [
            dict(message_no=i, role=m["role"], content=m["content"])
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
                    f"SELECT 1 FROM memory_steps WHERE {SCOPE} AND stage='session.failed' LIMIT 1",
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
                    parse_items(output)
                except (ValueError, TypeError):
                    raise ValueError(
                        "previous session extraction failed; stop this trajectory"
                    ) from None
            await conn.execute(
                "INSERT INTO session_records(run_id,episode_id,strategy_id,session_seq,timestamp,messages) VALUES($1,$2,$3,$4,$5,$6)",
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

    async def _insert(
        self,
        conn,
        trace,
        session_seq,
        text,
        type,
        entities,
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
            """INSERT INTO memories(run_id,episode_id,strategy_id,memory_id,version,
            text,type,origin,status,created_in_session,timestamp,entities)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,'unresolved',$9,$10,$11)""",
            *trace.args,
            memory_id,
            version,
            text,
            type,
            origin,
            session_seq,
            timestamp,
            normalize_entities(entities),
        )
        if sources is not None:
            await conn.execute(
                "INSERT INTO memory_sources VALUES($1,$2,$3,$4,$5,$6,$7)",
                *trace.args,
                memory_id,
                version,
                sorted(int(k) for k in sources),
                sources,
            )
        if type == "change":
            await conn.execute(
                "INSERT INTO change_events(run_id,episode_id,strategy_id,change_id) VALUES($1,$2,$3,$4)",
                *trace.args,
                memory_id,
            )
        return memory_id, version

    async def save_extracted(self, trace, session_seq, items):
        async with self.pool.acquire() as conn, conn.transaction():
            return [
                await self._insert(
                    conn,
                    trace,
                    session_seq,
                    item.text,
                    item.type,
                    item.entities,
                    "extracted",
                    uuid4(),
                    1,
                    {str(session_seq): item.message_refs},
                )
                for item in items
            ]

    async def add_memory(
        self, trace, session_seq, *, text, type, entities, origin, sources=None
    ):
        if origin == "derived" and sources is not None:
            raise ValueError("derived memories use dependency provenance")
        async with self.pool.acquire() as conn, conn.transaction():
            return await self._insert(
                conn,
                trace,
                session_seq,
                text,
                type,
                entities,
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
        origin,
        sources=None,
    ):
        if origin == "derived" and sources is not None:
            raise ValueError("derived memories use dependency provenance")
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
            if (
                old is None
                or old["type"] == "change"
                or type == "change"
                or old["status"] == "deleted"
            ):
                raise ValueError("cannot version a missing, forgotten or change memory")
            if session_seq < old["created_in_session"]:
                raise ValueError("cannot create a version in an earlier session")
            return await self._insert(
                conn,
                trace,
                session_seq,
                text,
                type,
                entities,
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

    async def put_dependency(self, trace, upstream, downstream, edge_status, reason):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO dependencies(run_id,episode_id,strategy_id,upstream_id,upstream_version,downstream_id,downstream_version,edge_status,reason) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT(run_id,episode_id,strategy_id,upstream_id,upstream_version,downstream_id,downstream_version)
                DO UPDATE SET edge_status=EXCLUDED.edge_status,reason=EXCLUDED.reason""",
                *trace.args,
                *upstream,
                *downstream,
                edge_status,
                reason,
            )

    async def downstream(self, trace, upstream_id):
        """All known old/new version edges, without similarity truncation. Not a repair proof."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                f"SELECT * FROM dependencies WHERE {SCOPE} AND upstream_id=$4 ORDER BY upstream_version,downstream_id,downstream_version",
                *trace.args,
                upstream_id,
            )

    async def get_change(self, trace, change_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"SELECT * FROM change_events WHERE {SCOPE} AND change_id=$4",
                *trace.args,
                change_id,
            )

    async def bind_change(self, trace, change_id, operation, targets):
        """Persist an explicit P1 binding; never infer targets or apply changes."""
        if operation not in ("update", "forget"):
            raise ValueError("unknown change operation")
        async with self.pool.acquire() as conn, conn.transaction():
            for target in targets:
                from uuid import UUID

                if not await conn.fetchval(
                    f"SELECT 1 FROM memories WHERE {SCOPE} AND memory_id=$4 AND version=$5",
                    *trace.args,
                    UUID(str(target["memory_id"])),
                    target["version"],
                ):
                    raise ValueError("change target missing in this trace")
            result = await conn.execute(
                f"UPDATE change_events SET operation=$5,target_refs=$6 WHERE {SCOPE} AND change_id=$4 AND applied_session_seq IS NULL",
                *trace.args,
                change_id,
                operation,
                targets,
            )
            if result != "UPDATE 1":
                raise ValueError("change missing or already applied")

    async def record(self, context, stage, payload, *, conn=None):
        import json

        payload = dict(payload, question_id=context.question_id, phase=context.phase)
        values = (
            *context.trace.args,
            context.session_seq,
            stage,
            json.loads(json.dumps(payload, default=str)),
        )
        sql = "INSERT INTO memory_steps(run_id,episode_id,strategy_id,session_seq,stage,payload) VALUES($1,$2,$3,$4,$5,$6)"
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

    async def related(self, trace, refs):
        from .contracts import split_ref, ref_of

        ids = list({split_ref(r)[0] for r in refs})
        async with self.pool.acquire() as conn:
            edges = await conn.fetch(
                f"""SELECT * FROM dependencies WHERE {SCOPE}
                AND (upstream_id=ANY($4::uuid[]) OR downstream_id=ANY($4::uuid[]))""",
                *trace.args,
                ids,
            )
            keys = {
                f"{e[p + '_id']}:{e[p + '_version']}"
                for e in edges
                for p in ("upstream", "downstream")
            }
            rows = [await self.read_ref(trace, r, conn=conn) for r in sorted(keys)]
            # Include the current version of each known neighbor without top-k truncation.
            current = await conn.fetch(
                f"""SELECT DISTINCT ON(memory_id) * FROM memories WHERE {SCOPE}
                AND memory_id=ANY($4::uuid[]) AND temporal_scope='current'
                ORDER BY memory_id,version DESC""",
                *trace.args,
                list({r["memory_id"] for r in rows}),
            )
        materials = {
            ref_of(r): dict(r)
            for r in [*rows, *current]
            if r["status"] != "deleted" and r["merged_into"] is None
        }
        visible_edges = [
            dict(e)
            for e in edges
            if all(
                f"{e[p + '_id']}:{e[p + '_version']}" in materials
                for p in ("upstream", "downstream")
            )
        ]
        return list(materials.values()), visible_edges
