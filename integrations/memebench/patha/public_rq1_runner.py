"""Public-service RQ1 orchestration; legacy S4 runner remains reproducible.

Raw session messages are stored intact as JSON. The core owns extraction,
discovery, source versions, watches and all attempt accounting.
"""

import json
from time import monotonic
from uuid import UUID, uuid5
from pydantic import model_validator
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    SealRef,
    Hash,
    canonical_hash,
)
from contexthub.models.request import RequestContext
from contexthub.models.knowledge_phases import (
    PrepareRequest,
    DiscoverPreparedRequest,
    FinalizeGraphRequest,
)
from contexthub.models.execution import AttemptRecord
from .schemas import MaintenanceInput
from .system_factory import SystemConfiguration, create_system, preflight_system
from .database_snapshot import DatabaseSnapshot
from .run_isolation import require_empty_run_account
from .rq1_scorer import GraphEvidence, load_graph_evidence
from contexthub.services.knowledge_service import safe_reason


class PublicRQ1Configuration(Contract):
    selective: SystemConfiguration
    full_history: SystemConfiguration

    @model_validator(mode="after")
    def fair(self):
        if (
            self.selective.mode != self.full_history.mode
            or self.selective.manifest != self.full_history.manifest
            or self.selective.authorization != self.full_history.authorization
        ):
            raise ContractError("rq1_run_identity_mismatch")
        a = self.selective.knowledge.model_dump(mode="json")
        b = self.full_history.knowledge.model_dump(mode="json")
        if (
            a["discovery"]["search"]["strategy"] != "selective"
            or b["discovery"]["search"]["strategy"] != "full-history"
        ):
            raise ContractError("rq1_two_strategies_required")
        for config in (a, b):
            config.pop("id")
            config["discovery"]["search"].pop("id")
            config["discovery"]["search"].pop("strategy")
        if a != b:
            raise ContractError("rq1_frozen_conditions_mismatch")
        return self


class PublicRQ1Result(Contract):
    run_id: UUID
    replicate_id: int
    input_sha256: Hash
    configuration_sha256: Hash
    prepared: tuple[SealRef, ...]
    shared_extractions: tuple[SealRef, ...]
    source_map: tuple[tuple[str, SealRef], ...]
    handoffs: tuple[SealRef | None, ...]
    selective_snapshot: DatabaseSnapshot | None
    shared_operations: tuple[UUID, ...]
    arm_operations: tuple[tuple[UUID, ...], ...]
    records: tuple[AttemptRecord, ...]
    graph_evidence: tuple[GraphEvidence | None, ...] = ()
    arm_errors: tuple[str | None, ...] = ()
    shared_wall_seconds: float | None = None
    arm_wall_seconds: tuple[float, ...] = ()


