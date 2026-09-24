import pytest
from contexthub.models.knowledge import ContractError
from integrations.memebench.patha.scorer import score_report, metrics
from integrations.memebench.patha.schemas import canonical_sha256
from s8_helpers import score_materials


@pytest.mark.asyncio
async def test_separate_joint_conditional_official_and_zero_fresh():
    data = await score_materials()
    report = score_report(**data)
    assert len(report["rows"]) == 6
    assert all(r["kappa_pass"] for r in report["p1"])
    for value in report["profiles"].values():
        a = value["all_cases"]
        abs_ = value["by_task"]["Abs"]
        assert a["joint_correct"] == 2 and a["rho_p2"] == 1 and a["tau_ff_pass"] is True
        assert set(a["quality"].values()) == {1.0}
        assert abs_["false_fresh_main"] is None and abs_["abs_correct_unresolved"] == 1
        assert abs_["abs_zero_fresh_acceptable"] is True
    data["p1_reviews"] = {}
    report = score_report(**data)
    assert all(
        p["all_cases"]["joint_correct"] == 0 and p["conditional_p2"]["total"] == 0
        for p in report["profiles"].values()
    )
    assert all(p["all_cases"]["p2_correct"] == 2 for p in report["profiles"].values())


@pytest.mark.asyncio
async def test_missing_failure_unknown_and_runtime_verifier_not_truth():
    data = await score_materials()
    data["p2_reviews"] = []
    data["judgments"] = []
    for seal in data["states"].values():
        seal["state"]["runtime_verifier"] = "PASS"
        seal.pop("sha256")
        seal["sha256"] = canonical_sha256(seal)
    report = score_report(**data)
    for value in report["profiles"].values():
        a = value["all_cases"]
        assert a["total"] == 2 and a["p2_correct"] == 0 and a["unreviewed_fresh"] == 1
        assert a["tau_ff_pass"] is None and a["quality"]["official_Cas"] == 0
    data["states"] = {}
    data["answers"] = {}
    data["references"] = []
    report = score_report(**data)
    for value in report["profiles"].values():
        a = value["all_cases"]
        assert a["total"] == 2 and a["false_fresh_main"] is None
        assert a["rho_p2_pass"] is None and a["tau_ff_pass"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["label", "state", "version", "evidence", "judge", "universe"]
)
async def test_offline_evidence_binding(fault):
    data = await score_materials()
    if fault == "label":
        data["p2_reviews"][0] = data["p2_reviews"][0].model_copy(
            update={"label_sha256": "f" * 64}
        )
    if fault == "state":
        next(iter(data["states"].values()))["state"]["contexts"][0]["version"] = 99
    if fault == "version":
        r = data["p2_reviews"][0]
        data["p2_reviews"][0] = r.model_copy(
            update={"final_target": r.final_target.model_copy(update={"version": 999})}
        )
    if fault == "evidence":
        key = next(k for k in data["states"] if k[-1] == "after")
        seal = data["states"][key]
        seal["state"]["context_versions"] = []
        seal.pop("sha256")
        seal["sha256"] = canonical_sha256(seal)
        data["p2_reviews"][0] = data["p2_reviews"][0].model_copy(
            update={"state_sha256": seal["sha256"]}
        )
    if fault == "judge":
        data["judgments"][0] = data["judgments"][0].model_copy(
            update={"official_code_sha256": "f" * 64}
        )
    if fault == "universe":
        data["labels"] = data["labels"][:-1]
    with pytest.raises(ContractError):
        score_report(**data)


@pytest.mark.asyncio
async def test_false_fresh_main_actual_fresh_and_all_case_auxiliary():
    data = await score_materials()
    data["p2_reviews"][0] = data["p2_reviews"][0].model_copy(
        update={"verdict": "INCORRECT"}
    )
    report = score_report(**data)
    a = report["profiles"]["local/base-only"]["all_cases"]
    assert (
        a["false_fresh_count"] == 1
        and a["false_fresh_main"] == 1
        and a["false_fresh_all"] == 0.5
    )
    assert a["tau_ff_pass"] is False and a["rho_p2_pass"] is False
    # All-stale does not pass Cas recovery merely because FF_main is N/A.
    rows = [
        dict(r, fresh=False, p2_correct=False, false_fresh=False)
        for r in report["rows"]
    ]
    assert metrics(rows)["false_fresh_main"] is None
    assert metrics(rows)["rho_p2_pass"] is False


@pytest.mark.asyncio
async def test_all_294_including_pl030_and_100_episode_universe_never_filtered():
    from s8_helpers import scoring_label
    from integrations.memebench.patha.schemas import ScoringSidecar
    from integrations.memebench.patha.scoring_contract import build_scoring_contract

    data = await score_materials()
    labels = []
    paths = []
    for i in range(294):
        raw = scoring_label(
            index=i + 1,
            kind="Cas" if i < 164 else "Abs",
            raw="pl_030" if i == 29 else "synthetic",
        ).model_dump(mode="json", exclude_none=True)
        raw["episode_id"] = "ep_" + f"{i % 100 + 1:024x}"
        raw.pop("record_sha256")
        raw["record_sha256"] = canonical_sha256(raw)
        labels.append(ScoringSidecar.model_validate(raw))
        path = dict(
            record_id=raw["record_id"],
            episode_id=raw["episode_id"],
            necessary_path_candidates=[],
        )
        path["record_sha256"] = canonical_sha256(path)
        paths.append(path)
    data.update(
        labels=labels,
        paths=paths,
        contract=build_scoring_contract(
            [r.model_dump(mode="json") for r in labels],
            paths,
            artifact_hashes={},
            contract_validator_code={},
        ),
        p1_graphs={},
        p1_reviews={},
        references=[],
        p2_reviews=[],
        states={},
        answers={},
        judgments=[],
    )
    report = score_report(**data)
    assert report["record_denominator"] == 294 and len(report["rows"]) == 882
    assert len({r["episode_id"] for r in report["rows"]}) == 100
    assert sum(r["task_type"] == "Cas" for r in report["rows"]) == 164 * 3
    assert sum(r["record_id"] == labels[29].record_id for r in report["rows"]) == 3
    assert all(
        r["retained_in_denominator"] and not r["p2_correct"] for r in report["rows"]
    )


@pytest.mark.asyncio
async def test_rho_threshold_exact_counts_and_four_quality_not_averaged():
    report = score_report(**await score_materials())
    row = next(r for r in report["rows"] if r["task_type"] == "Cas")
    for failures, expected in ((1, True), (2, True), (3, False)):
        rows = [dict(row, p2_correct=i >= failures) for i in range(20)]
        assert metrics(rows)["rho_p2_pass"] is expected
    rows = [
        dict(r, p2_correct=False) if r["task_type"] == "Abs" else r
        for r in report["rows"]
    ]
    value = metrics(rows)
    assert value["quality"] == dict(
        internal_Cas=1, internal_Abs=0, official_Cas=1, official_Abs=1
    )
