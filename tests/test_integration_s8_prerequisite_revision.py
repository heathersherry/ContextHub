"""R1/R4 only: real SQL boundaries and dedicated experiment history checks."""

# The imported pytest fixture is deliberately requested by name in each test.
# ruff: noqa: F811

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from test_integration_knowledge_phases import phased as phased, build
from contexthub.models.knowledge import ContractError
from contexthub.models.knowledge_phases import AssertionChangeRequest
from integrations.memebench.patha import database_snapshot as snapshots


def save(name, value):
    path = Path(os.environ["REVIEW_OUT"]) / (name + ".json")
    assert not path.exists()
    path.write_text(
        json.dumps(
            value,
            indent=2,
            default=lambda x: x.model_dump(mode="json")
            if hasattr(x, "model_dump")
            else str(x),
        )
    )


def snapshot_args():
    raw = json.loads(Path(os.environ["S8_TEST_IDENTITY"]).read_text())
    return dict(
        admin_dsn=os.environ["S8_TEST_ADMIN_URL"],
        pg_bin=os.environ["S8_TEST_PG_BIN"],
        identity=snapshots.DatabaseIdentity(
            system_identifier=raw["system_identifier"],
            data_directory=raw["data_directory"],
            port=raw["port"],
            database=raw["db"],
            ordinary_role=raw["role"],
            schema_revision=raw["revision"],
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    [
        "second_write",
        "completion_write",
        "transaction_exit",
        "commit_constraint",
        "committed_ack",
    ],
)
async def test_assertion_receipt_matches_actual_commit(phased, monkeypatch, boundary):
    factory, models, repo, ctx = phased
    service = await factory()
    _, handoff, _, _ = await build(
        service,
        ctx,
        ("A: allowance 500|D: subscription 20|B: uses A for reimbursement",),
    )
    request = AssertionChangeRequest(
        request_id=uuid4(),
        content="Now |A: allowance 400|D: subscription 10",
        handoff=handoff.reference(),
    )
    operation_id = uuid5(
        NAMESPACE_URL, f"knowledge:{ctx.account_id}:{ctx.agent_id}:{request.request_id}"
    )
    completion_id = uuid5(operation_id, "completion")
    original_session, original_seal, original_update = (
        service._session,
        service.artifacts.seal,
        service.context_service.update,
    )
    fired, writes = False, 0

    async def update(*args, **kwargs):
        nonlocal writes, fired
        writes += 1
        if boundary == "second_write" and writes == 2:
            fired = True
            raise RuntimeError("synthetic second write failure")
        return await original_update(*args, **kwargs)

    async def seal(db, obj):
        nonlocal fired
        if boundary == "completion_write" and obj.id == completion_id and not fired:
            fired = True
            raise RuntimeError("synthetic completion write failure")
        return await original_seal(db, obj)

    @asynccontextmanager
    async def session(account):
        nonlocal fired
        raise_after = False
        async with original_session(account) as db:
            yield db
            if (
                boundary in ("transaction_exit", "commit_constraint", "committed_ack")
                and not fired
            ):
                exists = await db.fetchval(
                    "SELECT 1 FROM knowledge_artifacts WHERE account_id=$1 AND id=$2",
                    account,
                    completion_id,
                )
                if exists:
                    fired = True
                    if boundary == "transaction_exit":
                        # The actual completion and carrier writes exist inside
                        # the transaction, then its __aexit__ rolls them back.
                        raise RuntimeError("synthetic transaction exit failure")
                    if boundary == "commit_constraint":
                        # Ordinary-role temporary constraint injects an actual
                        # PostgreSQL COMMIT failure, after all public writes.
                        # It rolls back with this transaction; no business row
                        # is hand-written and no persistent schema is changed.
                        await db.execute(
                            "CREATE TEMP TABLE s8_commit_fault (id int UNIQUE DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
                        )
                        await db.execute("INSERT INTO s8_commit_fault VALUES (1),(1)")
                    else:
                        raise_after = True
        if raise_after:
            # COMMIT succeeded; only the acknowledgement was lost.
            raise RuntimeError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(service.context_service, "update", update)
    monkeypatch.setattr(service.artifacts, "seal", seal)
    monkeypatch.setattr(service, "_session", session)
    result = await service.ingest_assertion_change(request, ctx)
    calls = len(models.calls)
    duplicate = await service.ingest_assertion_change(request, ctx)
    async with repo.session(ctx.account_id) as db:
        nodes = [
            dict(r)
            for r in await db.fetch(
                "SELECT id,version,l2_content,validity_status FROM contexts WHERE account_id=$1 ORDER BY id",
                ctx.account_id,
            )
        ]
        events = [
            dict(r)
            for r in await db.fetch(
                "SELECT event_id,context_id,source_version FROM change_events WHERE account_id=$1 AND change_type='modified' ORDER BY event_id",
                ctx.account_id,
            )
        ]
        causes = [
            dict(r)
            for r in await db.fetch(
                "SELECT i.* FROM context_invalidations i JOIN contexts c ON c.id=i.context_id WHERE c.account_id=$1",
                ctx.account_id,
            )
        ]
        receipt = await db.fetchval(
            "SELECT payload FROM knowledge_artifacts WHERE account_id=$1 AND id=$2",
            ctx.account_id,
            completion_id,
        )
        proposals = await db.fetchval(
            "SELECT count(*) FROM knowledge_artifacts WHERE account_id=$1 AND kind='assertion_change'",
            ctx.account_id,
        )
    save(
        "r1-" + boundary,
        dict(
            result=result,
            duplicate=duplicate,
            nodes=nodes,
            events=events,
            causes=causes,
            receipt=receipt,
            calls=models.calls,
        ),
    )
    assert fired and duplicate == result and len(models.calls) == calls
    assert proposals == 1
    assert (
        next(n for n in nodes if n["id"] == result.source.context_id)["l2_content"]
        == request.content
    )
    if boundary == "committed_ack":
        assert result.status == "complete" and result.location_status == "located"
        assert (
            set(result.event_ids) == {e["event_id"] for e in events}
            and len(events) == 2
        )
        assert len([n for n in nodes if n["version"] == 2]) == 2 and causes
    else:
        assert result.status == "pending" and result.location_status == "pending"
        assert not result.event_ids and not events and not causes
        assert all(n["version"] == 1 and n["validity_status"] == "fresh" for n in nodes)
        assert (
            receipt["result"]["status"] == "pending"
            and receipt["result"]["event_ids"] == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "full-history"])
