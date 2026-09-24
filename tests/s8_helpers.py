"""Synthetic S8 materials only; no MEME episode content or formal defaults."""

from integrations.memebench.patha.schemas import (
    MaintenanceInput,
    AnswerBundle,
    canonical_sha256,
)
from integrations.memebench.patha.chronology import (
    EpisodeBoundary,
    export_question_packages,
    prefix_hash,
)
from test_memebench_patha_rq1_runner import runtime


def materials(directory):
    value = runtime().model_dump(mode="json")
    template = value["sources"][0]
    value["sources"] = []
    for index, text in enumerate(
        (
            "A: allowance 500|X: unrelated fact",
            "B: uses A for reimbursement|C: uses B in report",
            "Now |A: allowance 400|Please delete an old shopping reminder.",
            "Filler message preserved intact.",
        )
    ):
        row = dict(
            template,
            ordinal=index,
            source_id="src_" + f"{index + 1:024x}",
            messages=[dict(role="user", content=text)],
        )
        row["content_sha256"] = canonical_sha256(row["messages"])
        value["sources"].append(row)
    value["input_sha256"] = canonical_sha256(
        {k: v for k, v in value.items() if k != "input_sha256"}
    )
    allowed = MaintenanceInput.model_validate(value)
    bundles = []
    for i, (phase, ordinal) in enumerate((("before", 1), ("after", 3))):
        data = dict(
            schema_version="patha-s1-v1",
            episode_id=allowed.episode_id,
            bundle_id="ab_" + f"{i + 1:024x}",
            phase=phase,
            release_after_ordinal=ordinal,
            release_after_source_id=allowed.sources[ordinal].source_id,
            source_prefix_sha256=prefix_hash(allowed, ordinal),
            sealed_state_required=True,
            questions=[
                dict(
                    question_id="q_" + f"{2 * i + j + 1:024x}",
                    text=f"Synthetic question {j}",
                )
                for j in range(2)
            ],
        )
        data["bundle_sha256"] = canonical_sha256(data)
        bundles.append(AnswerBundle.model_validate(data).model_dump(mode="json"))
    refs = export_question_packages(bundles, directory)
    return allowed, EpisodeBoundary(
        episode_id=allowed.episode_id,
        input_sha256=allowed.input_sha256,
        before=refs[(allowed.episode_id, "before")],
        after=refs[(allowed.episode_id, "after")],
    )


def scoring_label(*, index=1, kind="Cas", raw="synthetic"):
    from integrations.memebench.patha.schemas import ScoringSidecar

    data = dict(
        schema_version="patha-s1-v1",
        record_id="rec_" + f"{index:024x}",
        episode_id="ep_" + f"{index:024x}",
        raw_episode_id=raw,
        domain="personal_life",
        task_type=kind,
        hop=2,
        target_entities=["target"],
        event_root="root",
        event_before="old",
        event_after="new",
        before_question_id="q_" + f"{index * 2:024x}",
        after_question_id="q_" + f"{index * 2 + 1:024x}",
        before_entity_values={"target": "old value"},
        after_entity_values={"target": "new value" if kind == "Cas" else "old value"},
        before_expected_answer="old value",
        after_gold_answer="new value"
        if kind == "Cas"
        else "MUST NOT SUBSTITUTE THIS GOLD",
        answer_pair_status="scoreable",
        path_status="scoreable",
        issues=[],
    )
    data["record_sha256"] = canonical_sha256(data)
    return ScoringSidecar.model_validate(data)


