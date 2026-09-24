"""Offline artifact loading, score/report CLI support and additive code identity."""

import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal
from uuid import UUID
from pydantic import Field
from contexthub.models.knowledge import Contract, ContractError, canonical_hash
from .artifacts import ArtifactStore, file_hash
from .run_gates import FileIdentity, StatisticsConfiguration, RunManifest
from .public_rq1_runner import PublicRQ1Result
from .rq1_scorer import PathReview
from .scorer import CaseReference, P2Review, score_report, episode_view
from .official_adapter import OfficialJudgment
from .ledger import rq1_costs, export_cost
from .bootstrap import comparison_report
from .firewall import validate_maintenance_payload


def read_json(ref):
    ref.verify()
    return json.loads(Path(ref.path).read_text())


def read_rows(ref):
    ref.verify()
    # LF only; U+2028/U+2029 in original material must not become delimiters.
    return [json.loads(line) for line in Path(ref.path).read_text().split("\n") if line]


class RQ1Export(Contract):
    episode_id: str
    replicate_id: int = Field(ge=0, strict=True)
    result: FileIdentity | None
    failure_state: FileIdentity | None


class RQ2Export(Contract):
    episode_id: str
    replicate_id: int = Field(ge=0, strict=True)
    profile: Literal["local/base-only", "full/strong-only", "adaptive"]
    metadata: FileIdentity | None
    before_state: FileIdentity | None
    after_state: FileIdentity | None
    before_answers: FileIdentity | None
    after_answers: FileIdentity | None
    maintenance_attempts: FileIdentity | None
    answer_attempts: FileIdentity | None
    failure_state: FileIdentity | None
    failure_answer_state: FileIdentity | None


class ScorePlan(Contract):
    mode: Literal["synthetic", "development", "formal"]
    run_id: UUID
    rq1_run_id: UUID
    protocol: StatisticsConfiguration
    episode_ids: tuple[str, ...] = Field(min_length=1)
    s1_contract: FileIdentity
    maintenance_inputs: FileIdentity
    labels: FileIdentity
    paths: FileIdentity
    scorer_identity: FileIdentity
    run_manifest: RunManifest | None
    official_source: FileIdentity
    judge_configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rq1: tuple[RQ1Export, ...]
    rq2: tuple[RQ2Export, ...]
    # Path review JSON rows: arm,replicate_id,review(PathReview).
    path_reviews: FileIdentity | None
    case_references: FileIdentity | None
    p2_reviews: FileIdentity | None
    judgments: tuple[FileIdentity, ...]
    judge_attempts: tuple[FileIdentity, ...]


def dependency_identity(s1_contract, *, output):
    """Create a new identity record without changing any archived S1 byte/hash."""
    s1_contract.verify()
    root = Path(__file__).resolve().parents[3]
    files = {
        str(p.resolve()): file_hash(p)
        for folder in ("src/contexthub", "sdk/src", "integrations/memebench/patha")
        for p in (root / folder).rglob("*.py")
    }
    dependencies = {
        str((root / p).resolve()): file_hash(root / p)
        for p in ("pyproject.toml", "uv.lock")
        if (root / p).is_file()
    }
    versions = {
        p: importlib.metadata.version(p)
        for p in ("pydantic", "httpx", "asyncpg", "anyio")
    }
    store = ArtifactStore(output)
    diff = subprocess.check_output(["git", "-C", str(root), "diff", "--binary", "HEAD"])
    (store.directory / "dirty.diff").write_bytes(diff)
    result = dict(
        version="patha-s8-scorer-identity-v1",
        s1_contract_sha256=s1_contract.sha256,
        s1_contract_path=s1_contract.path,
        code_files=files,
        dependency_files=dependencies,
        runtime_dependencies=versions,
        python=sys.version,
        code_commit=subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        dirty_diff_sha256=file_hash(store.directory / "dirty.diff"),
        entrypoint="integrations.memebench.patha.cli score",
        real_execution_authorized=False,
    )
    store.put("identity.json", result)
    store.put("runtime-dependencies.json", versions)
    return result