async def test_capture_rejects_other_graph_history(phased, tmp_path, strategy):
    factory, models, repo, ctx = phased
    other = await factory(strategy)
    _, foreign, _, _ = await build(
        other, ctx, ("OTHER-STRATEGY-ONLY: generated secret conclusion",)
    )
    service = await factory()
    _, handoff, _, _ = await build(service, ctx)
    assert not set(handoff.sources) & set(foreign.sources)
    count = len(models.calls)
    try:
        with pytest.raises(ContractError, match="snapshot_foreign_history"):
            await snapshots.capture(
                **snapshot_args(),
                directory=tmp_path / "rejected",
                run_id=str(uuid4()),
                handoff=handoff,
                configuration_sha256=service.config_hash,
            )
    finally:
        save(
            "r4-capture-" + strategy,
            dict(handoff=handoff, foreign=foreign, calls=models.calls),
        )
    assert not (tmp_path / "rejected/database.dump").exists()
    assert len(models.calls) == count


def make_runner(service, repo, models):
    from examples.knowledge_fixture import secrets
    from integrations.memebench.patha.public_rq1_runner import (
        PublicRQ1Runner,
        PublicRQ1Configuration,
    )

    selective = service.config.model_dump(mode="json")
    full = json.loads(json.dumps(selective))
    full["discovery"]["search"]["strategy"] = "full-history"
    return PublicRQ1Runner(
        PublicRQ1Configuration.model_validate(
            dict(
                selective=dict(mode="synthetic", knowledge=selective),
                full_history=dict(mode="synthetic", knowledge=full),
            )
        ),
        repository=repo,
        context_service=service.context_service,
        secrets=secrets(),
        transport=models.transport,
    )


def allowed_input():
    from integrations.memebench.patha.schemas import MaintenanceInput, canonical_sha256
    from test_memebench_patha_rq1_runner import runtime

    payload = runtime().model_dump(mode="json")
    for source, text in zip(
        payload["sources"],
        ("A: allowance 500|X: legitimate filler", "B: uses A for reimbursement"),
    ):
        source["messages"][0]["content"] = text
        source["content_sha256"] = canonical_sha256(source["messages"])
    payload["input_sha256"] = canonical_sha256(
        {k: v for k, v in payload.items() if k != "input_sha256"}
    )
    return MaintenanceInput.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "full-history", "repeated_run"])
