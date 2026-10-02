"""Explicit S8 stage entry points. No defaults for experiment parameters."""

import argparse
import asyncio
import json
import os
import runpy
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx
from pydantic import Field
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.models.knowledge import Contract, ContractError, canonical_hash
from contexthub.services.knowledge_factory import KnowledgeSecrets
from contexthub.services.context_service import ContextService
from contexthub.services.acl_service import ACLService
from contexthub.services.masking_service import MaskingService
from contexthub.store.context_store import ContextStore
from contexthub.services.knowledge_service import safe_reason
from contexthub.propagation.candidate import ModelConfig
from .artifacts import ArtifactStore, export_account_state
from .chronology import EpisodeBoundary
from .database_snapshot import DatabaseIdentity, capture, check_instance, local_dsn
from .public_rq1_runner import PublicRQ1Configuration, PublicRQ1Result, PublicRQ1Runner
from .rq2_coordinator import RQ2Coordinator
from .answer_prompt import OfficialAnswerConfig
from .episode_deadline import episode_deadline, episode_failure
from .run_gates import (
    FileIdentity,
    StatisticsConfiguration,
    stage_preflight,
    validate_model_seed,
)
from .firewall import validate_maintenance_payload


class DatabaseConfiguration(Contract):
    admin_dsn: str
    identity: DatabaseIdentity
    pg_bin: str

    def kwargs(self):
        local_dsn(self.admin_dsn, self.identity)
        return dict(
            admin_dsn=self.admin_dsn, identity=self.identity, pg_bin=self.pg_bin
        )


class MainReference(Contract):
    result: FileIdentity
    run_id: UUID
    snapshot_directory: str


class StagePlan(Contract):
    run_id: UUID
    rq1: PublicRQ1Configuration
    answer: OfficialAnswerConfig | ModelConfig
    statistics: StatisticsConfiguration
    database: DatabaseConfiguration
    maintenance_inputs: FileIdentity
    boundaries: FileIdentity
    question_directory: str
    episode_ids: tuple[str, ...] = Field(min_length=1)
    # Account per episode AND RQ1 replicate: no reuse, cleanup or auto-renaming.
    accounts: dict[str, tuple[str, ...]]
    agent_id: str
    main_runs: dict[str, MainReference]
    credential_environment: dict[str, str]
    episode_timeout_seconds: float | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )
    synthetic_responses: FileIdentity | None


def preflight_plan(plan, stage):
    config = plan.rq1.selective
    if len(set(plan.episode_ids)) != len(plan.episode_ids):
        raise ContractError("duplicate_episode")
    if config.mode == "synthetic":
        if plan.synthetic_responses is None:
            raise ContractError("explicit_synthetic_responses_required")
        plan.synthetic_responses.verify()
    else:
        if plan.synthetic_responses is not None:
            raise ContractError("synthetic_fixture_not_real_execution")
        manifest = stage_preflight(
            config.manifest,
            config.authorization,
            stage,
            configuration_sha256=canonical_hash(config.knowledge),
        )
        if (
            plan.run_id != manifest.run_id
            or set(plan.episode_ids) != set(manifest.episode_ids)
            or plan.statistics != manifest.statistics
        ):
            raise ContractError("stage_plan_not_frozen")
        from .system_factory import preflight_system

        for system in (plan.rq1.selective, plan.rq1.full_history):
            preflight_system(system, transport=None, stage=stage)
        if stage == "rq2":
            validate_model_seed(manifest, plan.answer)
        timeout_values = json.loads(Path(manifest.timeout_identity.path).read_text())
        if (
            plan.episode_timeout_seconds is None
            or timeout_values.get("episode_timeout_seconds")
            != plan.episode_timeout_seconds
        ):
            raise ContractError("episode_timeout_not_frozen")
        frozen = {r.path: r.sha256 for r in manifest.frozen_files}
        from .prepare import artifact_layout

        parent = json.loads(Path(manifest.s1_contract.path).read_text())
        if (
            plan.maintenance_inputs.sha256
            != parent["artifact_hashes"][str(artifact_layout("v2")["maintenance"])]
        ):
            raise ContractError("stage_input_differs_from_s1")
        if any(
            frozen.get(ref.path) != ref.sha256
            for ref in (plan.maintenance_inputs, plan.boundaries)
        ):
            raise ContractError("stage_input_not_frozen")
    plan.maintenance_inputs.verify()
    plan.boundaries.verify()
    plan.database.kwargs()
    if stage == "rq1":
        if set(plan.accounts) != set(plan.episode_ids):
            raise ContractError("rq1_accounts_incomplete")
        accounts = [a for values in plan.accounts.values() for a in values]
        if len(accounts) != len(set(accounts)) or any(
            len(v) != plan.statistics.repeats for v in plan.accounts.values()
        ):
            raise ContractError("rq1_accounts_not_exclusive")
    elif stage == "rq2":
        if set(plan.main_runs) != set(plan.episode_ids):
            raise ContractError("rq2_main_runs_incomplete")
        for reference in plan.main_runs.values():
            reference.result.verify()