async def score_materials():
    from uuid import uuid4
    from test_memebench_patha_rq1_scorer import fixture
    from integrations.memebench.patha.scoring_contract import build_scoring_contract
    from integrations.memebench.patha.scorer import CaseReference, P2Review
    from integrations.memebench.patha.official_adapter import OfficialJudgment
    from integrations.memebench.patha.rq2_coordinator import PROFILES
    from contexthub.models.knowledge import canonical_hash, ContextVersion

    labels, paths, graphs, reviews, references, p2, states, answers, judgments = (
        [],
        [],
        {},
        [],
        [],
        [],
        {},
        {},
        [],
    )
    run = uuid4()
    for index, kind in enumerate(("Cas", "Abs"), 1):
        _, path, graph, review = await fixture()
        label = scoring_label(index=index, kind=kind)
        path = dict(path, record_id=label.record_id, episode_id=label.episode_id)
        path.pop("record_sha256")
        path["record_sha256"] = canonical_sha256(path)
        review = review.model_copy(
            update=dict(
                record_id=label.record_id, reference_record_sha256=path["record_sha256"]
            )
        )
        labels.append(label)
        paths.append(path)
        graphs[label.episode_id] = graph
        reviews.append(review)
        references.append(
            CaseReference(
                record_id=label.record_id,
                label_sha256=label.record_sha256,
                recoverable_cas=True if kind == "Cas" else None,
                reviewer_id="synthetic-author",
                review_method="offline_human_semantic_review",
                review_version="fixture",
                rationale="synthetic reference",
                reference_material_sha256=path["record_sha256"],
            )
        )
        target = review.nodes[-1]
        source = graph.source_snapshots[0]
        for profile in PROFILES:
            for phase in ("before", "after"):
                state = dict(
                    contexts=[
                        dict(
                            id=str(target.context_id),
                            version=2 if phase == "after" else 1,
                            validity_status="stale"
                            if kind == "Abs" and phase == "after"
                            else "fresh",
                            deleted_at=None,
                            status="active",
                            stale_at=None,
                        )
                    ],
                    invalidations=[],
                    artifacts=[
                        dict(
                            id=str(source.id),
                            sha256=source.reference().sha256,
                            payload=source.model_dump(mode="json"),
                        )
                    ],
                    context_versions=[
                        dict(
                            context_id=str(source.source.context_id),
                            version=source.source.version,
                            **{
                                source.content_level: graph.source_texts[
                                    source.reference().sha256
                                ]
                            },
                        )
                    ],
                )
                seal = dict(
                    phase=phase,
                    episode_id=label.episode_id,
                    profile=profile,
                    replicate_id=0,
                    run_id=str(run),
                    graph=graph.reference.model_dump(mode="json"),
                    state=state,
                )
                seal["sha256"] = canonical_sha256(seal)
                key = (profile, 0, label.episode_id, phase)
                states[key] = seal
                answer = dict(
                    question_id=getattr(label, phase + "_question_id"),
                    answer="synthetic answer",
                    error=None,
                    state_sha256=seal["sha256"],
                    call_id=str(uuid4()),
                )
                answers[key] = [answer]
                judgments.append(
                    OfficialJudgment(
                        record_id=label.record_id,
                        label_sha256=label.record_sha256,
                        run_id=str(run),
                        replicate_id=0,
                        profile=profile,
                        phase=phase,
                        question_id=answer["question_id"],
                        answer_sha256=canonical_hash(answer),
                        state_sha256=seal["sha256"],
                        official_code_sha256="a" * 64,
                        prompt_sha256="b" * 64,
                        configuration_sha256="c" * 64,
                        call_id=str(uuid4()),
                        correct=True,
                        reason="synthetic official contract",
                        error=None,
                    )
                )
                if phase == "after":
                    p2.append(
                        P2Review(
                            record_id=label.record_id,
                            label_sha256=label.record_sha256,
                            run_id=run,
                            replicate_id=0,
                            profile=profile,
                            graph_sha256=graph.reference.sha256,
                            state_sha256=seal["sha256"],
                            before_target=target,
                            final_target=ContextVersion(
                                context_id=target.context_id, version=2
                            ),
                            verdict="CORRECT",
                            reviewer_id="independent-synthetic-author",
                            review_method="offline_human_semantic_review",
                            review_version="fixture",
                            rationale="synthetic independent review",
                            evidence=review.steps[-1].evidence,
                        )
                    )
    contract = build_scoring_contract(
        [r.model_dump(mode="json") for r in labels],
        paths,
        artifact_hashes={},
        contract_validator_code={},
    )
    return dict(
        contract=contract,
        labels=labels,
        paths=paths,
        p1_graphs={(arm, 0): graphs for arm in ("selective", "full-history")},
        p1_reviews={(arm, 0): tuple(reviews) for arm in ("selective", "full-history")},
        references=references,
        p2_reviews=p2,
        states=states,
        answers=answers,
        judgments=judgments,
        run_id=run,
        repeats=1,
        official_code_sha256="a" * 64,
        judge_configuration_sha256="c" * 64,
    )
