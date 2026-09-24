"""R3 offline score views: fixed parent bytes, complete episodes, no executions.

Fixture manifests/seed values authorize no real/development run. Frozen data is
used only for membership/denominator contracts, never model or prompt selection.
"""

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from contexthub.models.knowledge import ContractError, canonical_hash
from integrations.memebench.patha.artifacts import file_hash
from integrations.memebench.patha.reporting import (
    ScorePlan,
    generate_report,
)
from integrations.memebench.patha.run_gates import FileIdentity
from integrations.memebench.patha.scorer import score_report, episode_view, metrics
from integrations.memebench.patha.rq1_scorer import score_rq1
from integrations.memebench.patha.scoring_contract import ScoringContractError
from test_memebench_patha_run_gates import manifest_fixture, S1
from s8_helpers import score_materials


def ref(path):
    return FileIdentity(path=str(path), sha256=file_hash(path))


def frozen_plan(tmp_path, *, formal=False, dev_count=1):
    manifest, _ = manifest_fixture(tmp_path)
    lock = json.loads((S1 / "manifests/forward-lock-v1.json").read_text())
    development = tuple(r["episode_id"] for r in lock["development_episodes"])
    locked = tuple(r["episode_id"] for r in lock["forward_locked_episodes"])
    episodes = development + locked if formal else development[:dev_count]
    official = ref(
        Path(
            "/Users/sherrylin/Documents/PythonProjects/public/MEME-public/code/eval/judge.py"
        )
    )
    manifest = manifest.model_copy(
        update=dict(
            mode="formal" if formal else "development",
            episode_ids=episodes,
            development=manifest.development.model_copy(
                update={"episode_ids": development}
            ),
            frozen_files=(*manifest.frozen_files, official),
        )
    )
    plan = ScorePlan(
        mode=manifest.mode,
        run_id=manifest.run_id,
        rq1_run_id=uuid4(),
        protocol=manifest.statistics,
        episode_ids=episodes,
        s1_contract=manifest.s1_contract,
        maintenance_inputs=ref(S1 / "inputs/maintenance-inputs-v1.jsonl"),
        labels=ref(S1 / "labels/scoreable-records-v2.jsonl"),
        paths=ref(S1 / "labels/necessary-paths-v2.jsonl"),
        scorer_identity=manifest.scorer_identity,
        run_manifest=manifest,
        official_source=official,
        judge_configuration_sha256="c" * 64,
        rq1=(),
        rq2=(),
        path_reviews=None,
        case_references=None,
        p2_reviews=None,
        judgments=(),
        judge_attempts=(),
    )
    return plan, lock


def rows(ref):
    return [json.loads(s) for s in Path(ref.path).read_text().split("\n") if s]


def test_development_subset_preserves_parent_and_missing_denominators(
    tmp_path, monkeypatch
):
    plan, lock = frozen_plan(tmp_path, dev_count=2)
    parent_before = Path(plan.s1_contract.path).read_bytes()
    expected = {
        r["record_id"] for r in rows(plan.labels) if r["episode_id"] in plan.episode_ids
    }
    # Real public scoring functions must only receive/evaluate the selected view.
    import integrations.memebench.patha.scorer as scorer
    import integrations.memebench.patha.rq1_scorer as p1

    original = scorer.score_rq1
    seen = []

    def checked(*args, **kwargs):
        assert kwargs["episode_ids"] == plan.episode_ids
        result = original(*args, **kwargs)
        assert {r["record_id"] for r in result["rows"]} == expected
        seen.append(result)
        return result

    monkeypatch.setattr(scorer, "score_rq1", checked)

    def no_semantic_review(*args):
        pytest.fail("missing runs must not invoke semantic path review")

    monkeypatch.setattr(p1, "_path_reason", no_semantic_review)
    result = generate_report(plan, tmp_path / "report")
    output = json.loads((tmp_path / "report/scores.json").read_text())
    assert len(seen) == 2
    assert result["record_denominator"] == len(expected)
    assert {r["record_id"] for r in output["rows"]} == expected
    assert len(output["rows"]) == 3 * len(expected)
    assert output["parent_record_denominator"] == 294
    assert all(
        r["missing_state"] and r["missing_review"] and not r["p2_correct"]
        for r in output["rows"]
    )
    assert all(r["retained_in_denominator"] for r in output["rows"])
    for p in output["profiles"].values():
        assert p["all_cases"]["total"] == len(expected)
        assert p["all_cases"]["false_fresh_main"] is None
        assert p["all_cases"]["official_classes"] == {"never_knew": len(expected)}
        assert p["conditional_p2"]["total"] == 0
    scope = output["scope"]
    assert scope["parent_s1_contract"]["sha256"] == plan.s1_contract.sha256
    assert scope["forward_lock"]["sha256"] == file_hash(
        S1 / "manifests/forward-lock-v1.json"
    )
    assert scope["sha256"] == canonical_hash(
        {k: v for k, v in scope.items() if k != "sha256"}
    )
    assert set(scope["episode_ids"]).isdisjoint(
        r["episode_id"] for r in lock["forward_locked_episodes"]
    )
    assert not (tmp_path / "report/forward-locked").exists()
    assert Path(plan.s1_contract.path).read_bytes() == parent_before
    for f in (plan.labels, plan.paths, plan.maintenance_inputs):
        f.verify()


