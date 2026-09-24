"""Evidence-bound dual weak judgments and mandatory/optional strong review."""

import json
from typing import Literal
from uuid import UUID, uuid5
from pydantic import Field
from contexthub.discovery.extraction import ExtractionConfig, error_code
from contexthub.discovery.planner import Plan, choose_price
from contexthub.discovery.stage0 import Stage0Config, Mention, apply_stage0
from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.execution import PriceBasis
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    EvidenceRef,
    Hash,
    Name,
    Nonnegative,
    canonical_hash,
)
from contexthub.services.evidence_service import EvidenceService


class GroundedStep(Contract):
    premises: tuple[EvidenceRef, ...]
    previous_steps: tuple[Nonnegative, ...]
    conclusion: EvidenceRef


class Judgment(Contract):
    verdict: Literal["SAME", "DIFFERENT", "DEPENDS", "INDEPENDENT", "UNKNOWN"]
    evidence_sha256: Hash
    citations: tuple[EvidenceRef, ...]
    # Every intermediate claim must quote saved original evidence. The final
    # relationship remains a semantic judgment, never certified by confidence.
    steps: tuple[GroundedStep, ...]
    negative_basis: Literal[
        "none",
        "explicit_non_use",
        "complete_derivation_excludes_upstream",
        "identity_exclusion",
        "alternative_support",
    ]
    exclusion: tuple[EvidenceRef, ...]
    conflicts: tuple[Name, ...]
    missing: tuple[Name, ...]
    semantic_basis: Name


class EvidenceSet(Contract):
    evidence: tuple[EvidenceRef, ...]
    complete: bool
    at_max_scope: bool
    issues: tuple[Name, ...]


class AdjudicationConfig(Contract):
    id: Name
    weak: ExtractionConfig
    strong: ExtractionConfig
    stage0: Stage0Config
    plans: tuple[Plan, ...]
    proxy_version: Name
    cost_formula_version: Name
    lambda_p1: float = Field(ge=0, allow_inf_nan=False)
    weak_price: PriceBasis | None
    strong_price: PriceBasis | None


class JudgmentTrace(Contract):
    role: Name
    call_id: UUID
    payload_sha256: Hash
    evidence_sha256: Hash
    judgment: Judgment | None
    errors: tuple[Name, ...]


class Adjudication(Contract):
    kind: Literal["entity", "dependency"]
    verdict: Literal["SAME", "DIFFERENT", "DEPENDS", "INDEPENDENT", "UNKNOWN"]
    action: Literal["complete", "read_more", "pending"]
    reason: Name
    evidence_sha256: Hash
    traces: tuple[JudgmentTrace, ...]
    accepted: Judgment | None = None
    stage0: dict | None = None
    plan_id: Name | None = None
    generation: Nonnegative


