"""Short, guarded commit. External generation/verification never holds locks."""

import asyncio
from typing import Literal
from uuid import UUID, uuid4

from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.models.knowledge import ContextVersion, ContractError, SealRef, Sealed
from contexthub.models.maintenance import CommitProof, WorkState
from contexthub.propagation.candidate import Cause, call_identity, remaining, now
from contexthub.propagation.executor import trace
from contexthub.propagation.hard_checks import passed, read_causes
from contexthub.services.context_service import ContextService


class GuardedProof(CommitProof):
    # Proof table already stores arbitrary immutable JSON, with no schema_version
    # column constraint. Existing v1 artifacts/proofs are never rewritten.
    schema_version: Literal[2] = 2
    frame: SealRef
    receipt: SealRef
    proposal: SealRef
    verdict: SealRef
    execution_id: UUID
    work_revision: int
    checked_causes: tuple[Cause, ...]
    commit_event_id: UUID | None


class Continuation(Sealed):
    kind: Literal["maintenance_continuation"] = "maintenance_continuation"
    previous: SealRef
    replacement: SealRef
    upstream_proofs: tuple[SealRef, ...]


COMMIT_LOCKS = """LOCK TABLE contexts, context_versions,
    context_invalidations, context_relations, dependencies,
    access_policies, teams, team_memberships,
    knowledge_artifacts, maintenance_work_items, change_events
    IN SHARE ROW EXCLUSIVE MODE"""


async def require_committed_upstream(artifacts, db, frame, proof_ref):
    """Consume a committed work result, never an audit archive as authority.

    Called again inside COMMIT_LOCKS for cause resolution and continuation, so
    the work/event/current-source checks share the actual write transaction.
    """
    proof = await artifacts.get(db, proof_ref)
    if not isinstance(proof, GuardedProof) or proof.execution_id != frame.execution_id:
        raise ContractError("continuation_proof_mismatch")
    await validate_guarded_proof(artifacts, db, proof)
    prior = await artifacts.get(db, proof.maintenance)
    prior_frame = await artifacts.get(db, proof.frame)
    committed = await artifacts.get_work(db, frame.account_id, prior.id)
    proposal = await artifacts.get(db, proof.proposal)
    snapshot = await artifacts.get(db, frame.snapshot)
    graph = await artifacts.get(db, snapshot.graph)
    ancestors, frontier = set(), {snapshot.target}
    while frontier:
        parents = {
            e.upstream for e in graph.edges if e.downstream in frontier
        } - ancestors
        ancestors.update(parents)
        frontier = parents
    if (
        proof.account_id != frame.account_id
        or prior.target not in ancestors
        or prior.graph != snapshot.graph
        or prior_frame.watch != frame.watch
        or prior_frame.root_event_id != frame.root_event_id
        or prior_frame.event_id != frame.event_id
        or committed.proof != proof_ref
        or committed.status != proposal.data.outcome
        or committed.status not in ("updated", "unchanged")
        or committed.revision != proof.work_revision + 1
        or proof.committed_target
        != ContextVersion(
            context_id=prior.target.context_id,
            version=prior.target.version + (committed.status == "updated"),
        )
    ):
        raise ContractError("continuation_uncommitted_proof")
    if proof.commit_event_id is not None:
        event = await db.fetchrow(
            "SELECT * FROM change_events WHERE account_id=$1 AND event_id=$2",
            frame.account_id,
            proof.commit_event_id,
        )
        if (
            event is None
            or event["context_id"] != proof.committed_target.context_id
            or event["source_version"] != proof.committed_target.version
            or event["change_type"] != "modified"
            or event["metadata"].get("s6_proposal")
            != proof.proposal.model_dump(mode="json")
            or event["metadata"].get("s6_execution_id") != str(frame.execution_id)
            or event["metadata"].get("s6_root_event_id") != str(frame.root_event_id)
        ):
            raise ContractError("committed_event_mismatch")
    row = await db.fetchrow(
        "SELECT version,status,validity_status FROM contexts WHERE id=$1",
        proof.committed_target.context_id,
    )
    if (
        row is None
        or row["version"] != proof.committed_target.version
        or row["status"] != "active"
        or row["validity_status"] != "fresh"
        or await read_causes(db, proof.committed_target.context_id)
    ):
        raise ContractError("continuation_upstream_unresolved")
    return proof, prior