def validate_score_plan(plan):
    identity = read_json(plan.scorer_identity)
    if identity.get("s1_contract_sha256") != plan.s1_contract.sha256:
        raise ContractError("scorer_s1_parent_mismatch")
    root = Path(__file__).resolve().parents[3]
    current = {
        str(p.resolve()): file_hash(p)
        for folder in ("src/contexthub", "sdk/src", "integrations/memebench/patha")
        for p in (root / folder).rglob("*.py")
    }
    if identity.get("code_files") != current or any(
        file_hash(p) != h for p, h in identity.get("dependency_files", {}).items()
    ):
        raise ContractError("scorer_code_identity_mismatch")
    if identity.get("python") != sys.version or any(
        importlib.metadata.version(p) != v
        for p, v in identity.get("runtime_dependencies", {}).items()
    ):
        raise ContractError("scorer_runtime_identity_mismatch")
    if len(set(plan.episode_ids)) != len(plan.episode_ids):
        raise ContractError("score_episode_duplicates")
    contract = read_json(plan.s1_contract)
    if plan.mode != "synthetic":
        from .preflight import preflight_artifacts

        manifest = plan.run_manifest
        if (
            manifest is None
            or manifest.mode != plan.mode
            or manifest.run_id != plan.run_id
            or manifest.statistics != plan.protocol
        ):
            raise ContractError("score_formal_identity_missing")
        if (
            manifest.s1_contract != plan.s1_contract
            or manifest.scorer_identity != plan.scorer_identity
        ):
            raise ContractError("score_manifest_parent_mismatch")
        if (
            Path(plan.s1_contract.path).resolve()
            != (Path(manifest.s1_root) / "manifests/scorer-contract-v2.json").resolve()
        ):
            raise ContractError("score_manifest_parent_mismatch")
        preflight_artifacts(Path(manifest.s1_root), material_version="v2")
        from .prepare import artifact_layout

        layout = artifact_layout("v2")
        for key, ref in (
            ("maintenance", plan.maintenance_inputs),
            ("scoreable", plan.labels),
            ("paths", plan.paths),
        ):
            if ref.sha256 != contract["artifact_hashes"][str(layout[key])]:
                raise ContractError("score_input_differs_from_s1_frozen_bytes")
        if (
            plan.official_source.sha256
            != contract["official_answer"]["reference"]["judge_sha256"]
        ):
            raise ContractError("score_official_code_differs_from_s1")
        frozen = {r.path: r.sha256 for r in manifest.frozen_files}
        if any(
            frozen.get(r.path) != r.sha256
            for r in (
                plan.labels,
                plan.paths,
                plan.official_source,
                plan.maintenance_inputs,
            )
        ):
            raise ContractError("score_privileged_input_not_frozen")
        if plan.mode == "formal" and (
            len(plan.episode_ids) != 100
            or set(plan.episode_ids) != set(manifest.episode_ids)
            or contract["frozen_record_universe"]["record_count"] != 294
        ):
            raise ContractError("formal_score_universe_mismatch")
        score_partition(plan, contract)
    elif plan.run_manifest is not None:
        raise ContractError("synthetic_score_cannot_claim_formal_manifest")
    return contract


def score_partition(plan, contract):
    """Validate the episode view against S1 and the predeclared run scope.

    This is offline membership validation, not a real-execution authorization.
    The original contract/files are parents, never rewritten subset contracts.
    """
    if plan.mode == "synthetic":
        return None
    from .forward_lock import validate_forward_lock_manifest

    manifest = plan.run_manifest
    lock_ref = FileIdentity(
        path=str(Path(manifest.s1_root) / "manifests/forward-lock-v1.json"),
        sha256=contract["artifact_hashes"]["manifests/forward-lock-v1.json"],
    )
    lock = read_json(lock_ref)
    validate_forward_lock_manifest(lock)
    development = {r["episode_id"] for r in lock["development_episodes"]}
    locked = tuple(r["episode_id"] for r in lock["forward_locked_episodes"])
    declared = set(manifest.episode_ids)
    if (
        len(declared) != len(manifest.episode_ids)
        or set(plan.episode_ids) != declared
        or not set(manifest.development.episode_ids) <= development
    ):
        raise ContractError("score_episode_manifest_mismatch")
    if plan.mode == "development":
        if not declared <= set(manifest.development.episode_ids):
            raise ContractError("score_development_episode_not_predeclared")
    elif declared != development | set(locked):
        raise ContractError("formal_score_universe_mismatch")
    return lock_ref, locked


