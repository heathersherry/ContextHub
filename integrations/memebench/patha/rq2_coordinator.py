"""One fixed episode chronology over independent copies of the selective main.

A maintenance round is one post-ingestion sweep over actual core event/watch
bindings. Core event executions are retained individually, never hidden or
relabelled as one core call. No question/task/hop labels select the events.
"""

import json
from pathlib import Path
import httpx
from time import monotonic
from uuid import UUID, uuid4

import asyncpg
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.models.knowledge_phases import AssertionChangeRequest
from contexthub.models.knowledge_public import KnowledgeMaintainRequest
from contexthub.models.request import RequestContext
from .answer_runner import AnswerRunner
from .artifacts import ArtifactStore, export_account_state, file_hash
from .chronology import QuestionVault
from .database_snapshot import restore
from .system_factory import SystemConfiguration, create_system
from .schemas import canonical_sha256
from .run_gates import stage_preflight
from contexthub.services.knowledge_service import safe_reason

PROFILES = ("local/base-only", "full/strong-only", "adaptive")


def validate_main(result, *, expected_run_id, runtime, configuration):
    snapshot = result.selective_snapshot
    if (
        result.run_id != expected_run_id
        or result.replicate_id != 0
        or snapshot is None
        or len(result.handoffs) != 2
    ):
        raise ContractError("rq2_selective_main_required")
    if (
        snapshot.run_id,
        snapshot.handoff,
        snapshot.configuration_sha256,
        result.input_sha256,
    ) != (
        str(expected_run_id),
        result.handoffs[0],
        canonical_hash(configuration),
        runtime.input_sha256,
    ):
        raise ContractError("rq2_main_identity_mismatch")
    return snapshot


def seal_state(
    store, phase, boundary, state, *, run_id, replicate_id, profile, snapshot
):
    payload = dict(
        phase=phase,
        episode_id=boundary.episode_id,
        prefix_sha256=getattr(boundary, phase).prefix_sha256,
        run_id=str(run_id),
        replicate_id=replicate_id,
        profile=profile,
        main_snapshot_sha256=canonical_hash(snapshot),
        graph=snapshot.graph.model_dump(mode="json"),
        state=state,
    )
    payload["sha256"] = canonical_sha256(payload)
    store.put(phase + "-state.json", payload)
    return payload


async def bound_events(repository, ctx, event_ids, graph):
    """Read the core's registered watch bindings, without inferring any edges."""
    selected, ignored = [], []
    async with repository.session(ctx.account_id) as db:
        for ident in dict.fromkeys(event_ids):
            event = await db.fetchrow(
                "SELECT metadata FROM change_events WHERE account_id=$1 AND event_id=$2",
                ctx.account_id,
                ident,
            )
            if event is None:
                raise ContractError("rq2_returned_event_missing")
            watches = (event["metadata"] or {}).get("s5_watches", [])
            matches = []
            for ref in watches:
                row = await db.fetchrow(
                    "SELECT sha256,payload FROM knowledge_artifacts WHERE account_id=$1 AND id=$2",
                    ctx.account_id,
                    UUID(ref["id"]),
                )
                if (
                    row is None
                    or row["sha256"] != ref["sha256"]
                    or canonical_hash(row["payload"]) != ref["sha256"]
                ):
                    raise ContractError("rq2_event_watch_hash_mismatch")
                if row["payload"]["graph"] == graph.model_dump(mode="json"):
                    matches.append(ref)
            if len(matches) > 1:
                raise ContractError("rq2_event_watch_not_unique")
            (selected if matches else ignored).append(ident)
    return selected, ignored


