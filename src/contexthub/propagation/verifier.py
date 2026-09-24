"""One fixed, separately called verifier; no generator self-grading inputs."""

from typing import Literal
from uuid import UUID, uuid4
import asyncio
from pydantic import Field

from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    EvidenceRef,
    Name,
    Nonnegative,
    Sealed,
    SealRef,
    canonical_hash,
)
from contexthub.models.maintenance import CheckItem, VerificationResult
from contexthub.propagation.candidate import ModelCall, call_identity
from contexthub.propagation.hard_checks import passed


class Judgment(Contract):
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]
    reason: Name
    evidence: tuple[EvidenceRef, ...]
    claim_indexes: tuple[Nonnegative, ...]
    cause_ids: tuple[UUID, ...]


class VerdictData(Contract):
    applicability: Judgment
    complete_support: Judgment
    conflicts_resolved: Judgment


class Verdict(Sealed):
    kind: Literal["maintenance_verdict"] = "maintenance_verdict"
    proposal: SealRef
    hard_check: SealRef
    verification: SealRef
    data: VerdictData
    configuration_sha256: str = Field(pattern="^[0-9a-f]{64}$")


def parse_verdict(raw, proposal, bundle):
    data = VerdictData.model_validate_json(raw)
    allowed = tuple(r.evidence for r in bundle.read)
    for item in (data.applicability, data.complete_support, data.conflicts_resolved):
        if any(e not in allowed for e in item.evidence):
            raise ContractError("verifier_evidence_not_read")
        if any(i >= len(proposal.data.claims) for i in item.claim_indexes) or any(
            i not in {c.cause.event_id for c in proposal.data.causes}
            for i in item.cause_ids
        ):
            raise ContractError("verifier_conclusion_reference_invalid")
        if item.verdict == "PASS" and not item.evidence:
            raise ContractError("verifier_pass_without_evidence")
    if data.complete_support.verdict == "PASS" and set(
        data.complete_support.claim_indexes
    ) != set(range(len(proposal.data.claims))):
        raise ContractError("verifier_incomplete_conclusions")
    if data.conflicts_resolved.verdict == "PASS" and set(
        data.conflicts_resolved.cause_ids
    ) != {c.cause.event_id for c in proposal.data.causes}:
        raise ContractError("verifier_incomplete_causes")
    return data


class FixedVerifier(ModelCall):
    def __init__(self, config, *, checks, **kwargs):
        super().__init__(config, **kwargs)
        self.checks = checks
        # A live successful invocation grants one submission attempt. Archived
        # records/proofs cannot mint this authority. No restart recovery claim.
        self._grants = {}

    async def verify(self, proposal_ref, check_ref, *, frame, binding):
        repo, artifacts = self.checks.executor.repo, self.checks.artifacts
        # The caller already owns these sealed inputs. Use them for accounting
        # before any fallible read; they confer no verification/commit authority.
        if (
            not (
                proposal_ref.account_id
                == check_ref.account_id
                == frame.account_id
                == binding.account_id
            )
            or binding.frame != frame.reference()
        ):
            raise ContractError("verifier_accounting_context_mismatch")
        work = LocalOperation(
            self.recorder,
            call_identity(frame, binding, "verifier-local", self.config, "local"),
            {
                "proposal": proposal_ref.model_dump(mode="json"),
                "check": check_ref.model_dump(mode="json"),
                "frame": frame.reference().model_dump(mode="json"),
                "action": binding.reference().model_dump(mode="json"),
            },
        )
        await work.start()
        try:
            async with repo.session(proposal_ref.account_id) as raw:
                db = MeteredDB(raw, work)
                proposal = await artifacts.get(db, proposal_ref)
                actual_binding = await artifacts.get(db, proposal.action)
                actual_frame = await artifacts.get(db, actual_binding.frame)
                if actual_binding != binding or actual_frame != frame:
                    raise ContractError("verifier_accounting_context_mismatch")
            result = await self._verify(
                proposal_ref, check_ref, proposal, binding, frame, work
            )
        except BaseException as exc:
            code = (
                exc.code
                if isinstance(exc, ContractError)
                else (
                    "verifier_cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "verifier_local_failed"
                )
            )
            await work.finish({}, code)
            raise
        await work.finish({"verdict": result.model_dump(mode="json")})
        if all(
            getattr(result.data, k).verdict == "PASS"
            for k in ("applicability", "complete_support", "conflicts_resolved")
        ):
            self._grants[result.reference()] = (
                proposal_ref,
                check_ref,
                binding.reference(),
            )
        return result

    async def _verify(self, proposal_ref, check_ref, proposal, binding, frame, work):
        repo, artifacts = self.checks.executor.repo, self.checks.artifacts
        async with repo.session(proposal_ref.account_id) as raw:
            db = MeteredDB(raw, work)
            bundle = await artifacts.get(db, binding.bundle)
            check = await artifacts.get(db, check_ref)
            current = await self.checks.check(db, proposal_ref, persist=False)
            work.add("hard_checks", len(current.checks))
            if (
                check.candidate != proposal.candidate
                or not passed(check)
                or not passed(current)
            ):
                raise ContractError("hard_checks_not_passed")
            sources = await self.checks.sources(db, frame, bundle)
        # Deliberate whitelist: no profile/stage, producer scores, questions,
        # offline labels, or previous generator/verifier response is included.
        payload = {
            "candidate": proposal.data.model_dump(mode="json"),
            "bundle": bundle.model_dump(mode="json"),
            "allowed_sources": sources,
            "hard_checks": check.model_dump(mode="json"),
        }

        def parse(raw):
            work.add("rule_checks")
            return parse_verdict(raw, proposal, bundle)

        data, ident = await self.invoke(
            frame,
            binding,
            payload,
            parse,
            "verifier",
        )
        candidate = None
        async with repo.session(frame.account_id) as raw:
            db = MeteredDB(raw, work)
            candidate = await artifacts.get(db, proposal.candidate)
            if candidate.generator_call_id == ident.call_id:
                raise ContractError("generator_cannot_verify")
            verdict = VerificationResult(
                account_id=frame.account_id,
                id=uuid4(),
                candidate=proposal.candidate,
                bundle=binding.bundle,
                verifier_call_id=ident.call_id,
                verifier_config_sha256=canonical_hash(self.config),
                **{
                    name: CheckItem(
                        code=name,
                        verdict=getattr(data, name).verdict,
                        reason=getattr(data, name).reason,
                    )
                    for name in (
                        "applicability",
                        "complete_support",
                        "conflicts_resolved",
                    )
                },
            )
            work.add("audit_records")  # attempted seals, including rollback/failure
            await artifacts.seal(db, verdict)
            result = Verdict(
                account_id=frame.account_id,
                id=uuid4(),
                proposal=proposal_ref,
                hard_check=check_ref,
                verification=verdict.reference(),
                data=data,
                configuration_sha256=canonical_hash(self.config),
            )
            work.add("audit_records")
            await artifacts.seal(db, result)
        return result

    def consume(self, verdict_ref, proposal_ref, check_ref, binding_ref):
        grant = self._grants.pop(verdict_ref, None)
        if grant != (proposal_ref, check_ref, binding_ref):
            raise ContractError("live_verifier_grant_required")