async def test_rq1_rejects_used_account_before_client_construction(
    phased, monkeypatch, strategy
):
    import integrations.memebench.patha.public_rq1_runner as runner_module

    factory, models, repo, ctx = phased
    service = await factory()
    runner = make_runner(service, repo, models)
    kwargs = dict(
        account_id=ctx.account_id,
        agent_id=ctx.agent_id,
        run_id=uuid4(),
        replicate_id=1,
        cutoff_ordinal=1,
    )
    if strategy == "repeated_run":
        first = await runner.run(allowed_input(), **kwargs)
        assert len(first.handoffs) == 2
    else:
        previous = await factory(strategy)
        await build(previous, ctx)
    calls = len(models.calls)
    async with repo.session(ctx.account_id) as db:
        before = await snapshots.table_hashes(db)
    constructed = []

    async def forbidden(*a, **kw):
        constructed.append(True)
        raise AssertionError("should reject before constructing a client")

    monkeypatch.setattr(runner_module, "create_system", forbidden)
    # New run and new replicate also cannot reuse another run's account.
    for run_id, replicate_id in ((kwargs["run_id"], 1), (uuid4(), 2)):
        with pytest.raises(ContractError, match="rq1_run_account_not_empty"):
            await runner.run(
                allowed_input(),
                **{**kwargs, "run_id": run_id, "replicate_id": replicate_id},
            )
    async with repo.session(ctx.account_id) as db:
        after = await snapshots.table_hashes(db)
    save("r4-entry-" + strategy, dict(before=before, after=after, calls=models.calls))
    assert not constructed and len(models.calls) == calls and before == after


@pytest.mark.asyncio
async def test_capture_rejects_second_graph_with_identical_contexts(phased, tmp_path):
    from contexthub.models.knowledge_phases import FinalizeGraphRequest

    factory, models, repo, ctx = phased
    service = await factory()
    _, handoff, prepared, reports = await build(service, ctx)
    async with repo.session(ctx.account_id) as db:
        before_ids = await db.fetch("SELECT id,version FROM contexts ORDER BY id")
    second = await service.finalize_graph(
        FinalizeGraphRequest(request_id=uuid4(), prepared=prepared, reports=reports),
        ctx,
    )
    assert second.status == "complete"
    async with repo.session(ctx.account_id) as db:
        assert before_ids == await db.fetch(
            "SELECT id,version FROM contexts ORDER BY id"
        )
    with pytest.raises(ContractError, match="snapshot_foreign_discovery"):
        await snapshots.capture(
            **snapshot_args(),
            directory=tmp_path / "rejected",
            run_id=str(uuid4()),
            handoff=handoff,
            configuration_sha256=service.config_hash,
        )
    assert not (tmp_path / "rejected/database.dump").exists()
    save("r4-same-contexts", dict(handoff=handoff, second=second))


@pytest.mark.asyncio
async def test_rq1_capture_rechecks_after_entry(phased, tmp_path):
    factory, models, repo, ctx = phased
    service = await factory()
    runner = make_runner(service, repo, models)

    async def capture_with_foreign_graph(handoff, config_hash):
        # A real graph arrives after the clean-account entry check. The final
        # exported snapshot must still reject it, not merely trust preflight.
        other = await factory("full-history")
        await build(other, ctx, ("OTHER-STRATEGY-ONLY: generated conclusion",))
        return await snapshots.capture(
            **snapshot_args(),
            directory=tmp_path / "rejected",
            run_id=str(run_id),
            handoff=handoff,
            configuration_sha256=config_hash,
        )

    run_id = uuid4()
    with pytest.raises(ContractError, match="snapshot_foreign_history"):
        await runner.run(
            allowed_input(),
            account_id=ctx.account_id,
            agent_id=ctx.agent_id,
            run_id=run_id,
            replicate_id=0,
            cutoff_ordinal=1,
            seal_selective=capture_with_foreign_graph,
        )
    assert not (tmp_path / "rejected/database.dump").exists()
    save("r4-entry-to-capture", dict(calls=models.calls))