def test_formal_same_run_fixed_twenty_report_all_missing(tmp_path):
    plan, lock = frozen_plan(tmp_path, formal=True)
    expected_episodes = [r["episode_id"] for r in lock["forward_locked_episodes"]]
    expected = {
        r["record_id"]
        for r in rows(plan.labels)
        if r["episode_id"] in expected_episodes
    }
    result = generate_report(plan, tmp_path / "report")
    root = tmp_path / "report"
    all_scores = json.loads((root / "scores.json").read_text())
    subset = json.loads((root / "forward-locked/scores.json").read_text())
    stats = json.loads((root / "forward-locked/statistics.json").read_text())
    assert result["record_denominator"] == 294
    assert len(all_scores["rows"]) == 294 * 3
    assert len(plan.episode_ids) == 100 and len(expected_episodes) == 20
    assert subset["record_denominator"] == len(expected)
    assert subset["scope"]["episode_ids"] == expected_episodes
    assert subset["scope"]["parent_scores_file"]["sha256"] == file_hash(
        root / "scores.json"
    )
    assert subset["run_id"] == all_scores["run_id"] == str(plan.run_id)
    assert subset["rows"] == [
        r for r in all_scores["rows"] if r["episode_id"] in expected_episodes
    ]
    for full, part in zip(all_scores["p1"], subset["p1"]):
        assert part["rows"] == [
            r for r in full["rows"] if r["episode_id"] in expected_episodes
        ]
        assert part["denominator"] == len(expected) and not part["kappa_pass"]
    for value in subset["profiles"].values():
        assert value["all_cases"]["total"] == len(expected)
        assert value["all_cases"]["unknown_fresh"] == len(expected)
    for name, value in stats["comparisons"].items():
        if isinstance(value, dict):
            assert value["interval"]["episode_order"] == expected_episodes
            assert value["interval"]["resamples"] == 10000
    assert stats["comparisons"]["P1_C_billed"]["passed"] is None
    assert stats["comparisons"]["recovery"]["passed"] is None
    assert "forward-locked/report.md" in (root / "report.md").read_text()
    for name, digest in result["files"].items():
        assert file_hash(root / name) == digest
    pl030 = {
        r["record_id"] for r in rows(plan.labels) if r["raw_episode_id"] == "pl_030"
    }
    assert len(pl030) == 4
    assert pl030 <= {r["record_id"] for r in all_scores["rows"]}


