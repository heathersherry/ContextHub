"""Independent offline P1/P2/answer scoring. Runtime verifier is never truth."""

from collections import Counter
from typing import Literal
from uuid import UUID
from pydantic import Field
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    ContextVersion,
    EvidenceRef,
    Hash,
    Name,
    SourceSnapshot,
    canonical_hash,
    text_hash,
)
from .schemas import ScoringSidecar, canonical_sha256
from .scoring_contract import validate_scoring_contract
from .rq1_scorer import score_rq1, _path_reason
from .rq2_coordinator import PROFILES
from .official_adapter import classify


class CaseReference(Contract):
    record_id: Name
    label_sha256: Hash
    # Established from reference materials, once for all profiles/repeats.
    recoverable_cas: bool | None
    reviewer_id: Name
    review_method: Literal["offline_human_semantic_review"]
    review_version: Name
    rationale: Name
    reference_material_sha256: Hash


class P2Review(Contract):
    record_id: Name
    label_sha256: Hash
    run_id: UUID
    replicate_id: int = Field(ge=0, strict=True)
    profile: Literal["local/base-only", "full/strong-only", "adaptive"]
    graph_sha256: Hash
    state_sha256: Hash
    before_target: ContextVersion
    final_target: ContextVersion
    verdict: Literal["CORRECT", "INCORRECT", "UNKNOWN"]
    reviewer_id: Name
    review_method: Literal["offline_human_semantic_review"]
    review_version: Name
    rationale: Name
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)


def validate_seal(seal):
    payload = dict(seal)
    digest = payload.pop("sha256", None)
    if canonical_sha256(payload) != digest or seal.get("phase") not in (
        "before",
        "after",
    ):
        raise ContractError("score_state_hash_mismatch")
    return seal


def review_evidence(review, seal):
    state = seal["state"]
    artifacts = {a["id"]: a for a in state["artifacts"]}
    versions = {(v["context_id"], v["version"]): v for v in state["context_versions"]}
    for evidence in review.evidence:
        row = artifacts.get(str(evidence.source.id))
        if (
            row is None
            or row["sha256"] != evidence.source.sha256
            or canonical_hash(row["payload"]) != row["sha256"]
        ):
            return False
        source = SourceSnapshot.model_validate(row["payload"])
        if source.reference() != evidence.source:
            return False
        version = versions.get((str(source.source.context_id), source.source.version))
        if version is None:
            return False
        text = version[source.content_level]
        if (
            not isinstance(text, str)
            or text_hash(text) != source.content_sha256
            or text[evidence.start : evidence.end] != evidence.excerpt
        ):
            return False
    return True


def rate(n, d):
    return n / d if d else None