def score_scope(plan, contract, labels, episode_ids, *, name, partition):
    selected = set(episode_ids)
    record_ids = [r["record_id"] for r in labels if r["episode_id"] in selected]
    result = dict(
        version="patha-s8-score-scope-v1",
        name=name,
        mode=plan.mode,
        run_id=str(plan.run_id),
        parent_s1_contract=plan.s1_contract.model_dump(mode="json"),
        parent_contract_sha256=contract["contract_sha256"],
        parent_labels=plan.labels.model_dump(mode="json"),
        parent_paths=plan.paths.model_dump(mode="json"),
        parent_record_denominator=contract["frozen_record_universe"]["record_count"],
        run_manifest_sha256=canonical_hash(plan.run_manifest)
        if plan.run_manifest
        else None,
        forward_lock=partition[0].model_dump(mode="json") if partition else None,
        episode_ids=list(episode_ids),
        record_ids=record_ids,
        record_denominator=len(record_ids),
        selection="all frozen records of each selected complete episode; independent of outcomes",
        real_execution_authorized=False,
    )
    return dict(result, sha256=canonical_hash(result))


def _failure_attempts(state, excluded=()):
    excluded = set(excluded)
    rows = []
    for item in state["attempts"]:
        if canonical_hash(item["payload"]) != item["sha256"]:
            raise ContractError("failure_attempt_hash_mismatch")
        if item["sha256"] not in excluded:
            rows.append(item["payload"])
    return rows


