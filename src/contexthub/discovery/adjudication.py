"""Evidence-bound dual weak judgments and mandatory/optional strong review."""

import json
from typing import Literal
from uuid import UUID, uuid5
from pydantic import Field, model_validator
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


class IdentifiedStep(Contract):
    premise_ids: tuple[Name, ...]
    previous_steps: tuple[Nonnegative, ...]
    conclusion_id: Name


class IdentifiedJudgment(Contract):
    verdict: Literal["SAME", "DIFFERENT", "DEPENDS", "INDEPENDENT", "UNKNOWN"]
    citation_ids: tuple[Name, ...]
    steps: tuple[IdentifiedStep, ...]
    negative_basis: Literal[
        "none", "explicit_non_use", "complete_derivation_excludes_upstream",
        "identity_exclusion", "alternative_support",
    ]
    exclusion_ids: tuple[Name, ...]
    conflicts: tuple[Name, ...]
    missing: tuple[Name, ...]
    semantic_basis: Name


def reference_registry(evidence):
    # IDs belong to this immutable bundle, independent of challenge presentation order.
    registry, reverse = {}, {}
    for ref in evidence:
        digest = canonical_hash(ref)
        if digest not in reverse:
            ident = f"E{len(registry) + 1:06d}"
            reverse[digest] = ident
            registry[ident] = ref
    return registry, reverse


def expand_judgment(raw, registry, digest):
    selected = IdentifiedJudgment.model_validate_json(raw)

    def resolve(ident):
        if ident not in registry:
            raise ContractError("undeclared_adjudication_evidence_id")
        return registry[ident]

    def resolve_many(ids):
        if len(ids) != len(set(ids)):
            raise ContractError("duplicate_adjudication_evidence_id")
        return tuple(resolve(ident) for ident in ids)

    return Judgment(
        verdict=selected.verdict, evidence_sha256=digest,
        citations=resolve_many(selected.citation_ids),
        steps=tuple(GroundedStep(premises=resolve_many(step.premise_ids),
                                previous_steps=step.previous_steps,
                                conclusion=resolve(step.conclusion_id))
                    for step in selected.steps),
        negative_basis=selected.negative_basis,
        exclusion=resolve_many(selected.exclusion_ids),
        conflicts=selected.conflicts, missing=selected.missing,
        semantic_basis=selected.semantic_basis,
    )


def compact_judgment(judgment, reverse):
    def ident(ref):
        return reverse[canonical_hash(ref)]
    return IdentifiedJudgment(
        verdict=judgment.verdict,
        citation_ids=tuple(ident(e) for e in judgment.citations),
        steps=tuple(IdentifiedStep(premise_ids=tuple(ident(e) for e in step.premises),
                                  previous_steps=step.previous_steps,
                                  conclusion_id=ident(step.conclusion))
                    for step in judgment.steps),
        negative_basis=judgment.negative_basis,
        exclusion_ids=tuple(ident(e) for e in judgment.exclusion),
        conflicts=judgment.conflicts, missing=judgment.missing,
        semantic_basis=judgment.semantic_basis,
    ).model_dump(mode="json")


class EvidenceSet(Contract):
    evidence: tuple[EvidenceRef, ...]
    complete: bool
    at_max_scope: bool
    issues: tuple[Name, ...]


class AdjudicationConfig(Contract):
    id: Name
    response_contract: Literal["references-v1", "evidence-ids-v2"] = Field(
        default="references-v1", exclude_if=lambda value: value == "references-v1"
    )
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
    action: Literal["complete", "read_more", "pending", "not_selected"]
    reason: Name
    evidence_sha256: Hash
    traces: tuple[JudgmentTrace, ...]
    accepted: Judgment | None = None
    stage0: dict | None = None
    plan_id: Name | None = None
    generation: Nonnegative

    @model_validator(mode="after")
    def omission_is_not_a_verdict(self):
        if self.action == "not_selected" and (
            self.kind != "dependency" or self.verdict != "UNKNOWN" or self.accepted is not None
        ):
            raise ContractError("not_selected_cannot_certify_independence")
        return self