def metrics(rows):
    """Unknown judgments fail correctness numerators, never remove denominators."""
    fresh = [r for r in rows if r["fresh"] is True]
    wrong = sum(r["false_fresh"] for r in rows)
    recoverable = [
        r for r in rows if r["task_type"] == "Cas" and r["recoverable_cas"] is True
    ]
    recovered = sum(r["p2_correct"] for r in recoverable)
    abs_rows = [r for r in rows if r["task_type"] == "Abs"]
    unknown_fresh = sum(r["fresh"] is None for r in rows)
    unreviewed_fresh = sum(
        r["fresh"] is True and r["p2_verdict"] == "UNKNOWN" for r in rows
    )
    unknown_recoverability = sum(
        r["task_type"] == "Cas" and r["recoverable_cas"] is None for r in rows
    )
    quality = {}
    for kind in ("Cas", "Abs"):
        subset = [r for r in rows if r["task_type"] == kind]
        quality["internal_" + kind] = rate(
            sum(r["p2_correct"] for r in subset), len(subset)
        )
        quality["official_" + kind] = rate(
            sum(r["official_class"] == "real" for r in subset), len(subset)
        )
    rho = rate(recovered, len(recoverable))
    # N/A is not zero, and unknown fresh/recoverability cannot certify a gate.
    ff_gate = None if unknown_fresh or unreviewed_fresh else wrong == 0
    rho_gate = None if unknown_recoverability or rho is None else rho >= 0.9
    abs_correct = sum(r["correct_unresolved"] for r in abs_rows)
    return dict(
        total=len(rows),
        p1_correct=sum(r["p1_correct"] for r in rows),
        p2_correct=sum(r["p2_correct"] for r in rows),
        joint_correct=sum(r["p1_correct"] and r["p2_correct"] for r in rows),
        joint_rate=rate(
            sum(r["p1_correct"] and r["p2_correct"] for r in rows), len(rows)
        ),
        actual_fresh=len(fresh),
        unknown_fresh=unknown_fresh,
        unreviewed_fresh=unreviewed_fresh,
        false_fresh_count=wrong,
        false_fresh_main=rate(wrong, len(fresh)),
        false_fresh_all=rate(wrong, len(rows)),
        recoverable_cas=len(recoverable),
        unknown_recoverability=unknown_recoverability,
        cas_recovered=recovered,
        cas_not_recovered=len(recoverable) - recovered,
        rho_p2=rho,
        abs_correct_unresolved=abs_correct,
        abs_total=len(abs_rows),
        quality=quality,
        official_classes=dict(Counter(r["official_class"] for r in rows)),
        tau_ff_pass=ff_gate,
        rho_p2_pass=rho_gate,
        zero_fresh_rule=(
            "N/A: inspect raw errors, Cas recovery and Abs correctly unresolved"
            if not fresh
            else None
        ),
        abs_zero_fresh_acceptable=(
            abs_correct == len(abs_rows) and bool(abs_rows) and ff_gate is True
        )
        if not fresh and abs_rows and not recoverable
        else None,
    )