def generate_report(plan: ScorePlan, output):
    contract = validate_score_plan(plan)
    partition = score_partition(plan, contract)
    labels, paths = read_rows(plan.labels), read_rows(plan.paths)
    inputs = {
        r.episode_id: r
        for r in (
            validate_maintenance_payload(row)
            for row in read_rows(plan.maintenance_inputs)
        )
    }
    if not set(plan.episode_ids) <= inputs.keys():
        raise ContractError("score_input_identity_missing")
    if plan.mode != "development" and set(r["episode_id"] for r in labels) - set(
        plan.episode_ids
    ):
        raise ContractError("score_episode_universe_missing")
    plan.official_source.verify()
    graphs, reviews, states, answers, costs, physical = {}, {}, {}, {}, [], []
    seen = set()
    main = {}
    for ref in plan.rq1:
        key = (ref.episode_id, ref.replicate_id)
        if (
            key in seen
            or ref.episode_id not in plan.episode_ids
            or not 0 <= ref.replicate_id < plan.protocol.repeats
        ):
            raise ContractError("rq1_export_membership_invalid")
        seen.add(key)
        if ref.result is None:
            if ref.failure_state:
                physical.append(
                    dict(
                        episode_id=ref.episode_id,
                        replicate_id=ref.replicate_id,
                        failure_unallocated=export_cost(
                            _failure_attempts(read_json(ref.failure_state)),
                            wall_seconds=None,
                            attribution="P1/failed/physical-unallocated",
                        ),
                    )
                )
            continue
        result = PublicRQ1Result.model_validate(read_json(ref.result))
        if (
            result.run_id != plan.rq1_run_id
            or result.replicate_id != ref.replicate_id
            or result.input_sha256 != inputs[ref.episode_id].input_sha256
        ):
            raise ContractError("rq1_export_run_mismatch")
        for i, arm in enumerate(("selective", "full-history")):
            if len(result.graph_evidence) > i and result.graph_evidence[i] is not None:
                graphs.setdefault((arm, ref.replicate_id), {})[ref.episode_id] = (
                    result.graph_evidence[i]
                )
        if ref.replicate_id == 0:
            main[ref.episode_id] = result
        allocated = rq1_costs(result)
        for arm in ("selective", "full-history"):
            costs.append(
                dict(
                    episode_id=ref.episode_id,
                    replicate_id=ref.replicate_id,
                    stage="P1",
                    arm=arm,
                    cost=allocated[arm],
                )
            )
        physical.append(
            dict(
                episode_id=ref.episode_id,
                replicate_id=ref.replicate_id,
                shared=allocated["shared_physical"],
                total=allocated["physical_total"],
            )
        )
    if plan.path_reviews:
        for row in read_json(plan.path_reviews):
            key = (row["arm"], row["replicate_id"])
            if key[0] not in ("selective", "full-history") or key[1] not in range(
                plan.protocol.repeats
            ):
                raise ContractError("path_review_run_membership_invalid")
            reviews.setdefault(key, []).append(PathReview.model_validate(row["review"]))
    seen = set()
    for ref in plan.rq2:
        key = (ref.profile, ref.replicate_id, ref.episode_id)
        if (
            key in seen
            or ref.episode_id not in plan.episode_ids
            or ref.replicate_id not in range(plan.protocol.repeats)
        ):
            raise ContractError("rq2_export_membership_invalid")
        seen.add(key)
        for phase in ("before", "after"):
            sr, ar = getattr(ref, phase + "_state"), getattr(ref, phase + "_answers")
            if sr:
                states[(*key, phase)] = read_json(sr)
            if ar:
                answers[(*key, phase)] = read_json(ar)
        metadata = read_json(ref.metadata) if ref.metadata else None
        if metadata and (
            metadata["run_id"] != str(plan.run_id)
            or metadata["episode_id"] != ref.episode_id
            or metadata["profile"] != ref.profile
            or metadata["replicate_id"] != ref.replicate_id
        ):
            raise ContractError("rq2_export_run_mismatch")
        before = states.get((*key, "before"))
        primary = main.get(ref.episode_id)
        if before is not None and (
            primary is None
            or primary.selective_snapshot is None
            or before["main_snapshot_sha256"]
            != canonical_hash(primary.selective_snapshot)
        ):
            raise ContractError("rq2_export_not_primary_snapshot")
        after = states.get((*key, "after"))
        if after is not None and (
            before is None
            or after["main_snapshot_sha256"] != before["main_snapshot_sha256"]
        ):
            raise ContractError("rq2_after_snapshot_binding_mismatch")
        records = (
            read_json(ref.maintenance_attempts) if ref.maintenance_attempts else None
        )
        if records is None and ref.failure_state:
            excluded = (
                [a["sha256"] for a in before["state"]["attempts"]] if before else ()
            )
            if before is None:
                if primary is None:
                    raise ContractError("failed_p2_cost_origin_unknown")
                excluded = [canonical_hash(r) for r in primary.records]
            records = _failure_attempts(read_json(ref.failure_state), excluded)
        if records is not None:
            costs.append(
                dict(
                    episode_id=ref.episode_id,
                    replicate_id=ref.replicate_id,
                    stage="P2",
                    arm=ref.profile,
                    cost=export_cost(
                        records,
                        wall_seconds=metadata.get("maintenance_wall_seconds")
                        if metadata
                        else None,
                        attribution="P2/" + ref.profile,
                    ),
                )
            )
        records = read_json(ref.answer_attempts) if ref.answer_attempts else None
        if records is None and ref.failure_answer_state:
            records = _failure_attempts(read_json(ref.failure_answer_state))
        if records is not None:
            # Sum of nonoverlapping per-answer attempts is diagnostic service
            # time, not P2 wall time. Answer stage wall is explicitly unavailable.
            costs.append(
                dict(
                    episode_id=ref.episode_id,
                    replicate_id=ref.replicate_id,
                    stage="answer",
                    arm=ref.profile,
                    cost=export_cost(
                        records, wall_seconds=None, attribution="answer/" + ref.profile
                    ),
                )
            )
    references = (
        [CaseReference.model_validate(r) for r in read_json(plan.case_references)]
        if plan.case_references
        else []
    )
    p2 = (
        [P2Review.model_validate(r) for r in read_json(plan.p2_reviews)]
        if plan.p2_reviews
        else []
    )
    judgments = [
        OfficialJudgment.model_validate(r)
        for ref in plan.judgments
        for r in read_json(ref)
    ]
    report = score_report(
        contract=contract,
        labels=labels,
        paths=paths,
        p1_graphs=graphs,
        p1_reviews=reviews,
        references=references,
        p2_reviews=p2,
        states=states,
        answers=answers,
        judgments=judgments,
        run_id=plan.run_id,
        repeats=plan.protocol.repeats,
        official_code_sha256=plan.official_source.sha256,
        judge_configuration_sha256=plan.judge_configuration_sha256,
        episode_ids=plan.episode_ids,
    )
    scope = score_scope(
        plan,
        contract,
        labels,
        plan.episode_ids,
        name="development" if plan.mode == "development" else "all",
        partition=partition,
    )
    report["scope"] = scope
    statistics = comparison_report(
        report, costs, episode_ids=plan.episode_ids, protocol=plan.protocol
    )
    judge_cost = export_cost(
        [r for ref in plan.judge_attempts for r in read_json(ref)],
        wall_seconds=None,
        attribution="judge",
        complete_export=bool(plan.judge_attempts),
    )
    store = ArtifactStore(output)
    store.put("plan.json", plan)
    store.put("scores.json", report)
    store.put("statistics.json", statistics)
    store.put("scope.json", scope)
    store.put(
        "costs.json",
        dict(
            logical=costs,
            rq1_physical=physical,
            judge=judge_cost,
            judge_records_supplied=bool(plan.judge_attempts),
            no_duplicate_experiment_billing=True,
        ),
    )
    if plan.mode == "formal":
        locked = partition[1]
        locked_scores = episode_view(report, locked)
        locked_scope = score_scope(
            plan, contract, labels, locked, name="forward-locked", partition=partition
        )
        locked_scope["parent_scores_file"] = dict(
            path="../scores.json", sha256=file_hash(store.directory / "scores.json")
        )
        locked_scope["sha256"] = canonical_hash(
            {k: v for k, v in locked_scope.items() if k != "sha256"}
        )
        locked_scores["scope"] = locked_scope
        locked_costs = [r for r in costs if r["episode_id"] in locked]
        locked_stats = comparison_report(
            locked_scores, locked_costs, episode_ids=locked, protocol=plan.protocol
        )
        subset = ArtifactStore(store.directory / "forward-locked")
        subset.put("scope.json", locked_scope)
        subset.put("scores.json", locked_scores)
        subset.put("statistics.json", locked_stats)
        subset.put(
            "costs.json",
            dict(
                logical=locked_costs,
                rq1_physical=[r for r in physical if r["episode_id"] in locked],
                judge=None,
                judge_note="Run-wide judge ledger is in ../costs.json; no episode attribution is inferred.",
                no_duplicate_experiment_billing=True,
            ),
        )
        with (subset.directory / "report.md").open("x") as handle:
            handle.write(render_report(locked_scores, locked_stats, mode=plan.mode))
    with (store.directory / "report.md").open("x") as handle:
        handle.write(render_report(report, statistics, mode=plan.mode))
        if plan.mode == "formal":
            handle.write(
                "\n[Fixed 20-episode forward-locked evidence from this same run](forward-locked/report.md).\n"
            )
    index = {
        str(p.relative_to(store.directory)): file_hash(p)
        for p in store.directory.rglob("*")
        if p.is_file()
    }
    store.put("files.json", index)
    return dict(
        directory=str(store.directory),
        mode=plan.mode,
        record_denominator=report["record_denominator"],
        real_execution_authorized=False,
        files=index,
    )


