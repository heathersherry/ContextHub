"""Local S3 operations use the same append-only ledger as physical model calls."""

from datetime import datetime, timezone
from time import monotonic

from contexthub.models.execution import (
    AttemptRecord,
    AttemptError,
    LocalWork,
    Usage,
    calculate_cost,
)
from contexthub.models.knowledge import canonical_hash


class LocalOperation:
    def __init__(self, sink, identity, inputs):
        self.sink, self.identity = sink, identity
        self.counts = {}
        usage = Usage(completeness="missing", missing_reason="local_operation_no_model")
        self.common = dict(
            identity=identity,
            attempt_no=1,
            requested_model=None,
            actual_model_missing_reason="local_operation_no_model",
            request_id_missing_reason="local_operation_no_provider",
            input_sha256=canonical_hash(inputs),
            prompt_sha256=canonical_hash({}),
            request_config_sha256=identity.config_sha256,
            retry_policy_sha256=canonical_hash({"local_attempts": 1}),
            started_at=datetime.now(timezone.utc),
            usage=usage,
            cost=calculate_cost(usage, None, None),
        )

    async def start(self):
        await self.sink.append(
            AttemptRecord(**self.common, phase="started", outcome="started")
        )
        self.tick = monotonic()

    def add(self, metric, count=1):
        self.counts[metric] = self.counts.get(metric, 0) + count

    async def finish(self, output, error=None):
        await self.sink.append(
            AttemptRecord(
                **self.common,
                phase="finished",
                outcome="error" if error else "success",
                error=AttemptError(
                    code=error, error_class="DiscoveryError", retryable=False
                )
                if error
                else None,
                stop_reason="non_retryable" if error else "success",
                finished_at=datetime.now(timezone.utc),
                wall_seconds=monotonic() - self.tick,
                output_sha256=canonical_hash(output),
                local_work=tuple(
                    LocalWork(metric=k, count=v) for k, v in sorted(self.counts.items())
                ),
            )
        )


class MeteredDB:
    """Count fetched rows and logical UTF-8 JSON bytes, including validation reads.

    This is local work, not network wire bytes or billed API usage.
    """

    def __init__(self, db, work):
        self.db, self.work = db, work

    def _observe(self, row):
        import json

        if row is not None:
            self.work.add("retrieval_rows")
            self.work.add(
                "retrieval_bytes",
                len(
                    json.dumps(dict(row), ensure_ascii=False, default=str).encode(
                        "utf-8"
                    )
                ),
            )

    async def fetchrow(self, sql, *args):
        row = await self.db.fetchrow(sql, *args)
        self._observe(row)
        return row

    async def fetch(self, sql, *args):
        rows = await self.db.fetch(sql, *args)
        for row in rows:
            self._observe(row)
        return rows

    async def fetchval(self, sql, *args):
        self.work.add("rule_checks")
        return await self.db.fetchval(sql, *args)

    async def execute(self, sql, *args):
        result = await self.db.execute(sql, *args)
        if sql.lstrip().startswith("INSERT"):
            self.work.add("writes")
        return result