class RQ2Coordinator:
    def __init__(
        self,
        *,
        knowledge,
        answer_config,
        secrets,
        answer_key,
        context_service,
        transport,
        rules=(),
        mode="synthetic",
        manifest=None,
        authorization=None,
    ):
        self.knowledge, self.answer_config = knowledge, answer_config
        self.dependencies = dict(
            secrets=secrets,
            context_service=context_service,
            transport=transport,
            rules=rules,
        )
        self.answer_key, self.transport = answer_key, transport
        self.used = False
        self.execution = dict(mode=mode, manifest=manifest, authorization=authorization)

    async def run(
        self,
        runtime,
        boundary,
        *,
        main,
        expected_main_run_id,
        snapshot_directory,
        database,
        question_directory,
        output,
        run_id,
        replicate_id,
    ):
        boundary.check(runtime)
        snapshot = validate_main(
            main,
            expected_run_id=expected_main_run_id,
            runtime=runtime,
            configuration=self.knowledge,
        )
        if self.used or type(replicate_id) is not int or replicate_id < 0:
            raise ContractError("rq2_coordinator_reuse_or_replicate_invalid")
        if self.execution["mode"] == "synthetic" and not isinstance(
            self.transport, httpx.MockTransport
        ):
            raise ContractError("synthetic_transport_required")
        configs = [
            self.knowledge.model_copy(
                update={
                    "profile": self.knowledge.profile.model_copy(update={"profile": p})
                }
            )
            for p in PROFILES
        ]
        for config in configs:
            SystemConfiguration(knowledge=config, **self.execution)
            if self.execution["mode"] != "synthetic":
                manifest = stage_preflight(
                    self.execution["manifest"],
                    self.execution["authorization"],
                    "rq2",
                    configuration_sha256=canonical_hash(config),
                )
                if (
                    manifest.run_id != run_id
                    or runtime.episode_id not in manifest.episode_ids
                    or replicate_id >= manifest.statistics.repeats
                    or manifest.main_graphs[runtime.episode_id].snapshot_file.sha256
                    != file_hash(Path(snapshot_directory) / "snapshot.json")
                ):
                    raise ContractError("rq2_run_identity_mismatch")
                stage_preflight(
                    manifest,
                    self.execution["authorization"],
                    "answer",
                    configuration_sha256=canonical_hash(self.answer_config),
                )
        self.used = True
        root = ArtifactStore(output)
        outputs, initial_hash = [], None
        for i, profile in enumerate(PROFILES):
            store = ArtifactStore(root.directory / f"profile-{i}")
            target = "contexthub_s8_rq2_" + uuid4().hex
            dsn = await restore(
                **database,
                snapshot=snapshot,
                directory=snapshot_directory,
                target_database=target,
                output=store.directory / "restore",
            )
            pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=5, init=init_pg_connection
            )
            repo = PgRepository(pool)
            ctx = RequestContext(
                account_id=snapshot.account_id, agent_id=snapshot.agent_id
            )
            config = self.knowledge.model_copy(
                update={
                    "profile": self.knowledge.profile.model_copy(
                        update={"profile": profile}
                    )
                }
            )
            service, answerer = None, None
            try:
                # Core factory validates the copied explicit config before clients.
                service = await create_system(
                    SystemConfiguration(knowledge=config, **self.execution),
                    repository=repo,
                    stage="rq2",
                    **self.dependencies,
                )
                state = await export_account_state(
                    repo, ctx.account_id, agent_id=ctx.agent_id
                )
                current_hash = canonical_sha256(state)
                if initial_hash is not None and current_hash != initial_hash:
                    raise ContractError("rq2_starting_state_mismatch")
                initial_hash = current_hash
                # Verify the real persisted handoff, including graph identity.
                async with repo.session(ctx.account_id) as db:
                    handoff = await service.artifacts.get(db, snapshot.handoff)
                if (
                    handoff.graph != snapshot.graph
                    or handoff.strategy != "selective"
                    or handoff.agent_id != ctx.agent_id
                ):
                    raise ContractError("rq2_restored_handoff_mismatch")
                answerer = AnswerRunner(
                    self.answer_config,
                    repository=repo,
                    account_id="answers-" + uuid4().hex,
                    api_key=self.answer_key,
                    transport=self.transport,
                    **self.execution,
                )
                vault = QuestionVault(question_directory, boundary)
                timeline = ["before_sealed"]
                before = seal_state(
                    store,
                    "before",
                    boundary,
                    state,
                    run_id=run_id,
                    replicate_id=replicate_id,
                    profile=profile,
                    snapshot=snapshot,
                )
                before_answers, before_records = await answerer.answer(
                    vault.release("before", before),
                    before,
                    maintenance_account=ctx.account_id,
                )
                if (
                    await export_account_state(
                        repo, ctx.account_id, agent_id=ctx.agent_id
                    )
                    != state
                ):
                    raise ContractError("answer_modified_maintenance_state")
                store.put("before-answers.json", before_answers)
                timeline.append("before_questions_complete")
                operations, events, ingest_results = [], [], []
                tick = monotonic()
                for source in runtime.sources[
                    boundary.before.ordinal + 1 : boundary.after.ordinal + 1
                ]:
                    changed = await service.ingest_assertion_change(
                        AssertionChangeRequest(
                            request_id=uuid4(),
                            content=json.dumps(
                                [m.model_dump(mode="json") for m in source.messages],
                                ensure_ascii=False,
                            ),
                            handoff=handoff.reference(),
                        ),
                        ctx,
                    )
                    operations.append(changed.operation_id)
                    events.extend(changed.event_ids)
                    ingest_results.append(
                        dict(
                            source_id=source.source_id,
                            source_version=source.source_version,
                            content_sha256=source.content_sha256,
                            result=changed.model_dump(mode="json"),
                        )
                    )
                timeline.append("all_changes_ingested")
                selected, ignored = await bound_events(
                    repo, ctx, events, snapshot.graph
                )
                maintenance = []
                # One sweep after ALL messages, independent of questions or tasks.
                # Each root event is invoked at most once; downstream execution,
                # version/cause checks and all strategies remain in the core.
                for event in selected:
                    result = await service.maintain(
                        KnowledgeMaintainRequest(
                            request_id=uuid4(),
                            event_id=event,
                            graph_id=snapshot.graph.id,
                        ),
                        ctx,
                    )
                    operations.append(result.operation_id)
                    maintenance.append(result.model_dump(mode="json"))
                elapsed = monotonic() - tick
                timeline.append("maintenance_round_complete")
                after_state = await export_account_state(
                    repo, ctx.account_id, agent_id=ctx.agent_id
                )
                after = seal_state(
                    store,
                    "after",
                    boundary,
                    after_state,
                    run_id=run_id,
                    replicate_id=replicate_id,
                    profile=profile,
                    snapshot=snapshot,
                )
                timeline.append("after_sealed")
                after_answers, after_records = await answerer.answer(
                    vault.release("after", after),
                    after,
                    maintenance_account=ctx.account_id,
                )
                if (
                    await export_account_state(
                        repo, ctx.account_id, agent_id=ctx.agent_id
                    )
                    != after_state
                ):
                    raise ContractError("answer_modified_maintenance_state")
                timeline.append("after_questions_complete")
                store.put("after-answers.json", after_answers)
                attempts = await service.execution_records(operations, ctx)
                store.put(
                    "maintenance-attempts.json",
                    [r.model_dump(mode="json") for r in attempts],
                )
                store.put(
                    "answer-attempts.json",
                    [
                        r.model_dump(mode="json")
                        for r in (*before_records, *after_records)
                    ],
                )
                result = dict(
                    status="complete"
                    if all(x["result"]["status"] == "complete" for x in ingest_results)
                    else "pending",
                    run_id=str(run_id),
                    episode_id=runtime.episode_id,
                    replicate_id=replicate_id,
                    profile=profile,
                    database=target,
                    configuration_sha256=service.config_hash,
                    main_snapshot_sha256=canonical_hash(snapshot),
                    initial_state_sha256=initial_hash,
                    before_sha256=before["sha256"],
                    after_sha256=after["sha256"],
                    timeline=timeline,
                    maintenance_rounds=1,
                    core_event_executions=len(maintenance),
                    ignored_unwatched_events=[str(e) for e in ignored],
                    ingestions=ingest_results,
                    maintenance=maintenance,
                    operation_ids=[str(o) for o in operations],
                    maintenance_wall_seconds=elapsed,
                )
                store.put("episode.json", result)
                outputs.append(result)
            except Exception as exc:
                failure = dict(
                    run_id=str(run_id),
                    episode_id=runtime.episode_id,
                    replicate_id=replicate_id,
                    profile=profile,
                    status="error",
                    reason=safe_reason(exc),
                    database=target,
                )
                store.put("failure.json", failure)
                # Preserve partially executed work, including attempts whose
                # operation never returned. Do not invent a successful round.
                store.put(
                    "failure-state.json",
                    await export_account_state(
                        repo, ctx.account_id, agent_id=ctx.agent_id
                    ),
                )
                if answerer is not None:
                    store.put(
                        "failure-answer-state.json",
                        await export_account_state(
                            repo, answerer.account_id, agent_id=ctx.agent_id
                        ),
                    )
                outputs.append(failure)
            finally:
                if answerer is not None:
                    await answerer.close()
                if service is not None:
                    await service.close()
                await pool.close()
        root.put("rq2.json", outputs)
        return outputs