class AdjudicationService:
    def __init__(self, config, *, api_key, recorder, transport=None, artifacts=None, clients=None):
        self.config, self.recorder = config, recorder
        self.evidence = EvidenceService(artifacts)
        self.clients = clients if clients is not None else {}
        if clients is not None:
            if set(clients) != {"weak", "strong"}:
                raise ContractError("adjudication_clients_incomplete")
            return
        for role, cfg in (("weak", config.weak), ("strong", config.strong)):
            self.clients[role] = OpenAIChatClient(
                api_key,
                base_url=cfg.base_url,
                model=cfg.model,
                temperature=cfg.temperature,
                timeout=cfg.retry.timeout_seconds,
                transport=transport,
            )

    async def close(self):
        for client in self.clients.values():
            await client.close()

    async def judge(
        self,
        db,
        *,
        kind,
        left,
        right,
        bundle: EvidenceSet,
        identity,
        generation=0,
        explicit_provenance=False,
        mentions: tuple[Mention, Mention] | None = None,
    ):
        if identity.config_sha256 != canonical_hash(self.config):
            raise ContractError("adjudication_config_mismatch")
        digest = canonical_hash(bundle)
        traces = []
        work = LocalOperation(
            self.recorder,
            identity.model_copy(
                update={
                    "call_id": uuid5(identity.call_id, "adjudication-local"),
                    "model_role": "local",
                    "operation": "adjudication_checks",
                }
            ),
            {"evidence": digest, "kind": kind, "left": left, "right": right},
        )
        await work.start()
        db = MeteredDB(db, work)
        stage0 = None

        async def result(verdict, action, reason, accepted=None, plan=None):
            out = Adjudication(
                kind=kind,
                verdict=verdict,
                action=action,
                reason=reason,
                evidence_sha256=digest,
                traces=tuple(traces),
                accepted=accepted,
                stage0=stage0,
                plan_id=plan.id if plan else None,
                generation=generation,
            )
            work.add("audit_records")
            await work.finish(out, reason if action == "pending" else None)
            return out

        try:
            if not bundle.complete or bundle.issues:
                return await result(
                    "UNKNOWN",
                    "pending" if bundle.at_max_scope else "read_more",
                    "search_incomplete",
                )
            for ref in bundle.evidence:
                await self.evidence.resolve(db, ref)
                work.add("hard_checks")
            if kind == "entity" and mentions:
                if any(
                    not any(
                        m.evidence.source == e.source
                        and e.start <= m.evidence.start < m.evidence.end <= e.end
                        for e in bundle.evidence
                    )
                    for m in mentions
                ):
                    raise ContractError("mention_outside_evidence")
                rule = apply_stage0(self.config.stage0, *mentions, bundle.evidence)
                stage0 = rule.model_dump(mode="json")
                work.add(
                    "rule_checks",
                    len(self.config.stage0.rules) + len(self.config.stage0.aliases),
                )
                if rule.verdict != "UNRESOLVED":
                    # DIFFERENT never removes a provenance dependency candidate.
                    return await result(rule.verdict, "complete", "stage0_rule")

            async def invoke(role, previous=()):
                strong = role == "strong"
                cfg = self.config.strong if strong else self.config.weak
                swapped = kind == "entity" and role == "challenge"
                evidence = (
                    tuple(reversed(bundle.evidence))
                    if role == "challenge"
                    else bundle.evidence
                )
                payload = {
                    "kind": kind,
                    "role": role,
                    "left": right if swapped else left,
                    "right": left if swapped else right,
                    "evidence_sha256": digest,
                    "evidence": [e.model_dump(mode="json") for e in evidence],
                    "counterexamples": (
                        ["type", "time", "place", "role"]
                        if kind == "entity"
                        else [
                            "delete_upstream",
                            "modify_upstream",
                            "alternative_source_does_not_disprove_use",
                        ]
                    )
                    if role == "challenge"
                    else [],
                    "explicit_provenance": explicit_provenance,
                }
                # Only strong receives prior responses. Both weak requests are built
                # from original inputs, with no conversation history or answer slot.
                if strong:
                    payload["prior_judgments"] = [
                        t.model_dump(mode="json") for t in previous
                    ]
                call_id = uuid5(identity.call_id, role)
                ident = identity.model_copy(
                    update={
                        "call_id": call_id,
                        "model_role": role,
                        "operation": "judge_" + kind,
                    }
                )
                judgment, errors = None, []
                try:
                    raw = await self.clients["strong" if strong else "weak"].complete(
                        cfg.prompt_template.format(
                            sources_json=json.dumps(payload, ensure_ascii=False)
                        ),
                        max_tokens=cfg.max_tokens,
                        call=RecordedCall(
                            identity=ident,
                            policy=cfg.retry,
                            recorder=self.recorder,
                            price=self.config.strong_price
                            if strong
                            else self.config.weak_price,
                            validate_response=Judgment.model_validate_json,
                        ),
                    )
                    judgment = Judgment.model_validate_json(raw)
                    allowed = (
                        {"SAME", "DIFFERENT", "UNKNOWN"}
                        if kind == "entity"
                        else {"DEPENDS", "INDEPENDENT", "UNKNOWN"}
                    )
                    if (
                        judgment.verdict not in allowed
                        or judgment.evidence_sha256 != digest
                    ):
                        errors.append("judgment_identity_mismatch")
                    for index, step in enumerate(judgment.steps):
                        if (not step.premises and not step.previous_steps) or any(
                            i >= index for i in step.previous_steps
                        ):
                            errors.append("unsupported_intermediate_step")
                    step_refs = tuple(
                        ref
                        for step in judgment.steps
                        for ref in (*step.premises, step.conclusion)
                    )
                    for ref in (*judgment.citations, *step_refs, *judgment.exclusion):
                        # Narrower citations are allowed only inside a read span.
                        if not any(
                            ref.source == e.source
                            and e.start <= ref.start < ref.end <= e.end
                            for e in bundle.evidence
                        ):
                            errors.append("citation_outside_read_evidence")
                            continue
                        await self.evidence.resolve(db, ref)
                        work.add("hard_checks")
                    if judgment.verdict != "UNKNOWN" and (
                        not judgment.citations or not judgment.steps
                    ):
                        errors.append("unsupported_derivation")
                    if (
                        judgment.verdict in ("DIFFERENT", "INDEPENDENT")
                        and not judgment.exclusion
                    ):
                        errors.append("exclusion_required")
                    if (
                        judgment.verdict == "INDEPENDENT"
                        and judgment.negative_basis
                        not in (
                            "explicit_non_use",
                            "complete_derivation_excludes_upstream",
                        )
                    ):
                        errors.append("alternative_support_is_not_exclusion")
                    if (
                        judgment.verdict == "DIFFERENT"
                        and judgment.negative_basis != "identity_exclusion"
                    ):
                        errors.append("identity_exclusion_required")
                    if judgment.verdict in ("DIFFERENT", "INDEPENDENT") and not all(
                        e in step_refs for e in judgment.exclusion
                    ):
                        errors.append("exclusion_derivation_missing")
                    if (
                        kind == "dependency"
                        and explicit_provenance
                        and judgment.verdict == "INDEPENDENT"
                    ):
                        errors.append("provenance_cannot_be_deleted")
                    if judgment.conflicts:
                        errors.append("unresolved_conflict")
                except Exception as exc:
                    errors.append(error_code(exc))
                trace = JudgmentTrace(
                    role=role,
                    call_id=call_id,
                    payload_sha256=canonical_hash(payload),
                    evidence_sha256=digest,
                    judgment=judgment,
                    errors=tuple(errors),
                )
                traces.append(trace)
                return trace

            def missing(trace):
                return bool(trace.judgment and trace.judgment.missing)

            async def missing_result():
                return await result(
                    "UNKNOWN",
                    "pending" if bundle.at_max_scope else "read_more",
                    "missing_premise_at_max"
                    if bundle.at_max_scope
                    else "missing_evidence",
                )

            first = await invoke("normal")
            if missing(first):
                return await missing_result()
            # Transport/schema failures are not retried at this level or hidden as
            # a new same-level call. They terminate this candidate, costs retained.
            if first.judgment is None:
                return await result("UNKNOWN", "pending", "weak_call_failed")
            second = await invoke("challenge")
            if missing(second):
                return await missing_result()
            if second.judgment is None:
                return await result("UNKNOWN", "pending", "weak_call_failed")
            accepted = (
                not first.errors
                and not second.errors
                and first.judgment.verdict == second.judgment.verdict != "UNKNOWN"
            )
            plan = (
                choose_price(self.config.plans, self.config.lambda_p1)
                if accepted
                else None
            )
            if accepted and plan is None:
                return await result("UNKNOWN", "pending", "no_legal_complete_plan")
            if accepted and not plan.optional_strong:
                return await result(
                    first.judgment.verdict,
                    "complete",
                    "dual_weak",
                    first.judgment,
                    plan,
                )
            strong = await invoke("strong", tuple(traces))
            if missing(strong):
                return await missing_result()
            if (
                strong.errors
                or strong.judgment is None
                or strong.judgment.verdict == "UNKNOWN"
            ):
                return await result(
                    "UNKNOWN", "pending", "strong_unresolved", plan=plan
                )
            if plan is None:
                plan = choose_price(self.config.plans, self.config.lambda_p1)
            if plan is None:
                return await result("UNKNOWN", "pending", "no_legal_complete_plan")
            return await result(
                strong.judgment.verdict,
                "complete",
                "optional_strong" if accepted else "mandatory_strong",
                strong.judgment,
                plan,
            )
        except Exception as exc:
            return await result("UNKNOWN", "pending", error_code(exc))