def score_report(
    *,
    contract,
    labels,
    paths,
    p1_graphs,
    p1_reviews,
    references,
    p2_reviews,
    states,
    answers,
    judgments,
    run_id,
    repeats,
    official_code_sha256,
    judge_configuration_sha256,
    episode_ids=None,
):
    """Mappings: P1 (arm,replicate)->episode->GraphEvidence; states/answers
    (profile,replicate,episode,phase). P2 always uses selective replicate 0.
    Missing runs and reviews are materialized for every frozen record.
    """
    validate_scoring_contract(contract)
    if type(repeats) is not int or repeats < 1:
        raise ContractError("explicit_repeat_protocol_required")
    labels = [
        ScoringSidecar.model_validate(
            x.model_dump(mode="json", exclude_none=True)
            if isinstance(x, ScoringSidecar)
            else x
        )
        for x in labels
    ]
    universe = contract["frozen_record_universe"]["record_ids"]
    by_id = {r.record_id: r for r in labels}
    if len(by_id) != len(labels) or set(by_id) != set(universe):
        raise ContractError("score_labels_must_match_all_frozen_records")
    parent_denominator = len(universe)
    if episode_ids is not None:
        selected = set(episode_ids)
        if (
            not selected
            or len(selected) != len(episode_ids)
            or not selected <= {r.episode_id for r in labels}
            or any(key[2] not in selected for key in (*states, *answers))
        ):
            raise ContractError("score_episode_view_invalid")
        universe = [r for r in universe if by_id[r].episode_id in selected]
        by_id = {r: by_id[r] for r in universe}
    refs = {r.record_id: r for r in references}
    reference_paths = {r["record_id"]: r for r in paths}
    if len(refs) != len(references) or set(refs) - set(universe):
        raise ContractError("reference_membership_invalid")
    for record, ref in refs.items():
        if (
            ref.label_sha256 != by_id[record].record_sha256
            or ref.reference_material_sha256 != reference_paths[record]["record_sha256"]
        ) or (by_id[record].task_type == "Abs" and ref.recoverable_cas is not None):
            raise ContractError("reference_label_mismatch")
    p1 = {}
    for arm in ("selective", "full-history"):
        for rep in range(repeats):
            p1[(arm, rep)] = score_rq1(
                contract,
                paths,
                p1_graphs.get((arm, rep), {}),
                p1_reviews.get((arm, rep), ()),
                episode_ids=episode_ids,
            )
            if {r["record_id"] for r in p1[(arm, rep)]["rows"]} != set(universe):
                raise ContractError("score_path_episode_membership_mismatch")
    for key, seal in states.items():
        validate_seal(seal)
        if (
            seal["profile"],
            seal["replicate_id"],
            seal["episode_id"],
            seal["phase"],
        ) != key or seal["run_id"] != str(run_id):
            raise ContractError("score_state_run_identity_mismatch")
    review_map = {(r.profile, r.replicate_id, r.record_id): r for r in p2_reviews}
    if len(review_map) != len(p2_reviews) or any(
        r.record_id not in by_id or r.replicate_id >= repeats or r.run_id != run_id
        for r in p2_reviews
    ):
        raise ContractError("p2_review_membership_invalid")
    judgment_map = {
        (r.profile, r.replicate_id, r.record_id, r.phase): r for r in judgments
    }
    if len(judgment_map) != len(judgments) or any(
        r.record_id not in by_id or r.replicate_id >= repeats or r.run_id != str(run_id)
        for r in judgments
    ):
        raise ContractError("judge_membership_invalid")
    main = {r["record_id"]: r for r in p1[("selective", 0)]["rows"]}
    path_map = {r["record_id"]: r for r in paths}
    rows = []
    for profile in PROFILES:
        for rep in range(repeats):
            for record in universe:
                label, p1row = by_id[record], main[record]
                graph = p1_graphs.get(("selective", 0), {}).get(label.episode_id)
                seal = states.get((profile, rep, label.episode_id, "after"))
                review = review_map.get((profile, rep, record))
                before_target = None
                if p1row["covered"]:
                    valid = next(
                        r
                        for r in p1_reviews[("selective", 0)]
                        if r.record_id == record
                        and _path_reason(path_map[record], r, graph) is None
                    )
                    before_target = valid.nodes[-1]
                if review is not None:
                    if (
                        review.label_sha256 != label.record_sha256
                        or graph is None
                        or review.graph_sha256 != graph.reference.sha256
                        or review.before_target not in graph.graph.nodes
                        or (
                            before_target is not None
                            and review.before_target != before_target
                        )
                        or review.final_target.context_id
                        != review.before_target.context_id
                        or seal is None
                        or review.state_sha256 != seal["sha256"]
                        or not review_evidence(review, seal)
                    ):
                        raise ContractError("p2_review_evidence_or_identity_mismatch")
                    before_target = review.before_target
                target = None
                if seal is not None:
                    if graph is None or seal["graph"] != graph.reference.model_dump(
                        mode="json"
                    ):
                        raise ContractError("p2_state_not_selective_main_graph")
                    if before_target is not None:
                        target = next(
                            (
                                c
                                for c in seal["state"]["contexts"]
                                if c["id"] == str(before_target.context_id)
                            ),
                            None,
                        )
                fresh = None
                verdict = "UNKNOWN"
                if target is not None:
                    # A work-item status or verifier PASS does not establish fresh.
                    unresolved = any(
                        i["context_id"] == target["id"] and i.get("resolved_at") is None
                        for i in seal["state"]["invalidations"]
                    )
                    fresh = (
                        target["validity_status"] == "fresh"
                        and target["status"] == "active"
                        and target["stale_at"] is None
                        and target["deleted_at"] is None
                        and not unresolved
                    )
                    if review is not None:
                        if review.final_target.version != target["version"]:
                            raise ContractError("p2_review_final_version_mismatch")
                        verdict = review.verdict
                correct = verdict == "CORRECT" and (
                    (label.task_type == "Cas" and fresh is True)
                    or (
                        label.task_type == "Abs"
                        and fresh is False
                        and target is not None
                        and target["deleted_at"] is None
                        and target["status"] in ("active", "stale")
                        and (target["validity_status"] == "stale" or unresolved)
                    )
                )
                wrong_fresh = fresh is True and (
                    verdict == "INCORRECT"
                    or (label.task_type == "Abs" and verdict != "UNKNOWN")
                )
                official = []
                for phase in ("before", "after"):
                    answer_rows = answers.get(
                        (profile, rep, label.episode_id, phase), []
                    )
                    if len({a["question_id"] for a in answer_rows}) != len(answer_rows):
                        raise ContractError("duplicate_answer")
                    answer = next(
                        (
                            a
                            for a in answer_rows
                            if a["question_id"]
                            == getattr(label, phase + "_question_id")
                        ),
                        None,
                    )
                    judgment = judgment_map.get((profile, rep, record, phase))
                    state = states.get((profile, rep, label.episode_id, phase))
                    if judgment is not None and (
                        answer is None
                        or state is None
                        or judgment.label_sha256 != label.record_sha256
                        or judgment.question_id
                        != getattr(label, phase + "_question_id")
                        or judgment.answer_sha256 != canonical_hash(answer)
                        or judgment.state_sha256 != state["sha256"]
                        or answer["state_sha256"] != state["sha256"]
                        or judgment.official_code_sha256 != official_code_sha256
                        or judgment.configuration_sha256 != judge_configuration_sha256
                    ):
                        raise ContractError("official_judgment_binding_mismatch")
                    official.append(
                        False
                        if answer is None
                        or answer.get("error")
                        or not answer.get("answer", "").strip()
                        else judgment.correct
                        if judgment
                        else None
                    )
                rows.append(
                    dict(
                        record_id=record,
                        episode_id=label.episode_id,
                        task_type=label.task_type,
                        profile=profile,
                        replicate_id=rep,
                        p1_correct=p1row["covered"],
                        p2_correct=correct,
                        p2_verdict=verdict,
                        fresh=fresh,
                        false_fresh=wrong_fresh,
                        correct_unresolved=correct and label.task_type == "Abs",
                        recoverable_cas=refs[record].recoverable_cas
                        if record in refs
                        else None,
                        official_before=official[0],
                        official_after=official[1],
                        official_class=classify(*official),
                        retained_in_denominator=True,
                        data_quality_notes=list(label.data_quality_notes or ()),
                        missing_state=seal is None,
                        missing_review=review is None,
                    )
                )
    summaries = profile_summaries(rows)
    return dict(
        version="patha-s8-score-v1",
        run_id=str(run_id),
        repeats=repeats,
        contract_sha256=contract["contract_sha256"],
        parent_record_denominator=parent_denominator,
        record_denominator=len(universe),
        rows=rows,
        p1=[
            dict(
                arm=arm,
                replicate_id=rep,
                kappa_pass=r["covered"] == r["denominator"],
                **r,
            )
            for (arm, rep), r in p1.items()
        ],
        profiles=summaries,
        independent_reviews_required=True,
        official_code_sha256=official_code_sha256,
        judge_configuration_sha256=judge_configuration_sha256,
    )