@pytest.mark.asyncio
async def test_restored_profiles_keep_only_run_history_and_all_filler(phased, tmp_path):
    import asyncpg
    from contexthub.db.codecs import init_pg_connection
    from contexthub.db.repository import PgRepository
    from contexthub.models.request import RequestContext
    from contexthub.models.knowledge_public import KnowledgeMaintainRequest
    from contexthub.services.knowledge_factory import create_knowledge_service
    from examples.knowledge_fixture import configuration, secrets

    factory, models, repo, ctx = phased
    service = await factory()
    foreign_ctx = RequestContext(
        account_id="other-" + str(uuid4()), agent_id=ctx.agent_id
    )
    foreign = await factory("full-history")
    marker = "OTHER-STRATEGY-ONLY: generated secret conclusion"
    await build(foreign, foreign_ctx, (marker,))
    before, handoff, _, _ = await build(service, ctx)
    kwargs = snapshot_args()
    frozen = await snapshots.capture(
        **kwargs,
        directory=tmp_path / "frozen",
        run_id=str(uuid4()),
        handoff=handoff,
        configuration_sha256=service.config_hash,
    )
    allowed_ids = {cv.context_id for cv in handoff.sources}
    async with repo.session(ctx.account_id) as db:
        graph = await service.artifacts.get(db, handoff.graph)
        allowed_ids.update(cv.context_id for cv in graph.nodes)
    for index, profile in enumerate(
        ("local/base-only", "full/strong-only", "adaptive")
    ):
        target = "contexthub_s8_revision_" + uuid4().hex
        dsn = await snapshots.restore(
            **kwargs,
            snapshot=frozen,
            directory=tmp_path / "frozen",
            target_database=target,
            output=tmp_path / f"restore-{index}",
        )
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=5, init=init_pg_connection
        )
        config = service.config.model_copy(
            update={"profile": configuration(profile).profile}
        )
        copied = await create_knowledge_service(
            PgRepository(pool),
            config,
            secrets(),
            context_service=service.context_service,
            transport=models.transport,
        )
        try:
            assert await copied.get_graph_handoff(before.operation_id, ctx) == handoff
            raw = "Now |A: allowance 400|Please retain this filler message."
            changed = await copied.ingest_assertion_change(
                AssertionChangeRequest(
                    request_id=uuid4(), content=raw, handoff=handoff.reference()
                ),
                ctx,
            )
            assert changed.location_status == "located"
            start = len(models.calls)
            result = await copied.maintain(
                KnowledgeMaintainRequest(
                    request_id=uuid4(),
                    event_id=changed.event_ids[0],
                    graph_id=handoff.graph.id,
                ),
                ctx,
            )
            calls = models.calls[start:]
            assert any(c["model"] == "verifier" for c in calls)
            assert any(c["model"] in ("base", "strong") for c in calls)
            assert all(marker not in json.dumps(c) for c in calls)
            async with copied.repo.session(ctx.account_id) as db:
                frames = [
                    r["payload"]
                    for r in await db.fetch(
                        "SELECT payload FROM knowledge_artifacts WHERE account_id=$1 AND kind='maintenance_frame'",
                        ctx.account_id,
                    )
                ]
                history = [
                    dict(r)
                    for r in await db.fetch(
                        "SELECT v.context_id,v.version,v.l2_content FROM context_versions v JOIN contexts c ON c.id=v.context_id WHERE c.account_id=$1",
                        ctx.account_id,
                    )
                ]
            save(
                f"r4-restored-{index}",
                dict(
                    snapshot=frozen,
                    target=target,
                    profile=profile,
                    result=result,
                    calls=calls,
                    frames=frames,
                    history=history,
                ),
            )
            assert {r["context_id"] for r in history} == allowed_ids | {
                changed.source.context_id
            }
            assert any(r["l2_content"] == "X: unrelated fact" for r in history)
            assert any(r["l2_content"] == raw for r in history)
            assert marker not in json.dumps(frames)
            if profile == "full/strong-only":
                strong = [c for c in calls if c["model"] == "strong"]
                verifier = [c for c in calls if c["model"] == "verifier"]
                assert strong and verifier
                # Full-history provider AND verifier must actually see filler.
                assert all(
                    "X: unrelated fact" in json.dumps(c) and raw in json.dumps(c)
                    for c in strong + verifier
                )
            assert (
                len({w.target_id for w in result.works if w.status == "updated"}) == 2
            )
        finally:
            await copied.close()
            await pool.close()
