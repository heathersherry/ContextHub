"""S5: atomic protection then short-transaction preparation; no candidate executor."""

import json
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4, uuid5

from contexthub.discovery.contracts import encode_configuration
from contexthub.discovery.service import read_graph_handoff
from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.models.execution import CallIdentity
from contexthub.models.knowledge import (
    ContextVersion,
    ContractError,
    SourceSnapshot,
    SealRef,
    canonical_hash,
)
from contexthub.models.maintenance import MaintenanceSnapshot, WorkState
from contexthub.models.request import RequestContext
from contexthub.propagation.contracts import (
    MaintenancePolicy,
    MaintenanceWatch,
    MaintenanceFrame,
    InputRegistration,
    RequiredInput,
    SourceSlot,
)
from contexthub.services.artifact_service import ArtifactService, require_account
from contexthub.services.acl_service import ACLService
from contexthub.services.execution_ledger import ExecutionLedger


def affected_nodes(graph, source, seeds, downstream):
    """Mapping denotes source carriers, not edges. Pending adds protection only."""
    roots = set(seeds.get(source, ()))
    if source in graph.nodes:
        roots.add(source)
    seen, todo = set(roots), list(roots)
    while todo:
        node = todo.pop()
        possible = tuple(p.downstream for p in graph.pending if p.upstream == node)
        for child in (*downstream.get(node, ()), *possible):
            if child not in seen:
                seen.add(child)
                todo.append(child)
    return tuple(sorted(roots, key=lambda n: str(n.context_id))), tuple(
        sorted(seen - {source}, key=lambda n: str(n.context_id))
    )


def identity(account, execution, event, policy, operation, work=None):
    return CallIdentity(
        account_id=account,
        execution_id=execution,
        call_id=uuid4(),
        event_id=event,
        work_item_id=work,
        strategy="stale-first",
        stage="S5",
        operation=operation,
        model_role="none",
        code_version="s5-v1",
        config_sha256=canonical_hash(policy),
    )


async def trace(db, event, kind, payload):
    await db.execute(
        "INSERT INTO propagation_trace(event_id,trace_type,payload) VALUES($1,$2,$3)",
        event,
        kind,
        payload,
    )


async def causes(db, target):
    return tuple(
        r["cause_event_id"]
        for r in await db.fetch(
            """SELECT i.cause_event_id FROM context_invalidations i JOIN contexts c ON c.id=i.context_id
           WHERE c.account_id=current_setting('app.account_id') AND i.context_id=$1
           AND i.resolved_at IS NULL ORDER BY i.cause_event_id""",
            target,
        )
    )


