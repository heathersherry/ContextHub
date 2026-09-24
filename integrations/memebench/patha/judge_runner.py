"""Offline official-answer evaluation of sealed outputs, separately authorized."""

import os
from types import SimpleNamespace
from typing import Literal
from uuid import UUID
import asyncpg
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.models.knowledge import Contract, ContractError, canonical_hash
from contexthub.propagation.candidate import ModelConfig
from .run_gates import FileIdentity, RunManifest, RunAuthorization, stage_preflight
from .cli import DatabaseConfiguration, fixture_transport
from .reporting import RQ2Export, read_json, read_rows
from .schemas import ScoringSidecar
from .chronology import EpisodeBoundary, QuestionVault
from .scorer import validate_seal
from .official_adapter import OfficialJudgeRunner
from .artifacts import ArtifactStore
from .database_snapshot import check_instance
from .run_isolation import require_empty_run_account


class JudgePlan(Contract):
    mode: Literal["synthetic", "development", "formal"]
    run_id: UUID
    manifest: RunManifest | None
    authorization: RunAuthorization | None
    config: ModelConfig
    official_source: FileIdentity
    labels: FileIdentity
    boundaries: FileIdentity
    question_directory: str
    rq2: tuple[RQ2Export, ...]
    database: DatabaseConfiguration
    account_id: str
    credential_environment: str
    synthetic_responses: FileIdentity | None


async def execute_judge(plan, output):
    # Authorization and immutable official/prompt/config identities before DB or
    # external clients. Labels are read only in this offline judge process.
    if plan.mode == "synthetic":
        if (
            plan.manifest is not None
            or plan.authorization is not None
            or plan.synthetic_responses is None
        ):
            raise ContractError("synthetic_judge_identity_invalid")
        plan.synthetic_responses.verify()
    else:
        manifest = stage_preflight(
            plan.manifest,
            plan.authorization,
            "judge",
            configuration_sha256=canonical_hash(plan.config),
        )
        if (
            plan.mode != manifest.mode
            or plan.run_id != manifest.run_id
            or plan.synthetic_responses is not None
        ):
            raise ContractError("judge_plan_run_mismatch")
        frozen = {r.path: r.sha256 for r in manifest.frozen_files}
        from .prepare import artifact_layout

        parent = read_json(manifest.s1_contract)
        if (
            plan.labels.sha256
            != parent["artifact_hashes"][str(artifact_layout("v2")["scoreable"])]
        ):
            raise ContractError("judge_labels_differ_from_s1")
        if any(
            frozen.get(r.path) != r.sha256
            for r in (plan.labels, plan.boundaries, plan.official_source)
        ):
            raise ContractError("judge_inputs_not_frozen")
    plan.database.kwargs()
    plan.official_source.verify()
    labels = [ScoringSidecar.model_validate(r) for r in read_rows(plan.labels)]
    boundaries = {
        k: EpisodeBoundary.model_validate(v)
        for k, v in read_json(plan.boundaries).items()
    }
    key = os.environ.get(plan.credential_environment, "")
    if not key:
        raise ContractError("judge_credential_environment_missing")
    transport = fixture_transport(
        SimpleNamespace(
            rq1=SimpleNamespace(selective=SimpleNamespace(mode=plan.mode)),
            synthetic_responses=plan.synthetic_responses,
        )
    )
    identity = plan.database.identity
    admin = await asyncpg.connect(plan.database.admin_dsn)
    try:
        await check_instance(admin, identity)
    finally:
        await admin.close()
    dsn = f"postgresql://{identity.ordinary_role}@127.0.0.1:{identity.port}/{identity.database}"
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=3, init=init_pg_connection
    )
    runner = None
    try:
        async with pool.acquire() as connection:
            await check_instance(connection, identity, ordinary=True)
        repo = PgRepository(pool)
        await require_empty_run_account(repo, plan.account_id)
        runner = OfficialJudgeRunner(
            plan.config,
            official_source=plan.official_source,
            repository=repo,
            account_id=plan.account_id,
            api_key=key,
            transport=transport,
            mode=plan.mode,
            manifest=plan.manifest,
            authorization=plan.authorization,
        )
        store = ArtifactStore(output)
        store.put("plan.json", plan)
        seen = set()
        index = []
        for number, ref in enumerate(plan.rq2):
            item = (ref.episode_id, ref.replicate_id, ref.profile)
            if item in seen:
                raise ContractError("duplicate_judge_profile")
            seen.add(item)
            if plan.mode != "synthetic" and (
                ref.episode_id not in plan.manifest.episode_ids
                or ref.replicate_id >= plan.manifest.statistics.repeats
            ):
                raise ContractError("judge_case_outside_manifest")
            vault = QuestionVault(plan.question_directory, boundaries[ref.episode_id])
            judgments, attempts = [], []
            for phase in ("before", "after"):
                state_ref, answers_ref = (
                    getattr(ref, phase + "_state"),
                    getattr(ref, phase + "_answers"),
                )
                if state_ref is None or answers_ref is None:
                    continue
                state = validate_seal(read_json(state_ref))
                if (
                    state["episode_id"],
                    state["replicate_id"],
                    state["profile"],
                    state["run_id"],
                    state["phase"],
                ) != (*item, str(plan.run_id), phase):
                    raise ContractError("judge_state_identity_mismatch")
                bundle = vault.release(phase, state)
                questions = {q.question_id: q.text for q in bundle.questions}
                answers = {a["question_id"]: a for a in read_json(answers_ref)}
                for label in labels:
                    if label.episode_id != ref.episode_id:
                        continue
                    question_id = getattr(label, phase + "_question_id")
                    if question_id not in answers:
                        continue
                    if answers[question_id]["state_sha256"] != state["sha256"]:
                        raise ContractError("judge_answer_state_mismatch")
                    judgment, records = await runner.judge(
                        sidecar=label,
                        phase=phase,
                        question=questions[question_id],
                        answer_row=answers[question_id],
                        run_id=plan.run_id,
                        replicate_id=ref.replicate_id,
                        profile=ref.profile,
                    )
                    judgments.append(judgment.model_dump(mode="json"))
                    attempts.extend(r.model_dump(mode="json") for r in records)
            index.append(
                dict(
                    episode_id=ref.episode_id,
                    replicate_id=ref.replicate_id,
                    profile=ref.profile,
                    judgments_file=f"judgments-{number}.json",
                    judgments_sha256=store.put(f"judgments-{number}.json", judgments),
                    attempts_file=f"attempts-{number}.json",
                    attempts_sha256=store.put(f"attempts-{number}.json", attempts),
                )
            )
        store.put("index.json", index)
        return dict(
            directory=str(store.directory),
            profiles=len(index),
            real_execution_authorized=False,
        )
    finally:
        if runner is not None:
            await runner.close()
        await pool.close()
