"""Program checks are authoritative for structure/current state, not semantics."""

from uuid import uuid4

from contexthub.models.knowledge import (
    ContractError,
    ContextVersion,
    canonical_hash,
    text_hash,
)
from contexthub.models.maintenance import CheckItem, HardCheckResult
from contexthub.propagation.candidate import (
    ActionBinding,
    Cause,
    Content,
    Proposal,
    remaining,
)
from contexthub.propagation.contracts import MaintenanceFrame, EvidenceReceipt
from contexthub.services.evidence_service import EvidenceService

CHECKER_VERSION = "s6-hard-checks-v1"
REQUIRED_CHECKS = (
    "binding",
    "manifest",
    "current_state",
    "sources",
    "conclusions",
    "causes",
    "rule",
)


async def read_causes(db, target):
    rows = await db.fetch(
        """SELECT i.cause_event_id,i.source_context_id,i.source_version,i.reason_hash
        FROM context_invalidations i JOIN contexts c ON c.id=i.context_id
        WHERE c.account_id=current_setting('app.account_id') AND i.context_id=$1
        AND i.resolved_at IS NULL ORDER BY i.cause_event_id""",
        target,
    )
    if any(r["source_version"] is None or r["source_context_id"] is None for r in rows):
        raise ContractError("cause_source_unknown")
    return tuple(
        Cause(
            event_id=r["cause_event_id"],
            source=ContextVersion(
                context_id=r["source_context_id"], version=r["source_version"]
            ),
            reason_sha256=r["reason_hash"],
        )
        for r in rows
    )


async def current_content(db, target):
    row = await db.fetchrow(
        "SELECT l0_content,l1_content,l2_content FROM contexts WHERE id=$1", target
    )
    if row is None:
        raise ContractError("target_missing")
    return Content(**dict(row))


