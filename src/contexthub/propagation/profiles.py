"""Finite, opt-in S6 orchestration. No API/default factory or benchmark imports."""

import asyncio
from types import SimpleNamespace
from pydantic import Field

from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.models.knowledge import Contract, ContractError, canonical_hash
from contexthub.models.maintenance import WorkState
from contexthub.propagation.candidate import (
    call_identity,
    remaining,
    seal_proposal,
    GenerationResponse,
)
from contexthub.propagation.evidence import EvidenceBuilder
from contexthub.propagation.executor import trace
from contexthub.propagation.hard_checks import HardChecks, passed, rule_evidence
from contexthub.propagation.planner import Profile, PlannerConfig, choose_next


class ProfileConfig(Contract):
    profile: Profile
    planner: PlannerConfig
    action_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    rule_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


class MaintenanceRunner:
    def __init__(self, executor, config, *, base, strong, verifier, rules=()):
        self.executor, self.config = executor, config
        self.repo, self.artifacts = executor.repo, executor.artifacts
        self.base, self.strong, self.verifier = base, strong, verifier
        self.rules = tuple(rules)
        self.checks = HardChecks(executor, self.rules)
        if (
            verifier.checks.executor is not executor
            or verifier.checks.rules != self.checks.rules
        ):
            raise ContractError("verifier_contract_mismatch")
        self.builder = EvidenceBuilder(executor)

    async def _audit(self, frame, kind, payload):
        async with self.repo.session(frame.account_id) as db:
            await trace(
                db,
                frame.event_id,
                kind,
                {
                    "frame": frame.reference().model_dump(mode="json"),
                    "root_event_id": str(frame.root_event_id),
                    "execution_id": str(frame.execution_id),
                    "profile": self.config.profile,
                    "config_sha256": canonical_hash(self.config),
                    **payload,
                },
            )

    async def _stop(self, frame, reason):
        async with self.repo.session(frame.account_id) as db:
            await self.executor.stop_work(db, frame, reason)
            snapshot = await self.artifacts.get(db, frame.snapshot)
            knowledge = await db.fetchval(
                "SELECT validity_status FROM contexts WHERE id=$1",
                snapshot.target.context_id,
            )
        await self._audit(
            frame, "s6_stopped", {"reason": reason, "knowledge": knowledge}
        )
        return await self._state(frame)

    async def _state(self, frame):
        async with self.repo.session(frame.account_id) as db:
            return await self.artifacts.get_work(
                db, frame.account_id, frame.snapshot.id
            )

    async def run(self, frame_ref, *, local_sources=None):
        # Loading the deadline has an explicit bound too. Include claiming and
        # its advisory-lock wait in the original frame's total time budget.
        async with asyncio.timeout(self.config.action_timeout_seconds):
            async with self.repo.session(frame_ref.account_id) as db:
                frame = await self.artifacts.get(db, frame_ref)
        try:
            async with asyncio.timeout(remaining(frame)):
                return await self._claim_and_run(frame_ref, local_sources=local_sources)
        except (TimeoutError, ContractError) as exc:
            if isinstance(exc, ContractError) and exc.code != "deadline_exhausted":
                raise
            return await self._stop(frame, "deadline_exhausted")

    async def _claim_and_run(self, frame_ref, *, local_sources=None):
        # CAS claims a work exactly once; abandoned/terminal work is not restarted.
        async with self.repo.session(frame_ref.account_id) as db:
            frame = await self.artifacts.get(db, frame_ref)
            snapshot = await self.artifacts.get(db, frame.snapshot)
            # Serialize S6 claims, not content writes. A fresh S5 preparation is
            # not permission to replenish time or replay the finite action menu.
            await db.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"s6:{frame.account_id}:{frame.event_id}:{snapshot.target.context_id}",
            )
            starts = await db.fetch(
                """SELECT payload FROM propagation_trace WHERE event_id=$1
                AND trace_type='s6_started' AND payload->>'target_id'=$2""",
                frame.event_id,
                str(snapshot.target.context_id),
            )
            lineage = await db.fetchrow(
                """SELECT payload FROM knowledge_artifacts WHERE account_id=$1
                AND kind='maintenance_continuation' AND payload->'replacement'->>'id'=$2""",
                frame.account_id,
                str(frame.id),
            )
            allowed_previous = lineage["payload"]["previous"]["id"] if lineage else None
            if any(s["payload"]["frame_id"] != allowed_previous for s in starts):
                raise ContractError("event_target_already_attempted")
            earliest = await db.fetchval(
                """SELECT min((f.payload->>'deadline_at')::timestamptz)
                FROM knowledge_artifacts f JOIN knowledge_artifacts s
                ON s.account_id=f.account_id AND s.id=(f.payload->'snapshot'->>'id')::uuid
                WHERE f.account_id=$1 AND f.kind='maintenance_frame'
                AND f.payload->>'event_id'=$2 AND s.payload->'target'->>'context_id'=$3""",
                frame.account_id,
                str(frame.event_id),
                str(snapshot.target.context_id),
            )
            if earliest is not None and frame.deadline_at > earliest:
                raise ContractError("deadline_cannot_be_renewed")
            state = await self.artifacts.get_work(
                db, frame.account_id, frame.snapshot.id
            )
            if state.status != "pending":
                raise ContractError("work_already_started")
            await self.artifacts.update_work(
                db,
                frame.account_id,
                frame.snapshot.id,
                state.revision,
                WorkState(revision=state.revision + 1, status="running"),
            )
            await trace(
                db,
                frame.event_id,
                "s6_started",
                {
                    "target_id": str(snapshot.target.context_id),
                    "frame_id": str(frame.id),
                    "execution_id": str(frame.execution_id),
                    "deadline_at": frame.deadline_at.isoformat(),
                    "profile": self.config.profile,
                },
            )
        try:
            async with asyncio.timeout(remaining(frame)):
                return await self._run(frame, local_sources)
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, ContractError)
                else "deadline_exhausted"
                if isinstance(exc, TimeoutError)
                else "maintenance_failed"
            )
            return await self._stop(frame, code)

    async def _run(self, frame, local_sources):
        async with self.repo.session(frame.account_id) as db:
            await self.checks.ready(db, frame)
        # Identical deterministic step in all profiles, with real local identity.
        receipt = None
        for rule in self.rules:
            async with self.repo.session(frame.account_id) as db:
                snap, _ = await self.checks.ready(db, frame)
                from contexthub.propagation.hard_checks import current_content

                original = await current_content(db, snap.target.context_id)
            applicability = LocalOperation(
                self.executor.recorder,
                call_identity(
                    frame,
                    SimpleNamespace(profile=self.config.profile, action="rule"),
                    "rule-applicability",
                    {"id": rule.id, "version": rule.version},
                    "local",
                ),
                {
                    "target": snap.target.model_dump(mode="json"),
                    "original": original.model_dump(mode="json"),
                },
            )
            await applicability.start()
            try:
                async with asyncio.timeout(
                    min(remaining(frame), self.config.rule_timeout_seconds)
                ):
                    applies = await asyncio.to_thread(
                        rule.applies, snap.target, original
                    )
                applicability.add("rule_checks")
                await applicability.finish({"applicable": applies})
            except BaseException:
                await applicability.finish({}, "rule_applicability_failed")
                raise
            if not applies:
                await self._audit(
                    frame,
                    "s6_rule_not_applicable",
                    {"rule_id": rule.id, "rule_version": rule.version},
                )
                continue
            receipt = receipt or await self.builder.build_evidence(
                frame.reference(), "M1"
            )
            result = await self._action(frame, receipt, "rule", rule=rule)
            if isinstance(result, WorkState):
                return result
            break  # first applicable registered rule, then the finite semantic menu
        previous = None
        m2 = None
        while True:  # bounded by choose_next's finite menu; no same-level retries
            async with self.repo.session(frame.account_id) as db:
                await self.checks.ready(db, frame)
            decision = choose_next(
                self.config.profile,
                previous,
                legal=True,
                stop_reason=None,
                config=self.config.planner,
            )
            await self._audit(frame, "s6_decision", decision.model_dump(mode="json"))
            if decision.action is None:
                return await self._stop(frame, decision.reason)
            action = decision.action
            if action == "M3":
                await self.builder.reuse_m2_for_m3(m2.reference())
                receipt = m2
            else:
                receipt = await self.builder.build_evidence(
                    frame.reference(),
                    "M1" if action == "M1" else "M2",
                    local_sources=local_sources if action == "M1" else None,
                )
                if action == "M2":
                    m2 = receipt
            if receipt.reasons == ("local_manifest_omits_known_source",):
                previous = action
                continue
            result = await self._action(frame, receipt, action)
            if isinstance(result, WorkState):
                return result
            previous = action

    async def _action(self, frame, receipt, action, rule=None):
        # Recheck even after M3 handoff; that method only hands off an input seal.
        config_hash = canonical_hash(self.config)
        async with self.repo.session(frame.account_id) as db:
            binding = await self.checks.bind(
                db,
                frame.reference(),
                receipt.reference(),
                profile=self.config.profile,
                action=action,
                configuration_sha256=config_hash,
            )
        ident = call_identity(frame, binding, "action", self.config, "local")
        work = LocalOperation(
            self.executor.recorder, ident, {"binding": binding.model_dump(mode="json")}
        )
        await work.start()
        try:
            async with asyncio.timeout(
                min(remaining(frame), self.config.action_timeout_seconds)
            ):
                result = await self._candidate_and_verify(frame, binding, rule, work)
            await work.finish(
                {
                    "result": result.model_dump(mode="json")
                    if isinstance(result, WorkState)
                    else result
                }
            )
            return result
        except BaseException as exc:
            code = (
                exc.code
                if isinstance(exc, ContractError)
                else "action_timeout"
                if isinstance(exc, (TimeoutError, asyncio.CancelledError))
                else "action_failed"
            )
            await work.finish({}, code)
            raise

    async def _candidate_and_verify(self, frame, binding, rule, work):
        async with self.repo.session(frame.account_id) as raw:
            db = MeteredDB(raw, work)
            bundle = await self.artifacts.get(db, binding.bundle)
            receipt = await self.artifacts.get(db, binding.receipt)
            snap, _ = await self.checks.ready(db, frame, receipt)
            sources = await self.checks.sources(db, frame, bundle)
        inputs = ()
        if rule:
            required = rule.required_inputs(snap.target)
            if not required or any(r not in frame.required for r in required):
                raise ContractError("rule_inputs_not_registered")
            inputs = rule_evidence(frame, bundle, required)
            ident = call_identity(
                frame,
                binding,
                "deterministic-rule",
                {"id": rule.id, "version": rule.version},
                "local",
            )
            local = LocalOperation(
                self.executor.recorder,
                ident,
                {
                    "original": binding.original.model_dump(mode="json"),
                    "inputs": [e.model_dump(mode="json") for e in inputs],
                },
            )
            await local.start()
            try:
                async with asyncio.timeout(
                    min(remaining(frame), self.config.rule_timeout_seconds)
                ):
                    generated = await asyncio.to_thread(
                        rule.execute, binding.original, inputs, binding.causes
                    )
                    repeated = await asyncio.to_thread(
                        rule.execute, binding.original, inputs, binding.causes
                    )
                generated = GenerationResponse.model_validate(
                    generated.model_dump(mode="json")
                )
                local.add("rule_checks", 2)
                if generated != repeated:
                    raise ContractError("rule_not_deterministic")
                await local.finish(generated.model_dump(mode="json"))
            except BaseException:
                await local.finish({}, "rule_failed")
                raise
        else:
            generator = (
                self.strong if binding.action in ("M3", "full/strong") else self.base
            )
            try:
                generated, ident = await generator.generate(
                    frame, binding, bundle, sources
                )
            except Exception as exc:
                remaining(frame)
                # Deterministic schema/no-candidate failure may upgrade; no retry
                # here. Exhausted infrastructure is a hard stop.
                from contexthub.llm.retry_policy import (
                    ResponseParseError,
                    ResponseSchemaError,
                )
                from pydantic import ValidationError

                if isinstance(
                    exc, (ResponseParseError, ResponseSchemaError, ValidationError)
                ):
                    await self._audit(
                        frame,
                        "s6_candidate_failed",
                        {
                            "action": binding.action,
                            "reason": "candidate_schema_invalid",
                        },
                    )
                    return "candidate_schema_invalid"
                raise ContractError("generator_unavailable") from exc
        if generated.proposal is None:
            await self._audit(
                frame,
                "s6_candidate_failed",
                {"action": binding.action, "reason": "no_candidate"},
            )
            return "no_candidate"
        async with self.repo.session(frame.account_id) as raw:
            db = MeteredDB(raw, work)
            await self.checks.ready(db, frame, receipt)
            if any(
                e not in tuple(r.evidence for r in bundle.read)
                for c in (*generated.proposal.claims, *generated.proposal.causes)
                for e in c.evidence
            ):
                await self._audit(
                    frame,
                    "s6_candidate_failed",
                    {"action": binding.action, "reason": "candidate_evidence_not_read"},
                )
                return "candidate_evidence_not_read"
            proposal = await seal_proposal(
                self.artifacts,
                db,
                binding,
                generated.proposal,
                ident,
                rule=rule,
                inputs=inputs,
            )
            check = await self.checks.check(db, proposal.reference())
        await self._audit(
            frame,
            "s6_checked",
            {
                "proposal": proposal.reference().model_dump(mode="json"),
                "check": check.model_dump(mode="json"),
            },
        )
        if not passed(check):
            # Current-state/source/cause failures are hard; malformed conclusions
            # can try the next finite action without bypassing a safety failure.
            failures = [c for c in check.checks if c.verdict != "PASS"]
            if any(c.code not in ("conclusions",) for c in failures):
                raise ContractError(failures[0].reason)
            return "hard_check_failed"
        try:
            result = await self.verifier.verify(
                proposal.reference(), check.reference(), frame=frame, binding=binding
            )
        except Exception as exc:
            raise ContractError("verifier_unavailable") from exc
        await self._audit(
            frame, "s6_verified", {"verdict": result.model_dump(mode="json")}
        )
        if any(
            getattr(result.data, k).verdict != "PASS"
            for k in ("applicability", "complete_support", "conflicts_resolved")
        ):
            return "semantic_not_passed"
        from contexthub.propagation.commit import GuardedCommit

        return await GuardedCommit(self.executor, self.checks, self.verifier).commit(
            result.reference()
        )
