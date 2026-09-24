"""Actual public service/S3–S7 and restored DBs, with offline HTTP only."""

# ruff: noqa: F811
import json
from pathlib import Path
from uuid import uuid4
import httpx
import pytest

from test_integration_knowledge_phases import phased as phased
from test_integration_s8_prerequisite_revision import make_runner, snapshot_args
from integrations.memebench.patha.database_snapshot import capture
from integrations.memebench.patha.rq2_coordinator import RQ2Coordinator
from s8_helpers import materials
from examples.knowledge_fixture import secrets


@pytest.mark.asyncio
async def test_public_rq1_rq2_actual_chronology(phased, tmp_path, monkeypatch):
    factory, models, repo, ctx = phased
    prototype = await factory()
    allowed, boundary = materials(tmp_path / "questions")
    main_id = uuid4()

    async def seal(handoff, config_hash):
        return await capture(
            **snapshot_args(),
            directory=tmp_path / "main",
            run_id=str(main_id),
            handoff=handoff,
            configuration_sha256=config_hash,
        )

    runner = make_runner(prototype, repo, models)
    main = await runner.run(
        allowed,
        account_id=ctx.account_id,
        agent_id=ctx.agent_id,
        run_id=main_id,
        replicate_id=0,
        cutoff_ordinal=boundary.before.ordinal,
        seal_selective=seal,
    )
    baseline = len(models.calls)
    old_send = models.send
    calls = []

    def send(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "answer":
            return httpx.Response(
                200,
                json=dict(
                    model="answer",
                    choices=[dict(message=dict(content="Synthetic answer"))],
                    usage=dict(prompt_tokens=4, completion_tokens=2, total_tokens=6),
                ),
            )
        return old_send(request)

    transport = httpx.MockTransport(send)
    answer_cfg = prototype.config.base.model_copy(update={"model": "answer"})
    coordinator = RQ2Coordinator(
        knowledge=prototype.config,
        answer_config=answer_cfg,
        secrets=secrets(),
        answer_key="synthetic",
        context_service=prototype.context_service,
        transport=transport,
    )
    # Actual question file opens must follow a persisted matching state seal.
    original = Path.read_text
    releases = []

    def read(path, *args, **kwargs):
        if path.parent == tmp_path / "questions":
            phase = "before" if path.name == boundary.before.filename else "after"
            profile = len(releases) // 2
            assert (
                tmp_path / "rq2" / f"profile-{profile}" / f"{phase}-state.json"
            ).is_file()
            releases.append(phase)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    results = await coordinator.run(
        allowed,
        boundary,
        main=main,
        expected_main_run_id=main_id,
        snapshot_directory=tmp_path / "main",
        database=snapshot_args(),
        question_directory=tmp_path / "questions",
        output=tmp_path / "rq2",
        run_id=uuid4(),
        replicate_id=0,
    )
    assert releases == ["before", "after"] * 3
    assert len({r["initial_state_sha256"] for r in results}) == 1
    for result in results:
        assert (
            result["maintenance_rounds"] == 1 and result["core_event_executions"] == 1
        )
        assert len(result["ingestions"]) == 2
        assert all(i["result"]["status"] == "complete" for i in result["ingestions"])
        assert result["timeline"] == [
            "before_sealed",
            "before_questions_complete",
            "all_changes_ingested",
            "maintenance_round_complete",
            "after_sealed",
            "after_questions_complete",
        ]
        assert (
            len(
                {
                    w["target_id"]
                    for m in result["maintenance"]
                    for w in m["works"]
                    if w["status"] == "updated"
                }
            )
            == 2
        )
    assert sum(c["model"] == "answer" for c in calls) == 12
    assert sum(c["model"] == "extract" for c in models.calls[baseline:]) == 0
    cursor = 0
    for result in results:
        assert [c["model"] for c in calls[cursor : cursor + 2]] == ["answer"] * 2
        cursor += 2
        after = next(
            i for i in range(cursor, len(calls)) if calls[i]["model"] == "answer"
        )
        model_sequence = [c["model"] for c in calls[cursor:after]]
        assert model_sequence[:2] == ["locator"] * 2 and "verifier" in model_sequence
        assert "locator" not in model_sequence[2:]
        cursor = after + 2
    assert cursor == len(calls)
    (tmp_path / "calls.json").write_text(json.dumps(calls, indent=2))

    # Perturb privileged sidecar AND the answer questions. Run the actual core
    # again from the same main. Maintenance must be unaffected, including SQL.
    from integrations.memebench.patha.schemas import canonical_sha256
    from integrations.memebench.patha.artifacts import file_hash
    from contexthub.models.knowledge import ContractError
    import re

    marker = "PRIVILEGED_CHANGED_GOLD_TASK_HOP_PATH"
    sidecar = tmp_path / "labels.json"
    sidecar.write_text(
        json.dumps(
            dict(
                gold=marker,
                task_type=marker,
                hop=999,
                root_change=marker,
                path=[marker],
            )
        )
    )
    changed = boundary.model_dump(mode="json")
    for phase in ("before", "after"):
        ref = changed[phase]
        path = tmp_path / "questions" / ref["filename"]
        bundle = json.loads(original(path))
        for question in bundle["questions"]:
            question["text"] = marker
        bundle.pop("bundle_sha256")
        bundle["bundle_sha256"] = canonical_sha256(bundle)
        path.write_text(json.dumps(bundle))
        ref["bundle_sha256"] = bundle["bundle_sha256"]
        ref["file_sha256"] = file_hash(path)
    changed_boundary = type(boundary).model_validate(changed)
    first_calls = list(calls)
    calls.clear()

    def later_read(path, *args, **kwargs):
        assert path != sidecar, "privileged labels read during maintenance"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", later_read)
    again = RQ2Coordinator(
        knowledge=prototype.config,
        answer_config=answer_cfg,
        secrets=secrets(),
        answer_key="synthetic",
        context_service=prototype.context_service,
        transport=transport,
    )
    second = await again.run(
        allowed,
        changed_boundary,
        main=main,
        expected_main_run_id=main_id,
        snapshot_directory=tmp_path / "main",
        database=snapshot_args(),
        question_directory=tmp_path / "questions",
        output=tmp_path / "perturbed",
        run_id=uuid4(),
        replicate_id=0,
    )
    assert all(r["status"] == "complete" for r in second)

    def normalize(value):
        # Preserve all text, versions, statuses, stale flags and graph structure.
        # UUIDs and identity hashes are alpha-renamed by first occurrence, with
        # repeated references retained. Only named wall-clock fields are removed.
        ids, hashes = {}, {}

        def visit(v):
            if isinstance(v, dict):
                return {
                    k: visit(x)
                    for k, x in sorted(v.items())
                    if k
                    not in {
                        "created_at",
                        "updated_at",
                        "started_at",
                        "finished_at",
                        "timestamp",
                        "cutoff_at",
                        "ingested_at",
                    }
                }
            if isinstance(v, list):
                # Cause collections are sets in the core, sorted there by
                # random event UUID. Canonicalize only those collections by
                # their stable restored source identity/version.
                if v and all(
                    isinstance(x, dict)
                    and {"source", "event_id"} <= set(x.get("cause", x))
                    for x in v
                ):
                    v = sorted(
                        v,
                        key=lambda x: json.dumps(
                            x.get("cause", x)["source"], sort_keys=True
                        ),
                    )
                return [visit(x) for x in v]
            if isinstance(v, str):
                # Parse nested JSON so only explicitly named clock fields are
                # removed; arbitrary dates/content inside source text remain.
                for delimiter in ("{", "["):
                    start = v.find(delimiter)
                    if start >= 0:
                        try:
                            nested = json.loads(v[start:])
                        except ValueError:
                            continue
                        return v[:start] + json.dumps(visit(nested), sort_keys=True)
                v = re.sub(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    lambda m: ids.setdefault(m[0], f"uuid:{len(ids)}"),
                    v,
                )
                v = re.sub(
                    r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])",
                    lambda m: hashes.setdefault(m[0], f"hash:{len(hashes)}"),
                    v,
                )
                return v
            return v

        return visit(value)

    maintenance_a = [c for c in first_calls if c["model"] != "answer"]
    maintenance_b = [c for c in calls if c["model"] != "answer"]
    (tmp_path / "perturbed-calls.json").write_text(json.dumps(calls, indent=2))
    (tmp_path / "normalized-a.json").write_text(
        json.dumps(normalize(maintenance_a), indent=2)
    )
    (tmp_path / "normalized-b.json").write_text(
        json.dumps(normalize(maintenance_b), indent=2)
    )
    assert marker not in json.dumps(maintenance_b)
    assert normalize(maintenance_a) == normalize(maintenance_b)
    for i in range(3):
        a = json.loads(original(tmp_path / "rq2" / f"profile-{i}" / "after-state.json"))
        b = json.loads(
            original(tmp_path / "perturbed" / f"profile-{i}" / "after-state.json")
        )

        # Sort by stable context identity (identical restored graph and source order).
        def business(state):
            return sorted(
                [
                    dict(
                        content=[c["l0_content"], c["l1_content"], c["l2_content"]],
                        version=c["version"],
                        validity_status=c["validity_status"],
                        stale=c["stale_at"] is not None,
                        deleted=c["deleted_at"] is not None,
                    )
                    for c in state["state"]["contexts"]
                ],
                key=lambda c: (str(c["content"]), c["version"]),
            )

        assert business(a) == business(b)
    (tmp_path / "perturbed-calls.json").write_text(json.dumps(calls, indent=2))
    (tmp_path / "normalized-maintenance.json").write_text(
        json.dumps(normalize(maintenance_a), indent=2)
    )
    with pytest.raises(ContractError, match="reuse"):
        await again.run(
            allowed,
            changed_boundary,
            main=main,
            expected_main_run_id=main_id,
            snapshot_directory=tmp_path / "main",
            database=snapshot_args(),
            question_directory=tmp_path / "questions",
            output=tmp_path / "must-not-create",
            run_id=uuid4(),
            replicate_id=0,
        )

    # Produce a complete OFFLINE report from these actual service exports.
    # The following human-authored synthetic reviews are independent fixtures:
    # the synthetic model's specified terminal output is the literal ' revised'.
    # No verifier result is consulted to create any score or review.
    from uuid import UUID
    from integrations.memebench.patha.schemas import ScoringSidecar
    from integrations.memebench.patha.scoring_contract import build_scoring_contract
    from integrations.memebench.patha.rq1_scorer import PathReview, StepReview
    from integrations.memebench.patha.scorer import CaseReference, P2Review
    from integrations.memebench.patha.reporting import (
        ScorePlan,
        RQ1Export,
        RQ2Export,
        dependency_identity,
        generate_report,
    )
    from integrations.memebench.patha.run_gates import FileIdentity
    from integrations.memebench.patha.judge_runner import JudgePlan, execute_judge
    from contexthub.models.knowledge import (
        canonical_hash,
        text_hash,
        EvidenceRef,
        SourceSnapshot,
        ContextVersion,
    )
    from test_memebench_patha_official_contract import source as official_source
    from test_memebench_patha_bootstrap import protocol
    from s8_helpers import scoring_label

    def put(name, value, lines=False):
        path = tmp_path / name
        path.write_text(
            ("\n".join(json.dumps(r) for r in value) + "\n")
            if lines
            else json.dumps(value)
        )
        return FileIdentity(path=str(path), sha256=file_hash(path))

    def ref(path):
        return FileIdentity(path=str(path), sha256=file_hash(path))

    label = scoring_label().model_dump(mode="json", exclude_none=True)
    label.update(
        episode_id=allowed.episode_id,
        before_question_id="q_" + f"{1:024x}",
        after_question_id="q_" + f"{3:024x}",
        before_entity_values={"target": "C: uses B in report"},
        after_entity_values={"target": "C: uses B in report revised"},
    )
    label.pop("record_sha256")
    label["record_sha256"] = canonical_sha256(label)
    label = ScoringSidecar.model_validate(label)
    g = main.graph_evidence[0]
    nodes = {m.text.split(":")[0]: m.node for m in g.mappings}
    edges = [
        next(
            e
            for e in g.graph.edges
            if e.upstream == nodes[a] and e.downstream == nodes[b]
        )
        for a, b in (("A", "B"), ("B", "C"))
    ]
    steps = []
    for edge in edges:
        ev = edge.evidence[0]
        source_id = next(
            source_id for source_id, seal in g.source_map if seal == ev.source
        )
        original_message = json.loads(g.source_texts[ev.source.sha256])[0]
        steps.append(
            dict(
                source_entity="synthetic upstream",
                target_entity="synthetic downstream",
                dialogue_evidence=[
                    dict(
                        source_id=source_id,
                        turn_index=0,
                        role=original_message["role"],
                        excerpt=original_message["content"],
                        excerpt_sha256=text_hash(original_message["content"]),
                    )
                ],
            )
        )
    path = dict(
        record_id=label.record_id,
        episode_id=allowed.episode_id,
        necessary_path_candidates=[
            dict(
                path_id="synthetic-reviewed-A-B-C",
                entities=["A", "B", "C"],
                direction="event_root_to_target",
                p2_traversable_if_all_edges_delivered=True,
                edges=steps,
            )
        ],
    )
    path["record_sha256"] = canonical_sha256(path)
    path_reviews = []
    for arm, g in zip(("selective", "full-history"), main.graph_evidence):
        ns = {m.text.split(":")[0]: m.node for m in g.mappings}
        edges = [
            next(
                e
                for e in g.graph.edges
                if e.upstream == ns[a] and e.downstream == ns[b]
            )
            for a, b in (("A", "B"), ("B", "C"))
        ]
        review = PathReview(
            record_id=label.record_id,
            path_id="synthetic-reviewed-A-B-C",
            reference_record_sha256=path["record_sha256"],
            graph=g.reference,
            reviewer_id="synthetic-fixture-author",
            review_method="offline_human_semantic_review",
            review_version="1",
            nodes=tuple(ns[x] for x in ("A", "B", "C")),
            steps=tuple(
                StepReview(
                    upstream=e.upstream,
                    downstream=e.downstream,
                    edge_sha256=canonical_hash(e),
                    reference_step_sha256=canonical_sha256(step),
                    evidence=e.evidence,
                    verdict="PASS",
                    rationale="Synthetic A-B-C dependency specified independently in fixture",
                )
                for e, step in zip(edges, steps)
            ),
        )
        path_reviews.append(
            dict(arm=arm, replicate_id=0, review=review.model_dump(mode="json"))
        )
    p2_reviews = []
    exports = []
    rq2_id = UUID(second[0]["run_id"])
    for i, result in enumerate(second):
        directory = tmp_path / "perturbed" / f"profile-{i}"
        state = json.loads(original(directory / "after-state.json"))
        target = next(
            c
            for c in state["state"]["contexts"]
            if c["id"] == str(nodes["C"].context_id)
        )
        assert (
            target["l2_content"] == "C: uses B in report revised"
            and target["version"] == 2
        )
        versions = {
            (v["context_id"], v["version"]): v
            for v in state["state"]["context_versions"]
        }
        sources = [
            SourceSnapshot.model_validate(a["payload"])
            for a in state["state"]["artifacts"]
            if a["kind"] == "source"
        ]
        snapshot = next(
            s
            for s in sources
            if "A: allowance 400"
            in versions[(str(s.source.context_id), s.source.version)][s.content_level]
        )
        text = versions[(str(snapshot.source.context_id), snapshot.source.version)][
            snapshot.content_level
        ]
        evidence = EvidenceRef(
            source=snapshot.reference(),
            start=0,
            end=len(text),
            excerpt=text,
            excerpt_sha256=text_hash(text),
            provenance=("synthetic independent review",),
        )
        p2_reviews.append(
            P2Review(
                record_id=label.record_id,
                label_sha256=label.record_sha256,
                run_id=rq2_id,
                replicate_id=0,
                profile=result["profile"],
                graph_sha256=main.graph_evidence[0].reference.sha256,
                state_sha256=state["sha256"],
                before_target=nodes["C"],
                final_target=ContextVersion(
                    context_id=nodes["C"].context_id, version=2
                ),
                verdict="CORRECT",
                reviewer_id="synthetic-independent-reviewer",
                review_method="offline_human_semantic_review",
                review_version="1",
                rationale="Literal fixture terminal content checked independently of verifier",
                evidence=(evidence,),
            ).model_dump(mode="json")
        )
        exports.append(
            RQ2Export(
                episode_id=allowed.episode_id,
                replicate_id=0,
                profile=result["profile"],
                metadata=ref(directory / "episode.json"),
                before_state=ref(directory / "before-state.json"),
                after_state=ref(directory / "after-state.json"),
                before_answers=ref(directory / "before-answers.json"),
                after_answers=ref(directory / "after-answers.json"),
                maintenance_attempts=ref(directory / "maintenance-attempts.json"),
                answer_attempts=ref(directory / "answer-attempts.json"),
                failure_state=None,
                failure_answer_state=None,
            )
        )
    labels_ref = put(
        "score-labels.jsonl", [label.model_dump(mode="json", exclude_none=True)], True
    )
    paths_ref = put("score-paths.jsonl", [path], True)
    contract = build_scoring_contract(
        [label.model_dump(mode="json")],
        [path],
        artifact_hashes={},
        contract_validator_code={},
    )
    contract_ref = put("score-contract.json", contract)
    boundary_ref = put(
        "score-boundaries.json",
        {allowed.episode_id: changed_boundary.model_dump(mode="json")},
    )
    fixture_file = tmp_path / "official-fixture.py"
    fixture_file.write_text(
        'import httpx\ndef build_transport():\n    return httpx.MockTransport(lambda req: httpx.Response(200, json={"model":"synthetic-judge","choices":[{"message":{"content":\'{"correct":true,"reason":"synthetic contract"}\'}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}))\n'
    )
    monkeypatch.setenv("S8_SYNTHETIC_JUDGE_KEY", "synthetic-not-a-secret")
    judge_cfg = prototype.config.base.model_copy(
        update={"model": "synthetic-judge", "prompt_template": "{input_json}"}
    )
    judge_plan = JudgePlan(
        mode="synthetic",
        run_id=rq2_id,
        manifest=None,
        authorization=None,
        config=judge_cfg,
        official_source=official_source(),
        labels=labels_ref,
        boundaries=boundary_ref,
        question_directory=str(tmp_path / "questions"),
        rq2=tuple(exports),
        database=snapshot_args(),
        account_id="s8-judge-" + uuid4().hex,
        credential_environment="S8_SYNTHETIC_JUDGE_KEY",
        synthetic_responses=ref(fixture_file),
    )
    judged = await execute_judge(judge_plan, tmp_path / "judged")
    assert judged["profiles"] == 3
    dependency_identity(contract_ref, output=tmp_path / "scorer-identity")
    references = [
        CaseReference(
            record_id=label.record_id,
            label_sha256=label.record_sha256,
            recoverable_cas=True,
            reviewer_id="synthetic-reference-author",
            review_method="offline_human_semantic_review",
            review_version="1",
            rationale="Fixture specifies a recoverable Cas; not inferred from the result",
            reference_material_sha256=path["record_sha256"],
        ).model_dump(mode="json")
    ]
    plan = ScorePlan(
        mode="synthetic",
        run_id=rq2_id,
        rq1_run_id=main_id,
        protocol=protocol(),
        episode_ids=(allowed.episode_id,),
        s1_contract=contract_ref,
        maintenance_inputs=put(
            "score-inputs.jsonl", [allowed.model_dump(mode="json")], True
        ),
        labels=labels_ref,
        paths=paths_ref,
        scorer_identity=ref(tmp_path / "scorer-identity/identity.json"),
        run_manifest=None,
        official_source=official_source(),
        judge_configuration_sha256=canonical_hash(judge_cfg),
        rq1=(
            RQ1Export(
                episode_id=allowed.episode_id,
                replicate_id=0,
                result=put("main-result.json", main.model_dump(mode="json")),
                failure_state=None,
            ),
        ),
        rq2=tuple(exports),
        path_reviews=put("path-reviews.json", path_reviews),
        case_references=put("case-references.json", references),
        p2_reviews=put("p2-reviews.json", p2_reviews),
        judgments=tuple(
            ref(tmp_path / "judged" / f"judgments-{i}.json") for i in range(3)
        ),
        judge_attempts=tuple(
            ref(tmp_path / "judged" / f"attempts-{i}.json") for i in range(3)
        ),
    )
    result = generate_report(plan, tmp_path / "report")
    report = json.loads(original(tmp_path / "report/scores.json"))
    assert result["record_denominator"] == 1
    assert all(row["kappa_pass"] for row in report["p1"])
    assert all(
        r["p2_correct"] and r["official_class"] == "real" for r in report["rows"]
    )
    costs = json.loads(original(tmp_path / "report/costs.json"))
    assert len([c for c in costs["logical"] if c["stage"] == "P2"]) == 3
    assert costs["judge"]["summary"]["model_attempts"] == 6
    assert all(
        "locator" in {a["role"] for a in c["cost"]["attempts"]}
        or "change_location" in {a["role"] for a in c["cost"]["attempts"]}
        for c in costs["logical"]
        if c["stage"] == "P2"
    )
    assert (tmp_path / "report/report.md").is_file()