class HardChecks:
    def __init__(self, executor, rules=()):
        self.executor, self.artifacts = executor, executor.artifacts
        self.rules = {(r.id, r.version): r for r in rules}
        if len(self.rules) != len(rules):
            raise ContractError("duplicate_rule")

    async def ready(self, db, frame, receipt=None):
        if not isinstance(frame, MaintenanceFrame):
            raise ContractError("frame_required")
        remaining(frame)
        snap = await self.artifacts.get(db, frame.snapshot)
        watch = await self.artifacts.get(db, frame.watch)
        if (
            frame.account_id != snap.account_id
            or snap.event_id != frame.event_id
            or snap.graph != watch.graph
            or snap.policy_sha256 != watch.policy_sha256
            or snap.target not in watch.affected
        ):
            raise ContractError("frame_binding_mismatch")
        state = await self.artifacts.get_work(db, frame.account_id, snap.id)
        if state.status not in ("pending", "running"):
            raise ContractError("work_terminal")
        reasons = await self.executor.recheck(db, frame)
        if reasons:
            raise ContractError(reasons[0])
        from contexthub.propagation.contracts import MaintenancePolicy
        from contexthub.propagation.evidence import chunks_for

        policy = MaintenancePolicy.model_validate_json(watch.policy_json)
        slots = {(s.source, s.content_level): s for s in frame.history}
        for item in frame.required:
            slot = slots.get((item.source, item.content_level or policy.content_level))
            if slot is None:
                raise ContractError("required_source_missing_or_forbidden")
            chunks_for(slot.length, item.start, item.end, policy.chunk_size)
        row = await db.fetchrow(
            "SELECT status,validity_status,version FROM contexts WHERE id=$1",
            snap.target.context_id,
        )
        if (
            row is None
            or row["version"] != snap.target.version
            or row["status"] not in ("active", "stale")
            or row["validity_status"] != "stale"
        ):
            raise ContractError("target_not_maintainable")
        if receipt is not None:
            if (
                not isinstance(receipt, EvidenceReceipt)
                or receipt.frame != frame.reference()
                or not receipt.ready
                or not receipt.complete
                or receipt.bundle is None
            ):
                raise ContractError("receipt_not_ready")
            await self.artifacts.validate_maintenance_receipt(db, receipt)
            bundle = await self.artifacts.get(db, receipt.bundle)
            if bundle.maintenance != frame.snapshot or any(
                not c.resolved for c in bundle.conflicts
            ):
                raise ContractError("bundle_conflict_or_binding")
        return snap, state

    async def bind(
        self, db, frame_ref, receipt_ref, *, profile, action, configuration_sha256
    ):
        frame = await self.artifacts.get(db, frame_ref)
        receipt = await self.artifacts.get(db, receipt_ref)
        snap, state = await self.ready(db, frame, receipt)
        scope = "M2" if action in ("M2", "M3", "full/strong") else "M1"
        if receipt.scope != scope:
            raise ContractError("action_scope_mismatch")
        binding = ActionBinding(
            account_id=frame.account_id,
            id=uuid4(),
            frame=frame_ref,
            receipt=receipt_ref,
            snapshot=frame.snapshot,
            bundle=receipt.bundle,
            execution_id=frame.execution_id,
            work_revision=state.revision,
            profile=profile,
            action=action,
            configuration_sha256=configuration_sha256,
            original=await current_content(db, snap.target.context_id),
            causes=await read_causes(db, snap.target.context_id),
        )
        await self.artifacts.seal(db, binding)
        return binding

    async def sources(self, db, frame, bundle):
        """Fixed source identities plus explicit historical/current applicability."""
        result = []
        for ref in dict.fromkeys(e.evidence.source for e in bundle.read):
            source = await self.artifacts.get(db, ref)
            await EvidenceService(self.artifacts).read_source(db, source)
            row = await db.fetchrow(
                "SELECT version,status,validity_status FROM contexts WHERE id=$1",
                source.source.context_id,
            )
            if row is None:
                raise ContractError("source_missing")
            result.append(
                {"source": source.model_dump(mode="json"), "current": dict(row)}
            )
        # Exact span/excerpt/hash checks, independent of receipt.complete.
        for entry in bundle.read:
            await EvidenceService(self.artifacts).resolve(db, entry.evidence)
        return result

    async def check(self, db, proposal_ref, *, persist=True):
        proposal = await self.artifacts.get(db, proposal_ref)
        if not isinstance(proposal, Proposal):
            raise ContractError("s6_proposal_required")
        candidate = await self.artifacts.get(db, proposal.candidate)
        binding = await self.artifacts.get(db, proposal.action)
        frame = await self.artifacts.get(db, binding.frame)
        receipt = await self.artifacts.get(db, binding.receipt)
        bundle = await self.artifacts.get(db, binding.bundle)
        snap = await self.artifacts.get(db, binding.snapshot)
        checks = []

        async def run(code, fn):
            try:
                await fn()
                checks.append(CheckItem(code=code, verdict="PASS", reason="checked"))
            except ContractError as exc:
                checks.append(CheckItem(code=code, verdict="FAIL", reason=exc.code))

        async def bindings():
            refs = (
                proposal_ref,
                proposal.action,
                proposal.candidate,
                binding.frame,
                binding.receipt,
                binding.snapshot,
                binding.bundle,
            )
            if (
                any(r.account_id != frame.account_id for r in refs)
                or binding.execution_id != frame.execution_id
                or binding.snapshot != frame.snapshot
                or receipt.frame != binding.frame
                or receipt.bundle != binding.bundle
                or candidate.maintenance != binding.snapshot
                or candidate.bundle != binding.bundle
                or candidate.proposed_content != proposal.data.content.text()
                or candidate.outcome != proposal.data.outcome
            ):
                raise ContractError("candidate_binding_mismatch")
            expected = tuple(
                dict.fromkeys(
                    e
                    for c in (*proposal.data.claims, *proposal.data.causes)
                    for e in c.evidence
                )
            )
            if candidate.evidence != expected:
                raise ContractError("candidate_evidence_mismatch")

        async def manifest():
            await self.artifacts.validate_maintenance_receipt(db, receipt)
            if not receipt.ready or bundle.maintenance != binding.snapshot:
                raise ContractError("receipt_not_ready")

        async def state():
            _, current = await self.ready(db, frame, receipt)
            if current.revision != binding.work_revision:
                raise ContractError("work_revision_conflict")
            if await current_content(db, snap.target.context_id) != binding.original:
                raise ContractError("target_content_drift")

        async def sources():
            await self.sources(db, frame, bundle)
            if any(
                e not in tuple(r.evidence for r in bundle.read)
                for e in candidate.evidence
            ):
                raise ContractError("evidence_not_read")
            for e in candidate.evidence:
                source = await self.artifacts.get(db, e.source)
                row = await db.fetchrow(
                    "SELECT status,validity_status FROM contexts WHERE id=$1",
                    source.source.context_id,
                )
                current_version = next(
                    s.current_version for s in frame.history if s.seal == e.source
                )
                if (
                    source.source not in (snap.target, frame.old)
                    and source.source.version != current_version
                ):
                    raise ContractError("historical_evidence_not_current")
                if (
                    source.source.context_id != snap.target.context_id
                    and source.source != frame.old
                    and (row["status"] != "active" or row["validity_status"] != "fresh")
                ):
                    raise ContractError("stale_evidence")
                if source.source not in (snap.target, frame.old):
                    from contexthub.propagation.executor import causes as open_causes

                    if await open_causes(db, source.source.context_id):
                        raise ContractError("evidence_unresolved_causes")

        async def conclusions():
            type(proposal.data).model_validate(proposal.data.model_dump(mode="json"))
            from contexthub.services.semantic_identity import semantic_identity

            old = semantic_identity(
                binding.original.l0_content,
                binding.original.l1_content,
                binding.original.l2_content,
            )
            new = semantic_identity(
                proposal.data.content.l0_content,
                proposal.data.content.l1_content,
                proposal.data.content.l2_content,
            )
            if (
                candidate.outcome == "unchanged"
                and proposal.data.content != binding.original
            ):
                raise ContractError("unchanged_content_mismatch")
            if candidate.outcome == "updated" and new == old:
                raise ContractError("updated_identity_unchanged")
            for claim in proposal.data.claims:
                versions = [
                    (await self.artifacts.get(db, e.source)).source
                    for e in claim.evidence
                ]
                if not any(v not in (snap.target, frame.old) for v in versions):
                    raise ContractError("conclusion_only_historical_support")

        async def causes():
            if binding.causes != await read_causes(db, snap.target.context_id):
                raise ContractError("cause_drift")
            if tuple(c.cause for c in proposal.data.causes) != binding.causes:
                raise ContractError("cause_support_incomplete")
            for support in proposal.data.causes:
                if support.cause.reason_sha256 != text_hash("s5:source_changed"):
                    raise ContractError("cause_kind_not_supported")
                event = await db.fetchrow(
                    "SELECT context_id,source_version,metadata FROM change_events WHERE event_id=$1",
                    support.cause.event_id,
                )
                if (
                    event is None
                    or event["context_id"] != support.cause.source.context_id
                    or event["source_version"] != support.cause.source.version
                ):
                    raise ContractError("cause_event_mismatch")
                # Archives have no authority. Locate an actual successful work's
                # exact proof, then validate its event and graph lineage too.
                if support.cause.event_id != frame.event_id:
                    from contexthub.propagation.commit import require_committed_upstream
                    from contexthub.models.knowledge import SealRef

                    rows = await db.fetch(
                        """SELECT p.id,p.sha256 FROM context_refresh_proofs p
                        JOIN maintenance_work_items w ON w.account_id=p.account_id
                        AND w.state->'proof'->>'id'=p.id::text
                        WHERE p.account_id=$1 AND p.payload->>'commit_event_id'=$2
                        AND p.payload->>'execution_id'=$3
                        AND w.status IN ('updated','unchanged')""",
                        frame.account_id,
                        str(support.cause.event_id),
                        str(frame.execution_id),
                    )
                    if len(rows) != 1:
                        raise ContractError("cause_resolution_not_authorized")
                    try:
                        proof, _ = await require_committed_upstream(
                            self.artifacts,
                            db,
                            frame,
                            SealRef(
                                account_id=frame.account_id,
                                kind="commit_proof",
                                id=rows[0]["id"],
                                sha256=rows[0]["sha256"],
                            ),
                        )
                        if proof.committed_target != support.cause.source:
                            raise ContractError("committed_event_mismatch")
                    except ContractError as exc:
                        raise ContractError("cause_resolution_not_authorized") from exc
                versions = [
                    (await self.artifacts.get(db, e.source)).source
                    for e in support.evidence
                ]
                if support.cause.source not in versions:
                    raise ContractError("cause_current_evidence_missing")

        async def rule():
            if proposal.producer == "model":
                if any(
                    v is not None
                    for v in (
                        proposal.rule_id,
                        proposal.rule_version,
                        proposal.rule_inputs_sha256,
                        candidate.rule_version,
                    )
                ):
                    raise ContractError("model_rule_identity_forbidden")
                return
            registered = self.rules.get((proposal.rule_id, proposal.rule_version))
            if registered is None or candidate.rule_version != registered.version:
                raise ContractError("rule_not_registered")
            required = registered.required_inputs(snap.target)
            if not required or any(r not in frame.required for r in required):
                raise ContractError("rule_inputs_not_registered")
            inputs = rule_evidence(frame, bundle, required)
            if (
                canonical_hash([e.model_dump(mode="json") for e in inputs])
                != proposal.rule_inputs_sha256
            ):
                raise ContractError("rule_inputs_mismatch")

        for code, fn in zip(
            REQUIRED_CHECKS,
            (bindings, manifest, state, sources, conclusions, causes, rule),
        ):
            await run(code, fn)
        result = HardCheckResult(
            account_id=frame.account_id,
            id=uuid4(),
            candidate=proposal.candidate,
            checker_version=CHECKER_VERSION,
            checks=tuple(checks),
        )
        if persist:
            await self.artifacts.seal(db, result)
        return result


def passed(check):
    return (
        check.checker_version == CHECKER_VERSION
        and tuple(c.code for c in check.checks) == REQUIRED_CHECKS
        and all(c.verdict == "PASS" for c in check.checks)
    )


def rule_evidence(frame, bundle, required):
    refs = {
        s.seal
        for s in frame.history
        if any(
            s.source == r.source
            and (r.content_level is None or r.content_level == s.content_level)
            for r in required
        )
    }
    return tuple(
        e.evidence
        for e in bundle.read
        if e.evidence.source in refs and e.evidence.provenance == ("rule",)
    )