@pytest.mark.parametrize(
    "fault",
    [
        "locked",
        "not_predeclared",
        "manifest_disagrees",
        "duplicate_manifest",
        "formal_missing",
        "forged_lock",
        "trimmed_labels",
    ],
)
def test_scoring_scope_cannot_bypass_frozen_membership(tmp_path, fault):
    plan, lock = frozen_plan(tmp_path, formal=fault == "formal_missing")
    manifest = plan.run_manifest
    if fault == "locked":
        ep = lock["forward_locked_episodes"][0]["episode_id"]
        manifest = manifest.model_copy(
            update={
                "episode_ids": (ep,),
                "development": manifest.development.model_copy(
                    update={"episode_ids": (ep,)}
                ),
            }
        )
        plan = plan.model_copy(update={"episode_ids": (ep,)})
    elif fault == "not_predeclared":
        manifest = manifest.model_copy(
            update={
                "development": manifest.development.model_copy(
                    update={
                        "episode_ids": (lock["development_episodes"][1]["episode_id"],)
                    }
                )
            }
        )
    elif fault == "manifest_disagrees":
        plan = plan.model_copy(
            update={"episode_ids": (lock["development_episodes"][1]["episode_id"],)}
        )
    elif fault == "duplicate_manifest":
        manifest = manifest.model_copy(update={"episode_ids": manifest.episode_ids * 2})
    elif fault == "formal_missing":
        plan = plan.model_copy(update={"episode_ids": plan.episode_ids[:-1]})
    elif fault == "forged_lock":
        # A second root cannot be substituted under the unchanged S1 parent.
        manifest = manifest.model_copy(update={"s1_root": str(tmp_path / "fake-s1")})
    elif fault == "trimmed_labels":
        file = tmp_path / "trimmed.jsonl"
        file.write_text(
            "\n".join(
                json.dumps(r)
                for r in rows(plan.labels)
                if r["episode_id"] in plan.episode_ids
            )
            + "\n"
        )
        plan = plan.model_copy(update={"labels": ref(file)})
    plan = plan.model_copy(update={"run_manifest": manifest})
    with pytest.raises(ContractError):
        generate_report(plan, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


@pytest.mark.asyncio
async def test_complete_episode_view_keeps_all_repeats_and_same_judgments():
    data = await score_materials()
    data["repeats"] = 2  # second repeat deliberately missing, stays in denominator
    all_scores = score_report(**data)
    episode = data["labels"][0].episode_id
    view = episode_view(all_scores, [episode])
    assert view["rows"] == [r for r in all_scores["rows"] if r["episode_id"] == episode]
    assert {r["replicate_id"] for r in view["rows"]} == {0, 1}
    for profile, summary in view["profiles"].items():
        selected = [r for r in view["rows"] if r["profile"] == profile]
        assert summary["all_cases"] == metrics(selected)
        assert summary["conditional_p2"] == metrics(
            [r for r in selected if r["p1_correct"]]
        )
    original = copy.deepcopy(data["contract"])
    records = {r.record_id for r in data["labels"] if r.episode_id == episode}
    filtered = dict(
        data,
        references=[r for r in data["references"] if r.record_id in records],
        p2_reviews=[r for r in data["p2_reviews"] if r.record_id in records],
        judgments=[r for r in data["judgments"] if r.record_id in records],
        states={k: v for k, v in data["states"].items() if k[2] == episode},
        answers={k: v for k, v in data["answers"].items() if k[2] == episode},
        p1_graphs={
            k: {ep: g for ep, g in v.items() if ep == episode}
            for k, v in data["p1_graphs"].items()
        },
        p1_reviews={
            k: [r for r in v if r.record_id in records]
            for k, v in data["p1_reviews"].items()
        },
    )
    scoped = score_report(**filtered, episode_ids=(episode,))
    assert scoped == view
    assert data["contract"] == original
    # Cannot trim the parent paths, drop one chosen record, or evaluate another
    # episode's review while claiming the development scope.
    with pytest.raises(ScoringContractError):
        score_rq1(original, [], {}, (), episode_ids=(episode,))
    with pytest.raises(ContractError):
        score_report(**data, episode_ids=("not-in-frozen-parent",))
    with pytest.raises(ContractError):
        score_report(**data, episode_ids=(episode, episode))
    with pytest.raises(ContractError):
        score_report(
            **dict(filtered, labels=data["labels"][:1]), episode_ids=(episode,)
        )
