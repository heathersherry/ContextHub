"""Memory context on the shared execution ledger, retry and pricing contract."""

from dataclasses import dataclass
from uuid import uuid4

from contexthub.db.repository import PgRepository
from contexthub.llm.retry_policy import RecordedCall, RetryPolicy
from contexthub.models.execution import AttemptRecord, CallIdentity, PriceBasis
from contexthub.models.knowledge import canonical_hash
from contexthub.services.execution_ledger import ExecutionLedger, validate_append
from .schemas import MemoryScope


class MemoryLedger(ExecutionLedger):
    def __init__(
        self, store, trace, session_seq, extraction_attempt, parser, output_attempt
    ):
        super().__init__(PgRepository(store.pool))
        self.raw_output = None
        self.trace = trace
        self.session_seq = session_seq
        self.extraction_attempt = extraction_attempt
        self.parser = parser
        self.output_attempt = output_attempt

    async def append(self, record):
        record = AttemptRecord.model_validate(record.model_dump(mode="json"))
        identity = record.identity
        if identity.event_id is not None or identity.work_item_id is not None:
            raise ValueError(
                "memory calls do not reference context-store events or work items"
            )
        parse_result = error = None
        raw = record.raw_output
        if record.phase == "finished":
            self.raw_output = raw
        if record.phase == "finished" and raw is not None and self.parser is not None:
            try:
                _, warnings = self.parser(raw)
                parse_result = "usable"
                error = "; ".join(warnings) or None
            except ValueError as exc:
                parse_result, error = "unusable", str(exc)
        if record.error:
            error = record.error.code
        reason = (
            "transport_error"
            if record.attempt_no > 1
            else "parse_error"
            if (self.extraction_attempt and self.extraction_attempt > 1)
            or (self.output_attempt and self.output_attempt > 1)
            else "initial"
        )
        async with self.repository.session(identity.account_id) as db:
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                identity.account_id + ":" + str(identity.call_id),
            )
            previous = await self._read(db, identity.account_id, identity.call_id)
            if not validate_append(previous, record):
                return
            await db.execute(
                """INSERT INTO execution_attempts
                (account_id,call_id,attempt_no,phase,execution_id,sha256,payload,
                 tenant_id,namespace,owner_id,session_seq,extraction_attempt,retry_reason,parse_result,raw_output,error)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
                identity.account_id,
                identity.call_id,
                record.attempt_no,
                record.phase,
                identity.execution_id,
                canonical_hash(record),
                record.model_dump(mode="json"),
                *self.trace.args,
                self.session_seq,
                self.extraction_attempt,
                reason,
                parse_result,
                raw,
                error,
            )


@dataclass(frozen=True)
class CallContext:
    store: object
    trace: MemoryScope
    session_seq: int
    code_version: str
    question_id: str | None = None
    phase: str | None = None

    def make(
        self,
        stage: str,
        config,
        policy: RetryPolicy,
        *,
        extraction_attempt=None,
        output_attempt=None,
        parser=None,
        price: PriceBasis | None = None,
    ):
        return RecordedCall(
            identity=CallIdentity(
                account_id=self.trace.tenant_id,
                execution_id=uuid4(),
                call_id=uuid4(),
                strategy=self.trace.owner_id,
                stage=stage,
                operation=":".join(
                    x
                    for x in (
                        stage,
                        self.question_id,
                        self.phase,
                        f"output_attempt_{output_attempt}" if output_attempt else None,
                    )
                    if x
                ),
                model_role=stage,
                code_version=self.code_version,
                config_sha256=canonical_hash(config),
            ),
            policy=policy,
            price=price,
            capture_raw_output=True,
            recorder=MemoryLedger(
                self.store,
                self.trace,
                self.session_seq,
                extraction_attempt,
                parser,
                output_attempt,
            ),
        )