async def continue_after_commits(executor, old_frame_ref, proof_refs):
    """One replacement per original downstream frame, using committed ancestors.

    No waiting loop, graph mutation, latest substitution, or renewed deadline.
    Calling code schedules the finite one/two-hop frontier explicitly.
    """
    from contexthub.propagation.contracts import MaintenancePolicy
    from contexthub.propagation.candidate import call_identity
    from types import SimpleNamespace

    artifacts = executor.artifacts
    async with executor.repo.session(old_frame_ref.account_id) as db:
        old = await artifacts.get(db, old_frame_ref)
    work = LocalOperation(
        executor.recorder,
        call_identity(
            old,
            SimpleNamespace(profile="continuation", action="S6"),
            "continue-after-commits",
            {"version": "s6-continuation-v1"},
            "local",
        ),
        {
            "previous": old_frame_ref.model_dump(mode="json"),
            "proofs": [r.model_dump(mode="json") for r in proof_refs],
        },
    )
    await work.start()
    try:
        async with asyncio.timeout(remaining(old)):
            async with executor.repo.session(old.account_id) as raw:
                db = MeteredDB(raw, work)
                await db.execute(COMMIT_LOCKS)
                remaining(old)
                if not proof_refs or len(set(proof_refs)) != len(proof_refs):
                    raise ContractError("continuation_proofs_required")
                used = await db.fetchval(
                    """SELECT 1 FROM knowledge_artifacts WHERE account_id=$1
                    AND kind='maintenance_continuation' AND
                    (payload->'previous'->>'id'=$2 OR payload->'replacement'->>'id'=$2)""",
                    old.account_id,
                    str(old.id),
                )
                if used:
                    raise ContractError("continuation_limit")
                snapshot = await artifacts.get(db, old.snapshot)
                state = await artifacts.get_work(db, old.account_id, snapshot.id)
                if state.status not in ("pending", "unresolved") or (
                    state.status == "unresolved"
                    and state.reason not in ("upstream_unresolved", "version_drift")
                ):
                    raise ContractError("continuation_work_not_waiting")
                attempted = await db.fetchval(
                    """SELECT 1 FROM execution_attempts WHERE account_id=$1
                    AND payload->'identity'->>'work_item_id'=$2
                    AND payload->'identity'->>'model_role' IN ('generator','verifier')""",
                    old.account_id,
                    str(snapshot.id),
                )
                if attempted:
                    raise ContractError("continuation_attempt_already_used")
                overrides = {}
                allowed_events = {old.event_id}
                for ref in proof_refs:
                    proof, prior = await require_committed_upstream(
                        artifacts, db, old, ref
                    )
                    overrides[prior.target] = proof.committed_target
                    if proof.commit_event_id:
                        allowed_events.add(proof.commit_event_id)
                expected = {s.source.context_id: s.current_version for s in old.history}
                expected.update({n.context_id: n.version for n in overrides.values()})
                current = await db.fetch(
                    "SELECT id,version FROM contexts WHERE id=ANY($1::uuid[])",
                    list(expected),
                )
                if len(current) != len(expected) or any(
                    r["version"] != expected[r["id"]] for r in current
                ):
                    raise ContractError("continuation_external_drift")
                # Required registrations/provenance/permissions must still match;
                # only explained versions and now-resolved upstream states differ.
                reasons = await executor.recheck(db, old)
                if any(
                    r not in ("version_drift", "cause_drift", "upstream_unresolved")
                    for r in reasons
                ):
                    raise ContractError("continuation_precondition_drift")
                current_causes = await read_causes(db, snapshot.target.context_id)
                if any(c.event_id not in allowed_events for c in current_causes):
                    raise ContractError("continuation_external_cause")
                watch = await artifacts.get(db, old.watch)
                event = await db.fetchrow(
                    "SELECT * FROM change_events WHERE event_id=$1", old.event_id
                )
                policy = MaintenancePolicy.model_validate_json(watch.policy_json)
                replacement = await executor._prepare(
                    db,
                    dict(event),
                    watch,
                    snapshot.target,
                    policy,
                    old.execution_id,
                    expected_overrides=overrides,
                    deadline_at=old.deadline_at,
                )
                # New unrelated history is not silently admitted during continuation.
                permitted = {(s.source, s.content_level) for s in old.history}
                permitted.update(
                    (n, s.content_level)
                    for n in overrides.values()
                    for s in old.history
                    if s.source.context_id == n.context_id
                )
                if any(
                    (s.source, s.content_level) not in permitted
                    for s in replacement.history
                ):
                    raise ContractError("continuation_external_history")
                # This is a new work/snapshot. Preserve the original terminal record.
                await executor.stop_work(db, old, "superseded_by_continuation")
                lineage = Continuation(
                    account_id=old.account_id,
                    id=uuid4(),
                    previous=old_frame_ref,
                    replacement=replacement.reference(),
                    upstream_proofs=tuple(proof_refs),
                )
                await artifacts.seal(db, lineage)
                await trace(
                    db, old.event_id, "s6_continuation", lineage.model_dump(mode="json")
                )
                remaining(old)
        await work.finish({"continuation": lineage.model_dump(mode="json")})
        return replacement.reference()
    except BaseException as exc:
        await work.finish(
            {}, exc.code if isinstance(exc, ContractError) else "continuation_failed"
        )
        raise