class PublicRQ1Runner:
    def __init__(
        self, config, *, repository, context_service, secrets, transport, rules=()
    ):
        self.config = PublicRQ1Configuration.model_validate(
            config.model_dump(mode="json")
        )
        for arm in (self.config.selective, self.config.full_history):
            preflight_system(arm, transport=transport, stage="rq1")
        self.dependencies = dict(
            repository=repository,
            context_service=context_service,
            secrets=secrets,
            transport=transport,
            rules=rules,
        )

    async def run(
        self,
        runtime,
        *,
        account_id,
        agent_id,
        run_id,
        replicate_id,
        cutoff_ordinal,
        seal_selective=None,
    ):
        if type(runtime) is not MaintenanceInput:
            raise ContractError("maintenance_input_required")
        runtime = MaintenanceInput.model_validate(runtime.model_dump(mode="json"))
        if (
            type(replicate_id) is not int
            or replicate_id < 0
            or type(cutoff_ordinal) is not int
            or not 0 <= cutoff_ordinal < len(runtime.sources)
        ):
            raise ContractError("rq1_invalid_run_boundary")
        if replicate_id == 0 and seal_selective is None:
            raise ContractError("selective_main_snapshot_required")
        if self.config.selective.mode != "synthetic":
            manifest = self.config.selective.manifest
            if (
                run_id != manifest.run_id
                or runtime.episode_id not in manifest.episode_ids
                or replicate_id >= manifest.statistics.repeats
            ):
                raise ContractError("rq1_run_identity_mismatch")
        await require_empty_run_account(self.dependencies["repository"], account_id)
        ctx = RequestContext(account_id=account_id, agent_id=agent_id)
        namespace = uuid5(run_id, f"replicate:{replicate_id}")
        prepared, shared, source_map, shared_operations = [], [], [], []
        handoffs, arm_operations, graph_evidence, arm_wall = [], [], [], []
        snapshot = None
        arm_errors = []
        # Full-history runs only AFTER the selective before-state was captured.
        selective = await create_system(self.config.selective, **self.dependencies)
        try:
            started = monotonic()
            previous = None
            for source in runtime.sources[: cutoff_ordinal + 1]:
                raw = json.dumps(
                    [m.model_dump(mode="json") for m in source.messages],
                    ensure_ascii=False,
                )
                result = await selective.prepare(
                    PrepareRequest(
                        request_id=uuid5(namespace, "prepare:" + source.source_id),
                        content=raw,
                        previous=previous,
                    ),
                    ctx,
                )
                shared_operations.append(result.operation_id)
                if result.status != "complete" or result.prepared is None:
                    raise ContractError("rq1_preparation_incomplete")
                previous = result.prepared
                handle = await selective.get_prepared(previous, ctx)
                prepared.append(previous)
                shared.append(handle.extraction)
                source_map.append((source.source_id, handle.history.entries[-1].source))
            shared_wall = monotonic() - started
            for name, configuration in (
                ("selective", self.config.selective),
                ("full-history", self.config.full_history),
            ):
                service = (
                    selective
                    if name == "selective"
                    else await create_system(configuration, **self.dependencies)
                )
                position = len(handoffs)
                try:
                    started = monotonic()
                    reports, operations = [], []
                    for i, ref in enumerate(prepared):
                        result = await service.discover_prepared(
                            DiscoverPreparedRequest(
                                request_id=uuid5(namespace, f"{name}:discover:{i}"),
                                prepared=ref,
                            ),
                            ctx,
                        )
                        operations.append(result.operation_id)
                        if result.report_id is None:
                            raise ContractError("rq1_discovery_incomplete")
                        # Public report lookup is tied to the completed operation.
                        reports.append(
                            await service.discovery_report_reference(
                                result.operation_id, ctx
                            )
                        )
                    result = await service.finalize_graph(
                        FinalizeGraphRequest(
                            request_id=uuid5(namespace, f"{name}:finalize"),
                            reports=tuple(reports),
                            prepared=tuple(prepared),
                        ),
                        ctx,
                    )
                    operations.append(result.operation_id)
                    handoff = await service.get_graph_handoff(result.operation_id, ctx)
                    handoffs.append(handoff.reference())
                    arm_operations.append(tuple(operations))
                    arm_wall.append(monotonic() - started)
                    async with self.dependencies["repository"].session(
                        account_id
                    ) as db:
                        graph_evidence.append(
                            await load_graph_evidence(
                                db,
                                graph_ref=handoff.graph,
                                report_refs=tuple(reports),
                                source_map=tuple(source_map),
                                status="complete",
                            )
                        )
                    if name == "selective" and replicate_id == 0:
                        snapshot = await seal_selective(handoff, service.config_hash)
                        if (
                            not isinstance(snapshot, DatabaseSnapshot)
                            or snapshot.run_id != str(run_id)
                            or snapshot.handoff != handoff.reference()
                            or snapshot.graph != handoff.graph
                            or snapshot.account_id != account_id
                            or snapshot.agent_id != agent_id
                            or snapshot.configuration_sha256 != service.config_hash
                        ):
                            raise ContractError("selective_snapshot_identity_mismatch")
                    arm_errors.append(None)
                except Exception as exc:
                    if name != "full-history":
                        raise
                    # A failed control cannot erase or replace the already
                    # sealed selective main. Preserve partial costs and a
                    # missing graph for the control's denominator rows.
                    handoffs[position:] = [None]
                    graph_evidence[position:] = [None]
                    arm_operations[position:] = [tuple(operations)]
                    arm_wall[position:] = [monotonic() - started]
                    arm_errors.append(safe_reason(exc))
                finally:
                    if service is not selective:
                        await service.close()
            records = await selective.execution_records(
                (*shared_operations, *(op for arm in arm_operations for op in arm)), ctx
            )
            return PublicRQ1Result(
                run_id=run_id,
                replicate_id=replicate_id,
                input_sha256=runtime.input_sha256,
                configuration_sha256=canonical_hash(self.config),
                prepared=tuple(prepared),
                shared_extractions=tuple(shared),
                source_map=tuple(source_map),
                handoffs=tuple(handoffs),
                selective_snapshot=snapshot,
                shared_operations=tuple(shared_operations),
                arm_operations=tuple(arm_operations),
                records=records,
                graph_evidence=tuple(graph_evidence),
                arm_errors=tuple(arm_errors),
                shared_wall_seconds=shared_wall,
                arm_wall_seconds=tuple(arm_wall),
            )
        finally:
            await selective.close()