class IdentityResolution(Contract):
    mentions: tuple[Mention, Mention]
    result: Adjudication


async def validate_judgment(db, judgment, *, kind, bundle, evidence_service, work,
                            explicit_provenance=False):
    """Shared per-item acceptance checks for single and batch transports."""
    errors = []
    digest = canonical_hash(bundle)
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
        await evidence_service.resolve(db, ref)
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
    return errors


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
                request_parameters=cfg.request_parameters,
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
        identity_resolutions: tuple[IdentityResolution, ...] = (),
    ):
        if identity.config_sha256 != canonical_hash(self.config):
            raise ContractError("adjudication_config_mismatch")
        digest = canonical_hash(bundle)
        for resolution in identity_resolutions:
            r = resolution.result
            allowed_reference = ((r.action == "complete" and r.verdict in ("SAME", "DIFFERENT"))
                                 or (r.action == "pending" and r.verdict == "UNKNOWN"))
            if (kind != "dependency" or r.kind != "entity" or not allowed_reference
                    or r.evidence_sha256 != digest
                    or r.generation != generation):
                raise ContractError("identity_resolution_not_current_complete")
            for mention in resolution.mentions:
                if not any(mention.evidence.source == e.source
                           and e.start <= mention.evidence.start < mention.evidence.end <= e.end
                           for e in bundle.evidence):
                    raise ContractError("identity_resolution_outside_evidence")
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
            {"evidence": digest, "kind": kind, "left": left, "right": right,
             **({"identity_resolutions": [r.model_dump(mode="json") for r in identity_resolutions]}
                if identity_resolutions else {})},
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

            registry, reverse = reference_registry(bundle.evidence)
            by_id = self.config.response_contract == "evidence-ids-v2"

            def parse(raw):
                return (expand_judgment(raw, registry, digest) if by_id
                        else Judgment.model_validate_json(raw))

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
                if identity_resolutions:
                    payload["identity_resolutions"] = [
                        dict(mentions=[m.model_dump(mode="json") for m in r.mentions],
                             verdict=r.result.verdict, evidence_sha256=r.result.evidence_sha256,
                             accepted=(compact_judgment(r.result.accepted, reverse) if by_id
                                       else r.result.accepted.model_dump(mode="json"))
                                       if r.result.accepted is not None and r.result.verdict != "UNKNOWN" else None,
                             stage0=r.result.stage0,
                             **(dict(action=r.result.action, reason=r.result.reason,
                                     uncertainty="identity_not_established_do_not_assume_same_or_different",
                                     failure_notes=[dict(role=t.role,call_id=str(t.call_id),errors=list(t.errors))
                                                    for t in r.result.traces])
                                if r.result.verdict == "UNKNOWN" else {}))
                        for r in identity_resolutions
                    ]
                # Only strong receives prior responses. Both weak requests are built
                # from original inputs, with no conversation history or answer slot.
                if strong:
                    payload["prior_judgments"] = [
                        t.model_dump(mode="json") for t in previous
                    ]
                if by_id:
                    payload["evidence"] = [
                        dict(id=reverse[canonical_hash(e)], evidence=e.model_dump(mode="json"))
                        for e in evidence
                    ]
                    if strong:
                        payload["prior_judgments"] = [
                            dict(role=t.role, errors=list(t.errors),
                                 judgment=compact_judgment(t.judgment, reverse)
                                 if t.judgment is not None else None)
                            for t in previous
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
                            validate_response=parse,
                        ),
                    )
                    judgment = parse(raw)
                    errors.extend(await validate_judgment(
                        db, judgment, kind=kind, bundle=bundle, evidence_service=self.evidence,
                        work=work, explicit_provenance=explicit_provenance))
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
