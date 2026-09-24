"""Legacy S4 synthetic reproduction; new service wiring is PublicRQ1Runner.

No scorer imports, questions, labels, model defaults, retries or best-run selection.
"""

import asyncio
import httpx
from datetime import datetime, timezone
from decimal import Decimal
from time import monotonic
from uuid import UUID, uuid5
from typing import Literal
from pydantic import Field, model_validator
from contexthub.discovery.extraction import (
    ExtractionConfig,
    ExtractionService,
    error_code,
)
from contexthub.discovery.adjudication import AdjudicationConfig, AdjudicationService
from contexthub.discovery.search import (
    SearchService,
    SearchPolicy,
    HistorySnapshot,
    HistoryEntry,
)
from contexthub.discovery.service import (
    DiscoveryConfig,
    DiscoveryService,
    assemble_graph_snapshot,
)
from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.models.execution import CallIdentity, AttemptRecord, PriceBasis
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    SealRef,
    ContextVersion,
    Name,
    Positive,
    Nonnegative,
    Hash,
    canonical_hash,
)
from contexthub.services.evidence_service import EvidenceService
from contexthub.services.artifact_service import ArtifactService
from .schemas import MaintenanceInput
from .public_rq1_runner import PublicRQ1Runner, PublicRQ1Configuration


class RQ1Config(Contract):
    extraction: ExtractionConfig
    extraction_price: PriceBasis | None
    adjudication: AdjudicationConfig
    selective: SearchPolicy
    full_history: SearchPolicy
    batch_size: Positive
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    discovery_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    code_version: Name
    # Execution gate: this stage exposes synthetic execution only. Real runs
    # require a later authorized entry point with S1 preflight and run manifest.
    mode: Literal["synthetic"]

    @model_validator(mode="after")
    def fairness(self):
        a = self.selective.model_dump(exclude={"strategy", "id"})
        b = self.full_history.model_dump(exclude={"strategy", "id"})
        if (
            a != b
            or self.selective.strategy != "selective"
            or self.full_history.strategy != "full-history"
        ):
            raise ContractError("arms_must_share_search_parameters")
        prices = (
            self.extraction_price,
            self.adjudication.weak_price,
            self.adjudication.strong_price,
        )
        if len({p.currency for p in prices if p is not None}) > 1:
            raise ContractError("mixed_cost_currency")
        if self.adjudication.lambda_p1 != self.selective.lambda_p1:
            raise ContractError("price_config_mismatch")
        return self


class CostSummary(Contract):
    model_attempts: Nonnegative
    incomplete_attempts: Nonnegative
    input_tokens_known: Nonnegative
    output_tokens_known: Nonnegative
    unknown_usage_attempts: Nonnegative
    known_model_amount: Decimal
    unknown_model_cost_attempts: Nonnegative
    billed_total: Decimal | None
    billed_status: Literal["unknown", "exact"]
    currency: str | None
    unpriced_local_work: bool
    local_work: dict[str, int]
    wall_seconds: float = Field(ge=0)
    record_sha256: Hash


def summarize(records, wall):
    records = tuple(records)
    finished = [r for r in records if r.phase == "finished"]
    model = [r for r in finished if r.requested_model is not None]
    incomplete = len([r for r in records if r.phase == "started"]) - len(finished)
    currencies = {r.cost.currency for r in model if r.cost.currency}
    local = {}
    for r in finished:
        for w in r.local_work:
            local[w.metric] = local.get(w.metric, 0) + w.count
    unknown = sum(r.cost.status == "unknown" for r in model)
    amount = sum(
        (r.cost.amount for r in model if r.cost.amount is not None), Decimal(0)
    )
    unpriced = any(r.requested_model is None for r in finished)
    exact = not unknown and not incomplete and not unpriced and len(currencies) <= 1
    return CostSummary(
        model_attempts=len(model),
        incomplete_attempts=incomplete,
        input_tokens_known=sum(r.usage.input_tokens or 0 for r in model),
        output_tokens_known=sum(r.usage.output_tokens or 0 for r in model),
        unknown_usage_attempts=sum(r.usage.completeness != "complete" for r in model),
        known_model_amount=amount,
        unknown_model_cost_attempts=unknown,
        billed_total=amount if exact else None,
        billed_status="exact" if exact else "unknown",
        currency=next(iter(currencies)) if len(currencies) == 1 else None,
        unpriced_local_work=unpriced,
        local_work=local,
        wall_seconds=wall,
        record_sha256=canonical_hash([r.model_dump(mode="json") for r in records]),
    )


class RunRecorder:
    """Per-run append observer, forwarding each event once to the S2 ledger."""

    def __init__(self, ledger):
        self.ledger, self.rows = ledger, []

    async def append(self, record):
        await self.ledger.append(record)
        self.rows.append(record)


