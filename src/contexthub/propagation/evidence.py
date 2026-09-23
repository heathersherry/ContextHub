"""Frozen M1/M2 evidence preparation. M3 reuses the exact M2 seal, without reads."""

import asyncio
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4
import asyncpg

from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.models.execution import AttemptError, AttemptRecord, LocalWork
from contexthub.models.knowledge import (
    ContractError,
    EvidenceRef,
    canonical_hash,
    text_hash,
)
from contexthub.models.maintenance import EvidenceBundle, ReadEntry
from contexthub.models.request import RequestContext
from contexthub.propagation.contracts import (
    EvidenceReceipt,
    MaintenancePolicy,
    RequiredInput,
)
from contexthub.propagation.executor import identity, trace
from contexthub.services.evidence_service import EvidenceService


EVIDENCE_ORDERING = "created_at-context_id-version/required-ordinal-span"


def required_manifest(frame, scope):
    if scope not in ("M1", "M2"):
        raise ContractError("invalid_evidence_scope")
    local = frame.required
    if scope == "M1":
        return local
    return tuple(
        dict.fromkeys(
            (
                *local,
                *(
                    RequiredInput(
                        source=s.source,
                        content_level=s.content_level,
                        role="provenance",
                    )
                    for s in frame.history
                ),
            )
        )
    )


def chunks_for(length, start, end, chunk_size):
    stop = length if end is None else end
    if start > length or stop > length or stop < start:
        raise ContractError("source_span_mismatch")
    return tuple(
        (pos, min(pos + chunk_size, stop)) for pos in range(start, stop, chunk_size)
    )