def fixture_transport(plan):
    if plan.rq1.selective.mode != "synthetic":
        return None
    # A hash-pinned local fixture may compute responses from newly generated
    # source IDs. This branch is synthetic-only and still requires MockTransport.
    if Path(plan.synthetic_responses.path).suffix == ".py":
        namespace = runpy.run_path(plan.synthetic_responses.path)
        transport = namespace["build_transport"]()
        if not isinstance(transport, httpx.MockTransport):
            raise ContractError("synthetic_transport_required")
        return transport
    rows = json.loads(Path(plan.synthetic_responses.path).read_text())
    index = 0

    def send(request):
        nonlocal index
        body = json.loads(request.content)
        if index >= len(rows) or canonical_hash(body) != rows[index]["request_sha256"]:
            raise ContractError("synthetic_request_fixture_mismatch")
        row = rows[index]
        index += 1
        return httpx.Response(row["status_code"], json=row["response"])

    return httpx.MockTransport(send)


async def execute_stage(plan, stage, output):
    # This gate is before any connection or model construction.
    preflight_plan(plan, stage)
    rows = [
        validate_maintenance_payload(json.loads(line))
        for line in Path(plan.maintenance_inputs.path).read_text().split("\n")
        if line
    ]
    inputs = {r.episode_id: r for r in rows}
    boundaries = {
        k: EpisodeBoundary.model_validate(v)
        for k, v in json.loads(Path(plan.boundaries.path).read_text()).items()
    }
    if (
        len(inputs) != len(rows)
        or not set(plan.episode_ids) <= inputs.keys()
        or not set(plan.episode_ids) <= boundaries.keys()
    ):
        raise ContractError("stage_episode_materials_incomplete")
    for episode in plan.episode_ids:
        boundaries[episode].check(inputs[episode])
    root = ArtifactStore(output)
    root.put("plan.json", plan)
    secret_values = {
        role: os.environ.get(plan.credential_environment.get(role, ""), "")
        for role in KnowledgeSecrets.__annotations__
    }
    secrets = KnowledgeSecrets(**secret_values)
    answer_key = os.environ.get(plan.credential_environment.get("answer", ""), "")
    embedding_required = any(s.knowledge.discovery.semantic is not None for s in (plan.rq1.selective, plan.rq1.full_history))
    if not all(v for k, v in secret_values.items() if k != "embedding" or embedding_required) or not answer_key:
        raise ContractError("explicit_credential_environment_missing")
    transport = fixture_transport(plan)
    kwargs = plan.database.kwargs()
    admin = await asyncpg.connect(plan.database.admin_dsn)
    try:
        await check_instance(admin, plan.database.identity)
    finally:
        await admin.close()
    identity = plan.database.identity
    ordinary = f"postgresql://{identity.ordinary_role}@127.0.0.1:{identity.port}/{identity.database}"
    pool = await asyncpg.create_pool(
        ordinary, min_size=1, max_size=5, init=init_pg_connection
    )
    summary = []
    deadline_aborted = False
    try:
        async with pool.acquire() as connection:
            await check_instance(connection, identity, ordinary=True)
        repo = PgRepository(pool)
        context = ContextService(
            ContextStore(ACLService(), MaskingService()), ACLService()
        )
        for episode in plan.episode_ids:
            for replicate in range(plan.statistics.repeats):
                destination = root.directory / f"{episode}-{replicate}"
                case = ArtifactStore(destination)
                if deadline_aborted:
                    failure = episode_failure(
                        episode, replicate, "not_started_after_episode_timeout"
                    )
                    case.put("failure.json", failure)
                    summary.append(failure)
                    continue
                try:
                    async with episode_deadline(plan.episode_timeout_seconds):
                        if stage == "rq1":

                            async def seal(handoff, configuration_sha256):
                                return await capture(
                                    **kwargs,
                                    directory=case.directory / "main",
                                    run_id=str(plan.run_id),
                                    handoff=handoff,
                                    configuration_sha256=configuration_sha256,
                                )

                            runner = PublicRQ1Runner(
                                plan.rq1,
                                repository=repo,
                                context_service=context,
                                secrets=secrets,
                                transport=transport,
                            )
                            result = await runner.run(
                                inputs[episode],
                                account_id=plan.accounts[episode][replicate],
                                agent_id=plan.agent_id,
                                run_id=plan.run_id,
                                replicate_id=replicate,
                                cutoff_ordinal=boundaries[episode].before.ordinal,
                                seal_selective=seal if replicate == 0 else None,
                            )
                            case.put("result.json", result)
                        else:
                            reference = plan.main_runs[episode]
                            main = PublicRQ1Result.model_validate_json(
                                Path(reference.result.path).read_text()
                            )
                            cfg = plan.rq1.selective
                            coordinator = RQ2Coordinator(
                                knowledge=cfg.knowledge,
                                answer_config=plan.answer,
                                secrets=secrets,
                                answer_key=answer_key,
                                context_service=context,
                                transport=transport,
                                mode=cfg.mode,
                                manifest=cfg.manifest,
                                authorization=cfg.authorization,
                            )
                            result = await coordinator.run(
                                inputs[episode],
                                boundaries[episode],
                                main=main,
                                expected_main_run_id=reference.run_id,
                                snapshot_directory=reference.snapshot_directory,
                                database=kwargs,
                                question_directory=plan.question_directory,
                                output=case.directory / "rq2",
                                run_id=plan.run_id,
                                replicate_id=replicate,
                            )
                    summary.append(
                        dict(
                            episode_id=episode,
                            replicate_id=replicate,
                            status="recorded",
                        )
                    )
                except Exception as exc:
                    # All requested episodes/replicates retain a result even if
                    # no graph, answer or maintenance output was produced.
                    deadline_aborted = isinstance(exc, TimeoutError)
                    failure = episode_failure(episode, replicate, safe_reason(exc))
                    case.put("failure.json", failure)
                    if stage == "rq1":
                        case.put(
                            "failure-state.json",
                            await export_account_state(
                                repo,
                                plan.accounts[episode][replicate],
                                agent_id=plan.agent_id,
                            ),
                        )
                    summary.append(failure)
        root.put("summary.json", summary)
        return summary
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=(
            "preflight-rq1",
            "preflight-rq2",
            "rq1",
            "rq2",
            "score",
            "freeze-scorer",
            "package-questions",
            "judge",
        ),
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None and not args.stage.startswith("preflight-"):
        parser.error("--output is required and must be a new directory")
    if args.stage == "score":
        from .reporting import ScorePlan, generate_report

        value = generate_report(
            ScorePlan.model_validate_json(args.plan.read_text()), args.output
        )
    elif args.stage == "freeze-scorer":
        from .reporting import dependency_identity

        value = dependency_identity(
            FileIdentity.model_validate_json(args.plan.read_text()), output=args.output
        )
    elif args.stage == "package-questions":
        from .chronology import package_s1_questions

        value = package_s1_questions(json.loads(args.plan.read_text()), args.output)
    elif args.stage == "judge":
        from .judge_runner import JudgePlan, execute_judge

        value = asyncio.run(
            execute_judge(
                JudgePlan.model_validate_json(args.plan.read_text()), args.output
            )
        )
    else:
        plan = StagePlan.model_validate_json(args.plan.read_text())
        stage = args.stage.removeprefix("preflight-")
        if args.stage.startswith("preflight-"):
            preflight_plan(plan, stage)
            value = dict(
                stage=stage,
                configuration_valid=True,
                execution_authorized=plan.rq1.selective.mode != "synthetic",
            )
        else:
            value = asyncio.run(execute_stage(plan, stage, args.output))
    print(json.dumps(value))


if __name__ == "__main__":
    main()