async def validate_guarded_proof(artifacts, db, proof):
    frame = await artifacts.get(db, proof.frame)
    proposal = await artifacts.get(db, proof.proposal)
    binding = await artifacts.get(db, proposal.action)
    verdict = await artifacts.get(db, proof.verdict)
    if (
        binding.frame != proof.frame
        or binding.snapshot != proof.maintenance
        or binding.receipt != proof.receipt
        or proposal.candidate != proof.candidate
        or verdict.proposal != proof.proposal
        or verdict.hard_check != proof.hard_check
        or verdict.verification != proof.verification
        or frame.execution_id != proof.execution_id
        or binding.work_revision != proof.work_revision
        or proof.checked_upstreams != frame.expected_upstreams
        or proof.checked_causes != binding.causes
        or proof.cleared_cause_ids != tuple(c.event_id for c in binding.causes)
        or (proposal.data.outcome == "updated") != (proof.commit_event_id is not None)
    ):
        raise ContractError("guarded_proof_binding_mismatch")
    receipt = await artifacts.get(db, proof.receipt)
    await artifacts.validate_maintenance_receipt(db, receipt)


class GuardedCommit:
    def __init__(self, executor, checks, verifier):
        self.executor, self.checks, self.verifier = executor, checks, verifier
        self.repo, self.artifacts = executor.repo, executor.artifacts

    async def commit(self, verdict_ref):
        async with self.repo.session(verdict_ref.account_id) as db:
            verdict = await self.artifacts.get(db, verdict_ref)
            proposal = await self.artifacts.get(db, verdict.proposal)
            binding = await self.artifacts.get(db, proposal.action)
            frame = await self.artifacts.get(db, binding.frame)
        # Consume before trying the transaction: failed/expired candidates cannot
        # be resubmitted. An ordinary archive or caller-provided PASS cannot grant.
        self.verifier.consume(
            verdict_ref, proposal.reference(), verdict.hard_check, binding.reference()
        )
        operation = LocalOperation(
            self.executor.recorder,
            call_identity(
                frame, binding, "guarded-commit", {"version": "s6-commit-v1"}, "local"
            ),
            {
                "verdict": verdict_ref.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
            },
        )
        await operation.start()
        try:
            async with asyncio.timeout(remaining(frame)):
                async with self.repo.session(frame.account_id) as raw:
                    db = MeteredDB(raw, operation)
                    # Existing writers do not share an advisory-lock protocol.
                    # PostgreSQL table locks fence their DML and phantom inserts
                    # too (causes/ACL/registrations), including independent conns.
                    # Deliberately conservative, short, no throughput/HA claim.
                    await db.execute(COMMIT_LOCKS)
                    remaining(frame)
                    check = await self.checks.check(
                        db, proposal.reference(), persist=False
                    )
                    if not passed(check):
                        reason = next(
                            c.reason for c in check.checks if c.verdict != "PASS"
                        )
                        raise ContractError(reason)
                    original_check = await self.artifacts.get(db, verdict.hard_check)
                    verification = await self.artifacts.get(db, verdict.verification)
                    if (
                        not passed(original_check)
                        or original_check.candidate != proposal.candidate
                        or verification.candidate != proposal.candidate
                        or verification.bundle != binding.bundle
                        or any(
                            getattr(verification, k).verdict != "PASS"
                            for k in (
                                "applicability",
                                "complete_support",
                                "conflicts_resolved",
                            )
                        )
                    ):
                        raise ContractError("commit_checks_not_passed")
                    snap = await self.artifacts.get(db, frame.snapshot)
                    actual = await read_causes(db, snap.target.context_id)
                    await trace(
                        db,
                        frame.event_id,
                        "s6_commit_checked",
                        {
                            "frame": frame.reference().model_dump(mode="json"),
                            "expected_target": snap.target.model_dump(mode="json"),
                            "actual_target": snap.target.model_dump(mode="json"),
                            "expected_causes": [
                                c.model_dump(mode="json") for c in binding.causes
                            ],
                            "actual_causes": [
                                c.model_dump(mode="json") for c in actual
                            ],
                        },
                    )
                    cause_pairs = tuple(
                        (str(c.event_id), c.source.version) for c in binding.causes
                    )
                    commit_event = None
                    content = proposal.data.content
                    if proposal.data.outcome == "updated":
                        change = await ContextService.apply_system_content_change(
                            db,
                            context_id=snap.target.context_id,
                            expected_version=snap.target.version,
                            l0_content=content.l0_content,
                            l1_content=content.l1_content,
                            l2_content=content.l2_content,
                            actor="s6-maintenance",
                            diff_summary="verified maintenance",
                            metadata={
                                "s6_execution_id": str(frame.execution_id),
                                "s6_root_event_id": str(frame.root_event_id),
                                "s6_proposal": proposal.reference().model_dump(
                                    mode="json"
                                ),
                            },
                            idempotency_key="s6:" + str(proposal.id),
                            resolve_invalidation_causes=cause_pairs,
                        )
                        commit_event = UUID(change["event_id"])
                    else:
                        await ContextService._apply_verified_unchanged(
                            db,
                            context_id=snap.target.context_id,
                            expected_version=snap.target.version,
                            causes=cause_pairs,
                        )
                    proof = GuardedProof(
                        account_id=frame.account_id,
                        id=uuid4(),
                        candidate=proposal.candidate,
                        hard_check=verdict.hard_check,
                        verification=verdict.verification,
                        maintenance=frame.snapshot,
                        committed_target=ContextVersion(
                            context_id=snap.target.context_id,
                            version=snap.target.version
                            + (proposal.data.outcome == "updated"),
                        ),
                        checked_upstreams=frame.expected_upstreams,
                        checked_sources=snap.allowed_sources,
                        cleared_cause_ids=tuple(c.event_id for c in binding.causes),
                        committed_at=now(),
                        commit_code_version="s6-commit-v1",
                        frame=frame.reference(),
                        receipt=binding.receipt,
                        proposal=proposal.reference(),
                        verdict=verdict_ref,
                        execution_id=frame.execution_id,
                        work_revision=binding.work_revision,
                        checked_causes=binding.causes,
                        commit_event_id=commit_event,
                    )
                    proof_ref = await self.artifacts.archive_commit_proof(db, proof)
                    state = WorkState(
                        revision=binding.work_revision + 1,
                        status=proposal.data.outcome,
                        proof=proof_ref,
                    )
                    changed = await db.fetchval(
                        """UPDATE maintenance_work_items SET revision=$4,status=$5,state=$6
                        WHERE account_id=$1 AND id=$2 AND revision=$3 AND status IN ('pending','running') RETURNING revision""",
                        frame.account_id,
                        frame.snapshot.id,
                        binding.work_revision,
                        state.revision,
                        state.status,
                        state.model_dump(mode="json"),
                    )
                    if changed is None:
                        raise ContractError("work_revision_conflict")
                    if await read_causes(db, snap.target.context_id):
                        raise ContractError("unresolved_causes_remain")
                    # clock_timestamp checks real time, not the transaction start.
                    if await db.fetchval(
                        "SELECT clock_timestamp() >= $1", frame.deadline_at
                    ):
                        raise ContractError("deadline_exhausted")
                    await trace(
                        db,
                        frame.event_id,
                        "s6_committed",
                        proof.model_dump(mode="json"),
                    )
        except BaseException as exc:
            code = (
                exc.code
                if isinstance(exc, ContractError)
                else "deadline_exhausted"
                if isinstance(exc, (TimeoutError, asyncio.CancelledError))
                else "commit_rolled_back"
            )
            # Ledger lives outside the rolled-back business transaction.
            await operation.finish({}, code)
            async with self.repo.session(frame.account_id) as db:
                await self.executor.stop_work(db, frame, code)
                snap = await self.artifacts.get(db, frame.snapshot)
                actual_versions = await db.fetch(
                    "SELECT id,version,status,validity_status FROM contexts WHERE id=ANY($1::uuid[])",
                    list(
                        {
                            snap.target.context_id,
                            frame.new.context_id,
                            *(s.source.context_id for s in frame.history),
                        }
                    ),
                )
                actual_causes = await db.fetch(
                    "SELECT cause_event_id,source_context_id,source_version,resolved_at FROM context_invalidations WHERE context_id=$1",
                    snap.target.context_id,
                )
                await trace(
                    db,
                    frame.event_id,
                    "s6_commit_rejected",
                    {
                        "proposal": proposal.reference().model_dump(mode="json"),
                        "reason": code,
                        "expected_target": snap.target.model_dump(mode="json"),
                        "expected_causes": [
                            c.model_dump(mode="json") for c in binding.causes
                        ],
                        "actual_versions": [
                            {
                                k: str(v) if isinstance(v, UUID) else v
                                for k, v in dict(r).items()
                            }
                            for r in actual_versions
                        ],
                        "actual_causes": [
                            {
                                k: str(v) if v is not None else None
                                for k, v in dict(r).items()
                            }
                            for r in actual_causes
                        ],
                    },
                )
            raise ContractError(code) from exc
        # Do not mislabel a post-commit ledger outage as a transaction rollback.
        await operation.finish(
            {
                "proof": proof.model_dump(mode="json"),
                "state": state.model_dump(mode="json"),
            }
        )
        return state