def profile_summaries(rows):
    summaries = {}
    for profile in PROFILES:
        selected = [r for r in rows if r["profile"] == profile]
        summaries[profile] = dict(
            all_cases=metrics(selected),
            conditional_p2=metrics([r for r in selected if r["p1_correct"]]),
            by_task={
                kind: metrics([r for r in selected if r["task_type"] == kind])
                for kind in ("Cas", "Abs")
            },
        )
    return summaries


def episode_view(report, episode_ids):
    """Aggregate the same scored rows; never rerun or reinterpret judgments."""
    selected = set(episode_ids)
    if (
        not selected
        or len(selected) != len(episode_ids)
        or not selected <= {r["episode_id"] for r in report["rows"]}
    ):
        raise ContractError("score_episode_view_invalid")
    rows = [r for r in report["rows"] if r["episode_id"] in selected]
    p1 = []
    for result in report["p1"]:
        subset = [r for r in result["rows"] if r["episode_id"] in selected]
        covered = sum(r["covered"] for r in subset)
        p1.append(
            dict(
                result,
                rows=subset,
                covered=covered,
                denominator=len(subset),
                kappa_pass=covered == len(subset),
            )
        )
    return dict(
        report,
        rows=rows,
        p1=p1,
        profiles=profile_summaries(rows),
        record_denominator=len({r["record_id"] for r in rows}),
    )