@pytest.mark.asyncio
async def test_coordinator_preserves_actual_pending_raw_change(phased, tmp_path):
    factory, models, repo, ctx = phased
    service = await factory()
    allowed, boundary = materials(tmp_path / "questions")
    run = uuid4()

    async def seal(handoff, config_hash):
        return await capture(
            **snapshot_args(),
            directory=tmp_path / "main",
            run_id=str(run),
            handoff=handoff,
            configuration_sha256=config_hash,
        )

    main = await make_runner(service, repo, models).run(
        allowed,
        account_id=ctx.account_id,
        agent_id=ctx.agent_id,
        run_id=run,
        replicate_id=0,
        cutoff_ordinal=1,
        seal_selective=seal,
    )
    models.invalid_target = str(uuid4())

    def send(req):
        if json.loads(req.content)["model"] == "answer":
            return httpx.Response(503, json={})
        return models.send(req)

    coordinator = RQ2Coordinator(
        knowledge=service.config,
        answer_config=service.config.base.model_copy(update={"model": "answer"}),
        secrets=secrets(),
        answer_key="synthetic",
        context_service=service.context_service,
        transport=httpx.MockTransport(send),
    )
    results = await coordinator.run(
        allowed,
        boundary,
        main=main,
        expected_main_run_id=run,
        snapshot_directory=tmp_path / "main",
        database=snapshot_args(),
        question_directory=tmp_path / "questions",
        output=tmp_path / "pending",
        run_id=uuid4(),
        replicate_id=0,
    )
    for i, result in enumerate(results):
        assert result["status"] == "pending" and result["core_event_executions"] == 0
        assert result["ingestions"][0]["result"]["status"] == "pending"
        assert not result["ingestions"][0]["result"]["event_ids"]
        after = json.loads(
            (tmp_path / "pending" / f"profile-{i}" / "after-state.json").read_text()
        )
        assert any(
            c["l2_content"]
            == json.dumps(
                [m.model_dump(mode="json") for m in allowed.sources[2].messages],
                ensure_ascii=False,
            )
            for c in after["state"]["contexts"]
        )
        answers = json.loads(
            (tmp_path / "pending" / f"profile-{i}" / "after-answers.json").read_text()
        )
        assert len(answers) == 2 and all(
            a["error"] and a["answer"] == "" for a in answers
        )
        attempts = json.loads(
            (tmp_path / "pending" / f"profile-{i}" / "answer-attempts.json").read_text()
        )
        assert any(
            r["outcome"] == "error" and r["cost"]["amount"] is None for r in attempts
        )