def render_report(report, statistics, *, mode):
    lines = [
        f"Path A S8 offline report ({mode})",
        "",
        f"Frozen event-target denominator: {report['record_denominator']}; repeats: {report['repeats']}.",
        "Missing/failed/unreviewed cases remain in denominators. No runtime verifier is a scoring oracle.",
        "",
        "| Profile | All cases | Joint correct | Conditional P2 correct/total | Actual fresh | False fresh | FF main | Cas recovery | Abs unresolved |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if "scope" in report:
        scope = report["scope"]
        lines[2:2] = [
            f"Scope: {scope['name']}; complete episodes: {len(scope['episode_ids'])}; parent frozen denominator: {scope['parent_record_denominator']}.",
            "Scope membership and unchanged S1 parent identities are in scope.json. Forward-locked evidence is not an unseen test.",
        ]
    for profile, value in report["profiles"].items():
        a, c = value["all_cases"], value["conditional_p2"]
        ff = "N/A" if a["false_fresh_main"] is None else str(a["false_fresh_main"])
        lines.append(
            f"| {profile} | {a['total']} | {a['joint_correct']} | {c['p2_correct']}/{c['total']} | {a['actual_fresh']} | {a['false_fresh_count']} | {ff} | {a['cas_recovered']}/{a['recoverable_cas']} | {a['abs_correct_unresolved']}/{a['abs_total']} |"
        )
    lines += [
        "",
        "Official real/trivial/knew_but_failed/never_knew/unknown counts and four separate quality metrics are in scores.json.",
        "Paired 10,000 episode-cluster samples, single-sided bounds and two-sided intervals are in statistics.json.",
        "Core attempt attribution, shared logical versus physical cost, and separate answer/judge costs are in costs.json.",
        "S2 token-tariff precision applies; unknown usage or prices are not zero. Missing intervals/tolerances cannot certify lower complete cost.",
        "",
        "| Comparison | Supported | One-sided lower | One-sided upper | Two-sided 95% |",
        "|---|---|---:|---:|---|",
    ]
    for name, row in statistics["comparisons"].items():
        if not isinstance(row, dict):
            lines.append(f"| {name} | {row} | | | |")
            continue
        interval = row["interval"]
        lines.append(
            f"| {name} | {row['passed']} | {interval['lower_one_sided_95']} | {interval['upper_one_sided_95']} | {interval['two_sided_95']} |"
        )
    lines += [
        "",
        "This report grants no real-run authorization and is not independent acceptance.",
        "",
    ]
    return "\n".join(lines)