class ArmResult(Contract):
    strategy: Literal["selective", "full-history"]
    graph: SealRef | None
    reports: tuple[SealRef, ...]
    status: Literal["complete", "pending", "error"]
    reasons: tuple[Name, ...]
    cost: CostSummary


class RQ1Result(Contract):
    run_id: UUID
    replicate_id: Nonnegative
    input_sha256: Hash
    config_sha256: Hash
    shared_extractions: tuple[SealRef, ...]
    source_map: tuple[tuple[str, SealRef], ...]
    arms: tuple[ArmResult, ...]
    actual_batch_cost: CostSummary
    records: tuple[AttemptRecord, ...]


class RQ1Runner:
    def __init__(self, config, *, repository, ledger, api_key, transport=None):
        self.config, self.repository, self.ledger = config, repository, ledger
        self.api_key, self.transport = api_key, transport
        if config.mode == "synthetic" and not isinstance(
            transport, httpx.MockTransport
        ):
            raise ContractError("synthetic_transport_required")

    async def run(
        self,
        runtime: MaintenanceInput,
        *,
        account_id,
        run_id: UUID,
        replicate_id: int,
        cutoff_ordinal: int,
    ):
        # Exact DTO type rejects privileged objects/subclasses before any work.
        if type(runtime) is not MaintenanceInput:
            raise ContractError("maintenance_input_required")
        runtime = MaintenanceInput.model_validate(runtime.model_dump(mode="json"))
        if (
            cutoff_ordinal < 0
            or cutoff_ordinal >= len(runtime.sources)
            or replicate_id < 0
        ):
            raise ContractError("invalid_run_boundary")
        if any(s.source_version != 1 for s in runtime.sources[: cutoff_ordinal + 1]):
            raise ContractError("initial_import_requires_source_version_one")
        cfg = self.config
        sink = RunRecorder(self.ledger)
        started = monotonic()
        shared_execution = uuid5(run_id, "shared")
        shared_wall = 0
        active_strategy = None
        shared = []
        source_map = []
        histories = []
        arm_reports = {s: [] for s in ("selective", "full-history")}
        arm_errors = {s: [] for s in arm_reports}
        arm_wall = {s: 0.0 for s in arm_reports}

        def identity(execution, strategy, operation, config):
            return CallIdentity(
                account_id=account_id,
                execution_id=execution,
                call_id=uuid5(execution, operation),
                strategy=strategy,
                stage="P1",
                operation=operation,
                model_role="local",
                code_version=cfg.code_version,
                config_sha256=canonical_hash(config),
            )

        extractor = ExtractionService(
            cfg.extraction,
            api_key=self.api_key,
            recorder=sink,
            transport=self.transport,
            price=cfg.extraction_price,
        )
        adjudicator = AdjudicationService(
            cfg.adjudication,
            api_key=self.api_key,
            recorder=sink,
            transport=self.transport,
        )
        try:
            async with asyncio.timeout(cfg.timeout_seconds):
                sources = runtime.sources[: cutoff_ordinal + 1]
                for offset in range(0, len(sources), cfg.batch_size):
                    tick = monotonic()
                    refs = []
                    ident = identity(
                        shared_execution, "shared", "ingest-" + str(offset), cfg
                    )
                    work = LocalOperation(
                        sink, ident, {"input": runtime.input_sha256, "offset": offset}
                    )
                    await work.start()
                    try:
                        async with self.repository.session(account_id) as raw_db:
                            db = MeteredDB(raw_db, work)
                            for source in sources[offset : offset + cfg.batch_size]:
                                # Keep exact messages as JSON; source metadata contains
                                # opaque IDs and ordinal only, never MEME task fields.
                                import json

                                text = json.dumps(
                                    [
                                        m.model_dump(mode="json")
                                        for m in source.messages
                                    ],
                                    ensure_ascii=False,
                                )
                                node_id = uuid5(run_id, "source:" + source.source_id)
                                await db.execute(
                                    """INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content)
                                    VALUES($1,$2,$3,'memory','user',$4)""",
                                    node_id,
                                    account_id,
                                    "ctx://rq1/" + str(node_id),
                                    text,
                                )
                                ref = await EvidenceService().snapshot(
                                    db,
                                    account_id=account_id,
                                    source=ContextVersion(
                                        context_id=node_id, version=1
                                    ),
                                    content_level="l2_content",
                                    cutoff_at=datetime.now(timezone.utc),
                                )
                                source_map.append((source.source_id, ref))
                                refs.append(ref)
                                histories.append(
                                    HistoryEntry(
                                        source=ref, ingestion_ordinal=source.ordinal
                                    )
                                )
                        await work.finish(
                            {"sources": [r.model_dump(mode="json") for r in refs]}
                        )
                    except Exception as exc:
                        await work.finish({"offset": offset}, error_code(exc))
                        raise
                    async with self.repository.session(account_id) as db:
                        artifact = await extractor.extract(
                            db,
                            tuple(refs),
                            identity=identity(
                                shared_execution,
                                "shared",
                                "extract-" + str(offset),
                                cfg.extraction,
                            ),
                            artifact_id=uuid5(run_id, "extraction-" + str(offset)),
                        )
                    previous = tuple(shared)
                    shared.append(artifact.reference())
                    shared_wall += monotonic() - tick
                    hist = HistorySnapshot(
                        account_id=account_id,
                        entries=tuple(histories),
                        cutoff_at=datetime.now(timezone.utc),
                        cutoff_ordinal=histories[-1].ingestion_ordinal,
                    )
                    for strategy, policy in (
                        ("selective", cfg.selective),
                        ("full-history", cfg.full_history),
                    ):
                        if arm_errors[strategy]:
                            continue
                        tick = monotonic()
                        active_strategy = strategy
                        core_cfg = DiscoveryConfig(
                            search=policy,
                            adjudication=cfg.adjudication,
                            timeout_seconds=cfg.discovery_timeout_seconds,
                        )
                        core = DiscoveryService(
                            core_cfg,
                            search=SearchService(recorder=sink),
                            adjudicator=adjudicator,
                            recorder=sink,
                        )
                        try:
                            async with self.repository.session(account_id) as db:
                                report = await core.discover(
                                    db,
                                    artifact.reference(),
                                    hist,
                                    identity=identity(
                                        uuid5(run_id, strategy),
                                        strategy,
                                        "discover-" + str(offset),
                                        core_cfg,
                                    ),
                                    report_id=uuid5(
                                        run_id, strategy + "-report-" + str(offset)
                                    ),
                                    prior_extractions=previous,
                                )
                            arm_reports[strategy].append(report)
                        except Exception as exc:
                            arm_errors[strategy].append(error_code(exc))
                        arm_wall[strategy] += monotonic() - tick
                        active_strategy = None

        except Exception as exc:
            if active_strategy is None:
                shared_wall += max(0, monotonic() - tick)
            else:
                arm_wall[active_strategy] += max(0, monotonic() - tick)
            for strategy in arm_errors:
                arm_errors[strategy].append(error_code(exc))
        finally:
            await extractor.close()
            await adjudicator.close()
        arms = []
        for strategy, reports in arm_reports.items():
            tick = monotonic()
            graph_ref = None
            reasons = list(arm_errors[strategy])
            if not reasons:
                ident = identity(
                    uuid5(run_id, strategy), strategy, "seal-run-graph", cfg
                )
                work = LocalOperation(
                    sink,
                    ident,
                    {
                        "reports": [
                            r.reference().model_dump(mode="json") for r in reports
                        ]
                    },
                )
                await work.start()
                try:
                    async with self.repository.session(account_id) as raw_db:
                        db = MeteredDB(raw_db, work)
                        artifacts = ArtifactService()
                        graphs = [await artifacts.get(db, r.graph) for r in reports]
                        graph = assemble_graph_snapshot(
                            graphs,
                            account_id=account_id,
                            graph_id=uuid5(run_id, strategy + "-graph"),
                            config_sha256=canonical_hash(cfg),
                        )
                        graph_ref = await artifacts.seal(db, graph)
                    work.add("audit_records")
                    await work.finish(graph)
                except Exception as exc:
                    reasons.append(error_code(exc))
                    await work.finish({}, error_code(exc))
            arm_wall[strategy] += monotonic() - tick
            status = (
                "error"
                if reasons
                else "pending"
                if any(r.status == "pending" for r in reports)
                else "complete"
            )
            reasons.extend(reason for r in reports for reason in r.reasons)
            records = [
                r
                for r in sink.rows
                if r.identity.execution_id
                in (shared_execution, uuid5(run_id, strategy))
            ]
            arms.append(
                ArmResult(
                    strategy=strategy,
                    graph=graph_ref,
                    reports=tuple(r.reference() for r in reports),
                    status=status,
                    reasons=tuple(dict.fromkeys(reasons)),
                    cost=summarize(records, shared_wall + arm_wall[strategy]),
                )
            )
        return RQ1Result(
            run_id=run_id,
            replicate_id=replicate_id,
            input_sha256=runtime.input_sha256,
            config_sha256=canonical_hash(cfg),
            shared_extractions=tuple(shared),
            source_map=tuple(source_map),
            arms=tuple(arms),
            actual_batch_cost=summarize(sink.rows, monotonic() - started),
            records=tuple(sink.rows),
        )