@pytest.mark.asyncio
async def test_failed_control_does_not_erase_selective_main(
    phased, tmp_path, monkeypatch
):
    factory, models, repo, ctx = phased
    service = await factory()
    allowed, boundary = materials(tmp_path / "questions")
    run = uuid4()
    control = False
    original = models.send
    # Provider errors may legitimately produce a pending-edge graph receipt.
    # Also inject an actual receipt-read exception after finalization, to test
    # the runner's outer control-failure path without replacing core execution.
    from contexthub.services.knowledge_service import KnowledgeService

    original_get = KnowledgeService.get_graph_handoff

    async def receipt(self, *args, **kwargs):
        if control:
            raise RuntimeError("synthetic control receipt read unavailable")
        return await original_get(self, *args, **kwargs)

    monkeypatch.setattr(KnowledgeService, "get_graph_handoff", receipt)

    def send(req):
        return httpx.Response(503, json={}) if control else original(req)

    models.transport = httpx.MockTransport(send)

    async def seal(handoff, config_hash):
        nonlocal control
        value = await capture(
            **snapshot_args(),
            directory=tmp_path / "main",
            run_id=str(run),
            handoff=handoff,
            configuration_sha256=config_hash,
        )
        control = True
        return value

    result = await make_runner(service, repo, models).run(
        allowed,
        account_id=ctx.account_id,
        agent_id=ctx.agent_id,
        run_id=run,
        replicate_id=0,
        cutoff_ordinal=1,
        seal_selective=seal,
    )
    assert result.selective_snapshot is not None and result.handoffs[0] is not None
    assert result.handoffs[1] is None and result.arm_errors[1] is not None
    assert result.graph_evidence[0] is not None and result.graph_evidence[1] is None
    from integrations.memebench.patha.rq2_coordinator import validate_main
    from integrations.memebench.patha.ledger import rq1_costs

    assert (
        validate_main(
            result, expected_run_id=run, runtime=allowed, configuration=service.config
        )
        == result.selective_snapshot
    )
    costs = rq1_costs(result)
    assert costs["full-history"]["summary"]["unknown_model_cost_attempts"] > 0