def check_progress(
    required_count, page_size, read_ordinals, pages, expected_chunks, chunks
):
    """Exact order detects lost/duplicate/reordered pages or blocks; next page is normal."""
    return (
        tuple(read_ordinals) == tuple(range(required_count))
        and tuple(pages) == tuple(range((required_count + page_size - 1) // page_size))
        and tuple(chunks) == tuple(expected_chunks)
    )


def validate_receipt_bundle(frame, receipt, policy, bundle):
    """Bind actual entries to the frozen plan, independently of read progress.

    Only sealed metadata is needed, so the same check can guard M3 without
    reading history again or changing the bundle. Empty inputs have no spans,
    but still require their read ordinal and page in the receipt.
    """
    required = required_manifest(frame, receipt.scope)
    if receipt.required != required:
        raise ContractError("required_manifest_mismatch")
    if (
        bundle.maintenance != frame.snapshot
        or bundle.scope_id != receipt.scope
        or not bundle.complete
        or bundle.truncated
        or bundle.next_cursor is not None
        or bundle.issues
        or bundle.page_size != policy.page_size
        or bundle.chunk_size != policy.chunk_size
        or bundle.ordering != EVIDENCE_ORDERING
    ):
        raise ContractError("bundle_reference_mismatch")
    slots = {(s.source, s.content_level): s for s in frame.history}
    expected_entries = []
    expected_chunks = []
    for ordinal, item in enumerate(required):
        slot = slots.get((item.source, item.content_level or policy.content_level))
        if slot is None:
            raise ContractError("required_source_missing")
        for chunk, (a, b) in enumerate(
            chunks_for(slot.length, item.start, item.end, policy.chunk_size)
        ):
            expected_chunks.append((ordinal, a, b))
            expected_entries.append(
                (
                    len(expected_entries),
                    ordinal // policy.page_size,
                    chunk,
                    slot.seal,
                    a,
                    b,
                    (item.role,),
                )
            )
    if not check_progress(
        len(required),
        policy.page_size,
        receipt.read_ordinals,
        receipt.pages,
        expected_chunks,
        receipt.chunks,
    ):
        raise ContractError("incomplete_manifest")
    # S2 validates excerpt text/hash against the exact source at bundle seal.
    # Here BOTH lists must match the independent frozen layout, not each other
    # alone: two equally damaged lists are not a completeness certificate.
    for entries in (bundle.required, bundle.read):
        actual = [
            (
                e.ordinal,
                e.page,
                e.chunk,
                e.evidence.source,
                e.evidence.start,
                e.evidence.end,
                e.evidence.provenance,
            )
            for e in entries
        ]
        if actual != expected_entries:
            raise ContractError("bundle_manifest_mismatch")
    if bundle.required != bundle.read:
        raise ContractError("bundle_manifest_mismatch")


def read_error(exc):
    if isinstance(exc, ContractError):
        return exc.code
    if isinstance(exc, (TimeoutError, asyncpg.QueryCanceledError)):
        return "timeout"
    if isinstance(exc, (ConnectionError, asyncpg.PostgresConnectionError)):
        return "connection"
    if isinstance(exc, asyncpg.SerializationError):
        return "serialization"
    return "read_failed"


class EvidenceBuilder:
    def __init__(self, executor):
        self.executor = executor
        self.repo = executor.repo
        self.artifacts = executor.artifacts
        self.evidence = EvidenceService(self.artifacts)

    async def _read_once(self, db, frame, watch, slot):
        # Repeated on EVERY retry, before body access (including metadata recovery).
        if slot not in frame.history or slot.ordinal > frame.cutoff_ordinal:
            raise ContractError("source_outside_scope")
        source = await self.artifacts.get(db, slot.seal)
        if (
            source.source != slot.source
            or source.content_level != slot.content_level
            or source.content_sha256 != slot.content_sha256
        ):
            raise ContractError("source_version_mismatch")
        if (
            source.ingested_at != slot.ingested_at
            or source.ingested_at > frame.cutoff_at
        ):
            raise ContractError("source_after_cutoff")
        row = await db.fetchrow(
            "SELECT uri FROM contexts WHERE id=$1", slot.source.context_id
        )
        if row is None:
            raise ContractError("source_version_missing")
        decision = await self.executor.acl.check_read_access(
            db,
            row["uri"],
            RequestContext(account_id=frame.account_id, agent_id=watch.agent_id),
        )
        if not decision.allowed or decision.field_masks:
            raise ContractError("source_permission_denied")
        text = await self.evidence.read_source(db, source)
        if len(text) != slot.length or text_hash(text) != slot.content_sha256:
            raise ContractError("source_version_mismatch")
        return text

    async def _read(self, frame, watch, slot, policy):
        ident = identity(
            frame.account_id,
            frame.execution_id,
            frame.event_id,
            policy,
            "read-evidence",
            frame.snapshot.id,
        )
        operation = LocalOperation(
            self.executor.recorder, ident, slot.model_dump(mode="json")
        )
        for attempt in range(1, policy.retry.max_attempts + 1):
            common = {
                **operation.common,
                "attempt_no": attempt,
                "started_at": datetime.now(timezone.utc),
                "retry_policy_sha256": canonical_hash(
                    {
                        "retry": policy.retry.model_dump(mode="json"),
                        "codes": list(policy.retry_read_codes),
                    }
                ),
            }
            await self.executor.recorder.append(
                AttemptRecord(**common, phase="started", outcome="started")
            )
            tick = monotonic()
            operation.counts = {}
            value = None
            code = None
            try:
                remaining = (
                    frame.deadline_at - datetime.now(timezone.utc)
                ).total_seconds()
                if remaining <= 0:
                    raise ContractError("deadline_exhausted")
                async with asyncio.timeout(
                    min(remaining, policy.retry.timeout_seconds)
                ):
                    async with self.repo.session(frame.account_id) as raw:
                        value = await self._read_once(
                            MeteredDB(raw, operation), frame, watch, slot
                        )
            except Exception as exc:
                code = read_error(exc)
            retryable = code in policy.retry_read_codes if code else False
            retry = bool(
                retryable
                and attempt < policy.retry.max_attempts
                and datetime.now(timezone.utc) < frame.deadline_at
            )
            stop = (
                "success"
                if not code
                else "retry_scheduled"
                if retry
                else "retry_exhausted"
                if retryable
                else "non_retryable"
            )
            await self.executor.recorder.append(
                AttemptRecord(
                    **common,
                    phase="finished",
                    outcome="error" if code else "success",
                    finished_at=datetime.now(timezone.utc),
                    wall_seconds=monotonic() - tick,
                    stop_reason=stop,
                    output_sha256=text_hash(value)
                    if value is not None
                    else canonical_hash({"code": code}),
                    error=AttemptError(
                        code=code, error_class="EvidenceRead", retryable=retryable
                    )
                    if code
                    else None,
                    local_work=tuple(
                        LocalWork(metric=k, count=v)
                        for k, v in sorted(operation.counts.items())
                    ),
                )
            )
            if not code:
                return value
            if not retry:
                raise ContractError(code)
            # One retry owner. No caller loop and no transaction during backoff.
            await asyncio.sleep(
                min(
                    policy.retry.backoff_seconds[attempt - 1],
                    max(
                        0,
                        (
                            frame.deadline_at - datetime.now(timezone.utc)
                        ).total_seconds(),
                    ),
                )
            )
        raise ContractError("retry_exhausted")

    async def build_evidence(self, frame_ref, scope, *, local_sources=None):
        """Returns a sealed receipt with the pre-read required slots and bundle ref.

        A missing required source has no fabricated EvidenceRef. In that case the
        receipt retains the slot and partial progress; no complete bundle exists.
        local_sources is an explicit M1 selection to audit against known inputs.
        """
        async with self.repo.session(frame_ref.account_id) as db:
            frame = await self.artifacts.get(db, frame_ref)
            watch = await self.artifacts.get(db, frame.watch)
            policy = MaintenancePolicy.model_validate_json(watch.policy_json)
            initial_reasons = await self.executor.recheck(db, frame)
        required = required_manifest(frame, scope)
        work = LocalOperation(
            self.executor.recorder,
            identity(
                frame.account_id,
                frame.execution_id,
                frame.event_id,
                policy,
                "build-evidence",
                frame.snapshot.id,
            ),
            {
                "frame": frame_ref.model_dump(mode="json"),
                "scope": scope,
                "required": [r.model_dump(mode="json") for r in required],
            },
        )
        await work.start()
        reasons = list(initial_reasons)
        slots = {(s.source, s.content_level): s for s in frame.history}
        pages = []
        read = []
        entries = []
        chunks = []
        expected_chunks = []
        omitted = (
            scope == "M1"
            and local_sources is not None
            and any(r.source not in local_sources for r in required)
        )
        if omitted:
            reasons.append("local_manifest_omits_known_source")
        for ordinal, item in enumerate(required):
            slot = slots.get((item.source, item.content_level or policy.content_level))
            if slot is None:
                reasons.append("required_source_missing_or_forbidden")
                continue
            try:
                expected_chunks.extend(
                    (ordinal, a, b)
                    for a, b in chunks_for(
                        slot.length, item.start, item.end, policy.chunk_size
                    )
                )
            except ContractError as exc:
                reasons.append(exc.code)
        hard_initial = any(
            r in reasons
            for r in (
                "version_drift",
                "cause_drift",
                "required_registry_drift",
                "provenance_drift",
                "graph_basis_version_drift",
                "deadline_exhausted",
                "source_permission_denied",
                "required_source_missing_or_forbidden",
                "source_span_mismatch",
            )
        )
        if not omitted and not hard_initial:
            for offset in range(0, len(required), policy.page_size):
                page = offset // policy.page_size
                work.add("retrieval_pages")
                page_ok = True
                for ordinal in range(
                    offset, min(offset + policy.page_size, len(required))
                ):
                    item = required[ordinal]
                    slot = slots[
                        (item.source, item.content_level or policy.content_level)
                    ]
                    try:
                        text = await self._read(frame, watch, slot, policy)
                        spans = chunks_for(
                            slot.length, item.start, item.end, policy.chunk_size
                        )
                        for chunk, (a, b) in enumerate(spans):
                            ref = EvidenceRef(
                                source=slot.seal,
                                start=a,
                                end=b,
                                excerpt=text[a:b],
                                excerpt_sha256=text_hash(text[a:b]),
                                provenance=(item.role,),
                            )
                            entries.append(
                                ReadEntry(
                                    evidence=ref,
                                    ordinal=len(entries),
                                    page=page,
                                    chunk=chunk,
                                )
                            )
                            chunks.append((ordinal, a, b))
                        read.append(ordinal)
                    except ContractError as exc:
                        reasons.append(exc.code)
                        page_ok = False
                        break
                if page_ok:
                    pages.append(page)
                else:
                    break
        complete = check_progress(
            len(required), policy.page_size, read, pages, expected_chunks, chunks
        )
        try:
            async with self.repo.session(frame.account_id) as raw:
                db = MeteredDB(raw, work)
                reasons.extend(await self.executor.recheck(db, frame))
                bundle_ref = None
                # Complete is declared-scope processing, independent of upstream readiness.
                if complete:
                    bundle = EvidenceBundle(
                        account_id=frame.account_id,
                        id=uuid4(),
                        maintenance=frame.snapshot,
                        scope_id=scope,
                        required=tuple(entries),
                        read=tuple(entries),
                        complete=True,
                        truncated=False,
                        ordering=EVIDENCE_ORDERING,
                        page_size=policy.page_size,
                        chunk_size=policy.chunk_size,
                    )
                    bundle_ref = await self.artifacts.seal(db, bundle)
                reasons = tuple(dict.fromkeys(reasons))
                receipt = EvidenceReceipt(
                    account_id=frame.account_id,
                    id=uuid4(),
                    frame=frame_ref,
                    scope=scope,
                    required=required,
                    read_ordinals=tuple(read),
                    pages=tuple(pages),
                    chunks=tuple(chunks),
                    complete=complete,
                    ready=complete and not reasons,
                    reasons=reasons,
                    bundle=bundle_ref,
                )
                await self.artifacts.seal(db, receipt)
                if reasons and reasons != ("local_manifest_omits_known_source",):
                    await self.executor.stop_work(db, frame, reasons[0])
                await trace(
                    db, frame.event_id, "s5_evidence", receipt.model_dump(mode="json")
                )
            await work.finish(receipt.model_dump(mode="json"))
            return receipt
        except Exception as exc:
            await work.finish({}, read_error(exc))
            raise

    async def reuse_m2_for_m3(self, receipt_ref):
        """An input handoff only; no generation, reframing, rechunking or source read."""
        async with self.repo.session(receipt_ref.account_id) as db:
            receipt = await self.artifacts.get(db, receipt_ref)
            if (
                not isinstance(receipt, EvidenceReceipt)
                or receipt.scope != "M2"
                or not receipt.complete
                or receipt.bundle is None
            ):
                raise ContractError("complete_m2_required")
            # Recheck persisted receipts too: older seals may predate the
            # cross-artifact completeness check. Preserve them as audit data,
            # but do not hand a damaged bundle to the next stage.
            await self.artifacts.validate_maintenance_receipt(db, receipt)
            return receipt.bundle