class MaintenanceExecutor:
    def __init__(self, repository, *, recorder=None, acl=None):
        self.repo = repository
        self.artifacts = ArtifactService()
        self.recorder = recorder or ExecutionLedger(repository)
        self.acl = acl or ACLService()

    async def register_graph(
        self, db, report_ref, policy: MaintenancePolicy, ctx: RequestContext
    ):
        """Explicit opt-in BEFORE changes. Lock sources to serialize activation/write."""
        await require_account(db, ctx.account_id)
        report, graph, seeds, downstream = await read_graph_handoff(db, report_ref)
        report.configuration_payload()
        sources = set(seeds) | set(graph.nodes)
        result = []
        for source in sorted(sources, key=lambda n: str(n.context_id)):
            row = await db.fetchrow(
                "SELECT version FROM contexts WHERE id=$1 FOR UPDATE", source.context_id
            )
            if row is None or row["version"] != source.version:
                raise ContractError("registration_version_drift")
            roots, affected = affected_nodes(graph, source, seeds, downstream)
            if not affected:
                continue
            watch = MaintenanceWatch(
                account_id=ctx.account_id,
                id=uuid5(report.id, "s5-watch:" + str(source.context_id)),
                report=report_ref,
                graph=graph.reference(),
                source=source,
                seeds=roots,
                affected=affected,
                reasons=("graph_pending",)
                if report.status != "complete" or graph.pending
                else (),
                policy_json=encode_configuration(policy)["payload"],
                policy_sha256=canonical_hash(policy),
                agent_id=ctx.agent_id,
            )
            result.append(await self.artifacts.seal(db, watch))
        return tuple(result)

    async def register_inputs(self, db, registration: InputRegistration):
        # Rule inputs require a registered ID/version; no executable rule runs here.
        return await self.artifacts.seal(db, registration)

    async def register_rule_inputs(self, db, *, account_id, target, provider):
        inputs = provider.required_inputs(target)
        if (
            not provider.id
            or not provider.version
            or any(
                i.role != "rule"
                or i.rule_id != provider.id
                or i.rule_version != provider.version
                for i in inputs
            )
        ):
            raise ContractError("registered_rule_identity_required")
        return await self.register_inputs(
            db,
            InputRegistration(
                account_id=account_id,
                id=uuid4(),
                target_id=target.context_id,
                inputs=inputs,
            ),
        )

    async def handle_event(self, event):
        metadata = event.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        refs = metadata.get("s5_watches", ())
        if not refs:
            raise ContractError("maintenance_event_not_bound")
        outputs = []
        for raw in refs:
            outputs.extend(
                await self.begin_maintenance(
                    event["event_id"], SealRef.model_validate(raw)
                )
            )
        return tuple(outputs)

    async def begin_maintenance(self, event_id, watch_ref, *, execution_id=None):
        """One event has N immutable target snapshots. Protection already committed.

        Each target preparation commits independently. A failed target never rolls
        back the source-event barrier. No transaction crosses evidence retries.
        """
        execution = execution_id or uuid4()
        async with self.repo.session(watch_ref.account_id) as db:
            watch = await self.artifacts.get(db, watch_ref)
            if not isinstance(watch, MaintenanceWatch):
                raise ContractError("reference_kind_mismatch")
            event = await db.fetchrow(
                "SELECT * FROM change_events WHERE account_id=$1 AND event_id=$2",
                watch.account_id,
                event_id,
            )
            if event is None:
                raise ContractError("event_missing")
            metadata = event["metadata"] or {}
            if watch_ref not in tuple(
                SealRef.model_validate(r) for r in metadata.get("s5_watches", ())
            ):
                raise ContractError("maintenance_event_not_bound")
            report, graph, seeds, downstream = await read_graph_handoff(
                db, watch.report
            )
            report.configuration_payload()
            if affected_nodes(graph, watch.source, seeds, downstream) != (
                watch.seeds,
                watch.affected,
            ):
                raise ContractError("graph_handoff_mismatch")
            # Confirm ALL known targets before entering the per-target loop.
            rows = await db.fetch(
                "SELECT id,status,validity_status FROM contexts WHERE id=ANY($1::uuid[])",
                [n.context_id for n in watch.affected],
            )
            if len(rows) != len(watch.affected) or any(
                r["status"] == "active" and r["validity_status"] == "fresh"
                for r in rows
            ):
                raise ContractError("barrier_missing")
            policy = MaintenancePolicy.model_validate_json(watch.policy_json)
            if canonical_hash(policy) != watch.policy_sha256:
                raise ContractError("configuration_hash_mismatch")
        results = []
        for target in watch.affected:
            work = LocalOperation(
                self.recorder,
                identity(watch.account_id, execution, event_id, policy, "prepare"),
                {
                    "event": str(event_id),
                    "watch": watch_ref.model_dump(mode="json"),
                    "target": target.model_dump(mode="json"),
                },
            )
            await work.start()
            try:
                async with self.repo.session(watch.account_id) as raw:
                    db = MeteredDB(raw, work)
                    async with asyncio.timeout(policy.timeout_seconds):
                        frame = await self._prepare(
                            db, dict(event), watch, target, policy, execution
                        )
                    results.append(frame.reference())
                await work.finish({"frame": frame.model_dump(mode="json")})
            except Exception as exc:
                code = (
                    exc.code
                    if isinstance(exc, ContractError)
                    else "initialization_failed"
                )
                await work.finish({}, code)
                async with self.repo.session(watch.account_id) as db:
                    await trace(
                        db,
                        event_id,
                        "s5_initialization_stopped",
                        {"target": str(target.context_id), "code": code},
                    )
                raise
        return tuple(results)

    async def _prepare(self, db, event, watch, target, policy, execution, *,
                       expected_overrides=None, deadline_at=None):
        old = watch.source
        new_version = event["source_version"]
        if event["context_id"] != old.context_id or new_version != old.version + 1:
            raise ContractError("change_version_drift")
        new = ContextVersion(context_id=old.context_id, version=new_version)
        _, graph, _, _ = await read_graph_handoff(db, watch.report)
        row = await db.fetchrow(
            "SELECT version FROM contexts WHERE id=$1", target.context_id
        )
        if row is None or row["version"] != target.version:
            raise ContractError("target_version_drift")
        upstreams = tuple(e.upstream for e in graph.edges if e.downstream == target)
        overrides = expected_overrides or {}
        expected = tuple(overrides.get(n, new if n == old else n) for n in upstreams)
        registrations = await db.fetch(
            """SELECT payload,sha256 FROM knowledge_artifacts WHERE account_id=$1
            AND kind='maintenance_inputs' AND payload->>'target_id'=$2 ORDER BY id""",
            watch.account_id,
            str(target.context_id),
        )
        if any(canonical_hash(r["payload"]) != r["sha256"] for r in registrations):
            raise ContractError("artifact_hash_mismatch")
        registrations = tuple(
            InputRegistration.model_validate(r["payload"]) for r in registrations
        )
        required = [
            RequiredInput(source=old, role="before"),
            RequiredInput(source=new, role="after"),
            RequiredInput(source=target, role="target"),
        ]
        required.extend(RequiredInput(source=n, role="upstream") for n in expected)
        # Verified ancestor commits also explain newly added downstream causes.
        required.extend(RequiredInput(source=n, role="provenance") for n in overrides.values())
        report = await self.artifacts.get(db, watch.report)
        refs = [
            e
            for m in report.mappings
            if m.node in (target, *upstreams)
            for e in m.evidence
        ]
        refs += [r for e in graph.edges if e.downstream == target for r in e.evidence]
        original_hashes = {}
        for ref in refs:
            obj = await self.artifacts.get(db, ref.source)
            original_hashes[(obj.source, obj.content_level)] = obj.content_sha256
            required.append(
                RequiredInput(
                    source=obj.source,
                    content_level=obj.content_level,
                    role="provenance",
                    start=ref.start,
                    end=ref.end,
                )
            )
            if obj.source == old:
                required.append(RequiredInput(source=new, role="provenance"))
        relations = await db.fetch(
            """SELECT c.id,c.version FROM context_relations r JOIN contexts c
            ON c.id=r.related_context_id WHERE r.context_id=$1""",
            target.context_id,
        )
        required.extend(
            RequiredInput(
                source=ContextVersion(context_id=r["id"], version=r["version"]),
                role="provenance",
            )
            for r in relations
        )
        for reg in registrations:
            required.extend(reg.inputs)
        required = tuple(dict.fromkeys(required))
        # Freeze the complete tenant/ACL-permitted inventory, no relevance/label filter.
        # One SQL snapshot determines membership. Hash/length are metadata, not read receipts.
        cutoff = await db.fetchval("SELECT clock_timestamp()")
        levels = sorted(
            {policy.content_level}
            | {r.content_level for r in required if r.content_level}
        )
        queries = [
            f"""SELECT v.context_id,v.version,v.created_at,c.version current_version,
                c.uri,encode(digest(v.{level},'sha256'),'hex') sha256,
                length(v.{level}) length, '{level}' AS content_level
                FROM context_versions v JOIN contexts c ON c.id=v.context_id
                WHERE c.account_id=$1 AND v.created_at <= $2 AND v.{level} IS NOT NULL"""
            for level in levels
        ]
        inventory = await db.fetch(
            " UNION ALL ".join(queries)
            + " ORDER BY created_at,context_id,version,content_level",
            watch.account_id,
            cutoff,
        )
        ctx = RequestContext(account_id=watch.account_id, agent_id=watch.agent_id)
        admitted = []
        for r in inventory:
            decision = await self.acl.check_read_access(db, r["uri"], ctx)
            if decision.allowed and not decision.field_masks:
                admitted.append(r)
        # A failed seal/permission read must not erase the pre-read checklist.
        # Persist its audit record separately; no content or invented evidence.
        async with self.repo.session(watch.account_id) as audit_db:
            await trace(
                audit_db,
                event["event_id"],
                "s5_required_manifest",
                {
                    "target": target.model_dump(mode="json"),
                    "watch": watch.reference().model_dump(mode="json"),
                    "required": [r.model_dump(mode="json") for r in required],
                    "cutoff_at": cutoff.isoformat(),
                    "inventory_count": len(admitted),
                },
            )
        if isinstance(db, MeteredDB):
            db.work.add("writes")
            db.work.add("audit_records")
        # A historical graph hash cannot be silently replaced with a newly
        # snapshotted hash of corrupt text at the same version.
        for r in admitted:
            cv = ContextVersion(context_id=r["context_id"], version=r["version"])
            expected_hash = original_hashes.get((cv, r["content_level"]))
            if expected_hash is not None and expected_hash != r["sha256"]:
                raise ContractError("source_version_mismatch")
        # The required list was generated independently above, before body reads.
        history = []
        snapshot_id = uuid4()
        for ordinal, r in enumerate(admitted):
            src = SourceSnapshot(
                account_id=watch.account_id,
                id=uuid5(snapshot_id, "source:" + str(ordinal)),
                source=ContextVersion(context_id=r["context_id"], version=r["version"]),
                content_level=r["content_level"],
                ingested_at=r["created_at"],
                cutoff_at=cutoff,
                content_sha256=r["sha256"],
            )
            ref = await self.artifacts.seal(db, src)
            history.append(
                SourceSlot(
                    source=src.source,
                    content_level=src.content_level,
                    ordinal=ordinal,
                    ingested_at=src.ingested_at,
                    content_sha256=src.content_sha256,
                    length=r["length"],
                    current_version=r["current_version"],
                    seal=ref,
                )
            )
        all_causes = await causes(db, target.context_id)
        if event["event_id"] not in all_causes:
            raise ContractError("barrier_cause_missing")
        snapshot = MaintenanceSnapshot(
            account_id=watch.account_id,
            id=snapshot_id,
            event_id=event["event_id"],
            target=target,
            necessary_upstreams=upstreams,
            cause_ids=all_causes,
            graph=watch.graph,
            allowed_sources=tuple(s.seal for s in history),
            cutoff_at=cutoff,
            policy_sha256=watch.policy_sha256,
        )
        snap_ref = await self.artifacts.seal(db, snapshot)
        reasons = list(watch.reasons)
        for ref in refs:
            basis = await self.artifacts.get(db, ref.source)
            latest = next(
                (
                    s.current_version
                    for s in history
                    if s.source.context_id == basis.source.context_id
                ),
                None,
            )
            if basis.source != old and latest != overrides.get(basis.source, basis.source).version:
                reasons.append("graph_basis_version_drift")
        if any(reg.unresolved_conflict_ids for reg in registrations):
            reasons.append("registered_conflict_unresolved")
        frame = MaintenanceFrame(
            account_id=watch.account_id,
            id=uuid4(),
            execution_id=execution,
            event_id=event["event_id"],
            root_event_id=event["root_event_id"] or event["event_id"],
            watch=watch.reference(),
            snapshot=snap_ref,
            old=old,
            new=new,
            expected_upstreams=expected,
            required=required,
            history=tuple(history),
            cutoff_ordinal=max(0, len(history) - 1),
            cutoff_at=cutoff,
            registrations=tuple(r.reference() for r in registrations),
            reasons=tuple(dict.fromkeys(reasons)),
            relation_sources=tuple(
                ContextVersion(context_id=r["id"], version=r["version"])
                for r in relations
            ),
            deadline_at=min(deadline_at, cutoff + timedelta(seconds=policy.timeout_seconds))
            if deadline_at is not None else cutoff + timedelta(seconds=policy.timeout_seconds),
        )
        await self.artifacts.seal(db, frame)
        await self.artifacts.create_work(db, snapshot.id, snap_ref)
        await trace(
            db, event["event_id"], "s5_initialized", frame.model_dump(mode="json")
        )
        return frame

    async def continue_after_commits(self, old_frame_ref, proof_refs):
        """Bounded S6 continuation; archived proofs alone cannot authorize it."""
        from contexthub.propagation.commit import continue_after_commits
        return await continue_after_commits(self, old_frame_ref, proof_refs)

    async def recheck(self, db, frame):
        """S6 must call again immediately before use/commit; this grants no commit."""
        snap = await self.artifacts.get(db, frame.snapshot)
        watch = await self.artifacts.get(db, frame.watch)
        reasons = list(frame.reasons)
        expected = {s.source.context_id: s.current_version for s in frame.history}
        expected.update(
            {
                n.context_id: n.version
                for n in (snap.target, frame.new, *frame.expected_upstreams)
            }
        )
        rows = await db.fetch(
            "SELECT id,version,status,validity_status,uri FROM contexts WHERE id=ANY($1::uuid[])",
            list(expected),
        )
        current = {r["id"]: r for r in rows}
        for ident, version in expected.items():
            if ident not in current or current[ident]["version"] != version:
                reasons.append("version_drift")
        for n in (frame.new, *frame.expected_upstreams):
            row = current.get(n.context_id)
            if (
                row is None
                or row["status"] != "active"
                or row["validity_status"] != "fresh"
                or await causes(db, n.context_id)
            ):
                reasons.append("upstream_unresolved")
        if await causes(db, snap.target.context_id) != snap.cause_ids:
            reasons.append("cause_drift")
        relations = await db.fetch(
            "SELECT related_context_id FROM context_relations WHERE context_id=$1",
            snap.target.context_id,
        )
        if {r["related_context_id"] for r in relations} != {
            r.context_id for r in frame.relation_sources
        }:
            reasons.append("provenance_drift")
        regs = await db.fetch(
            """SELECT id,sha256 FROM knowledge_artifacts WHERE account_id=$1
            AND kind='maintenance_inputs' AND payload->>'target_id'=$2 ORDER BY id""",
            frame.account_id,
            str(snap.target.context_id),
        )
        if tuple((r["id"], r["sha256"]) for r in regs) != tuple(
            (r.id, r.sha256) for r in frame.registrations
        ):
            reasons.append("required_registry_drift")
        ctx = RequestContext(account_id=frame.account_id, agent_id=watch.agent_id)
        for r in rows:
            decision = await self.acl.check_read_access(db, r["uri"], ctx)
            if not decision.allowed or decision.field_masks:
                reasons.append("source_permission_denied")
        if datetime.now(timezone.utc) >= frame.deadline_at:
            reasons.append("deadline_exhausted")
        return tuple(dict.fromkeys(reasons))

    async def stop_work(self, db, frame, reason):
        snap = await self.artifacts.get(db, frame.snapshot)
        state = await self.artifacts.get_work(db, frame.account_id, snap.id)
        if state.status in ("pending", "running"):
            await self.artifacts.update_work(
                db,
                frame.account_id,
                snap.id,
                state.revision,
                WorkState(
                    revision=state.revision + 1, status="unresolved", reason=reason
                ),
            )
