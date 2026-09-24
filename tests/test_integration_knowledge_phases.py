"""S8 prerequisite contracts through public factory, S3–S7 and a fresh database.

Opt-in DSN/instance identity only. Never uses conftest's db_pool/clean_db.
"""

import json
import os
from pathlib import Path
from uuid import uuid4
import asyncpg
import httpx
import pytest
import pytest_asyncio
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.models.request import RequestContext
from contexthub.models.knowledge import ContractError
from contexthub.models.knowledge_public import KnowledgeMaintainRequest
from contexthub.models.knowledge_phases import (
    PrepareRequest,
    DiscoverPreparedRequest,
    FinalizeGraphRequest,
    AssertionChangeRequest,
)
from contexthub.services.knowledge_factory import create_knowledge_service
from contexthub.services.context_service import ContextService
from contexthub.services.acl_service import ACLService
from contexthub.services.masking_service import MaskingService
from contexthub.store.context_store import ContextStore
from examples.knowledge_fixture import configuration, secrets, SyntheticModels, response


class Models(SyntheticModels):
    def __init__(self):
        super().__init__()
        self.invalid_target = None
        self.locate_all = False

    def send(self, request):
        body = json.loads(request.content)
        if body["model"] == "extract":
            prompt = body["messages"][-1]["content"]
            sources = json.loads(prompt[prompt.index("[") :])
            if all(s["text"].startswith("[") for s in sources):
                self.calls.append(body)
                props = []
                for source in sources:
                    for message in json.loads(source["text"]):
                        for sentence in message["content"].split("|"):
                            start = source["text"].index(sentence)
                            props.append(
                                dict(
                                    text=sentence,
                                    evidence=[
                                        dict(
                                            source_id=source["snapshot"]["id"],
                                            start=start,
                                            end=start + len(sentence),
                                            excerpt=sentence,
                                            provenance=[
                                                "synthetic original JSON message"
                                            ],
                                        )
                                    ],
                                )
                            )
                return response(dict(propositions=props))
        if body["model"] != "locator":
            return super().send(request)
        self.calls.append(body)
        prompt = body["messages"][-1]["content"]
        payload = json.loads(prompt[prompt.index("{") :])
        assert payload["contract"] == "direct-assertion-change-v1"
        raw = payload["change"]

        def span(source, text):
            start = source["text"].index(text)
            return dict(
                source_id=source["snapshot_id"],
                start=start,
                end=start + len(text),
                excerpt=text,
                provenance=["synthetic raw statement"],
            )

        matches = []
        # Deterministic fixture grammar only; no benchmark labels or IDs.
        for old in payload["candidates"]:
            subject = old["text"].split(":")[0]
            new = next(
                (
                    s.strip()
                    for s in raw["text"].split("|")
                    if s.strip().startswith(subject + ":")
                ),
                None,
            )
            if new is None or new == old["text"]:
                continue
            ev = old["assertions"][0]
            original = dict(
                source_id=ev["source"]["id"],
                start=ev["start"],
                end=ev["end"],
                excerpt=ev["excerpt"],
                provenance=["original assertion"],
            )
            matches.append(
                dict(
                    carrier_id=self.invalid_target or old["context_id"],
                    version=old["version"],
                    old=span(old, old["text"]),
                    original_assertion=original,
                    replacement=span(raw, new),
                    identity_basis=span(raw, new),
                    temporal_basis=span(raw, raw["text"]),
                    relation="same_subject_same_attribute_direct_assertion_replacement",
                )
            )
        return response(
            dict(
                status="located" if matches else "unrelated",
                matches=matches,
                reason="synthetic direct change",
            )
        )


@pytest_asyncio.fixture
async def phased():
    dsn = os.environ["S8_TEST_DATABASE_URL"]
    identity = json.loads(Path(os.environ["S8_TEST_IDENTITY"]).read_text())
    assert identity["db"].startswith("contexthub_s8_")
    assert identity["role"] == "s8_app"
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, init=init_pg_connection
    )
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT current_database()") == identity["db"]
        assert await c.fetchval("SELECT current_user") == identity["role"]
        assert await c.fetchval("SHOW port") == str(identity["port"])
        assert await c.fetchval("SHOW server_encoding") == "UTF8"
        assert await c.fetchval("SELECT version_num FROM alembic_version") == "011"
        assert await c.fetchval("SELECT current_schema()") == "public"
        role = await c.fetchrow(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
        assert not role["rolsuper"] and not role["rolbypassrls"]
    repo = PgRepository(pool)
    models = Models()
    services = []

    async def factory(strategy="selective", profile="adaptive"):
        config = configuration(profile).model_dump(mode="json")
        config["discovery"]["search"]["strategy"] = strategy
        config["discovery"]["search"]["initial_sources"] = (
            2  # explicit synthetic two-batch fixture
        )
        from contexthub.knowledge_config import KnowledgeConfig

        service = await create_knowledge_service(
            repo,
            KnowledgeConfig.model_validate(config),
            secrets(),
            context_service=ContextService(
                ContextStore(ACLService(), MaskingService()), ACLService()
            ),
            transport=models.transport,
        )
        services.append(service)
        return service

    try:
        yield (
            factory,
            models,
            repo,
            RequestContext(account_id="s8-" + str(uuid4()), agent_id="reader"),
        )
    finally:
        for service in services:
            await service.close()
        await pool.close()


async def build(
    service,
    ctx,
    texts=(
        "A: allowance 500|X: unrelated fact",
        "B: uses A for reimbursement|C: uses B in report",
    ),
):
    prepared, reports, previous = [], [], None
    for text in texts:
        p = await service.prepare(
            PrepareRequest(request_id=uuid4(), content=text, previous=previous), ctx
        )
        assert p.status == "complete", p
        previous = p.prepared
        prepared.append(previous)
        d = await service.discover_prepared(
            DiscoverPreparedRequest(request_id=uuid4(), prepared=previous), ctx
        )
        assert d.status == "complete", d
        assert not d.maintenance_ready and not d.watch_ids
        async with service.repo.session(ctx.account_id) as db:
            report = await service._get(db, d.report_id, "discovery_report")
        reports.append(report.reference())
    body = FinalizeGraphRequest(
        request_id=uuid4(), reports=tuple(reports), prepared=tuple(prepared)
    )
    result = await service.finalize_graph(body, ctx)
    assert result.maintenance_ready, result
    async with service.repo.session(ctx.account_id) as db:
        handoff = await service._get(db, result.report_id, "graph_handoff")
    return result, handoff, prepared, reports


@pytest.mark.asyncio
async def test_shared_preparation_and_complete_graph(phased):
    factory, models, repo, ctx = phased
    selective = await factory()
    before, handoff, prepared, reports = await build(selective, ctx)
    assert len(before.nodes) == 4
    assert len(handoff.reports) == 2
    extraction_calls = sum(c["model"] == "extract" for c in models.calls)
    full = await factory("full-history")
    full_reports = []
    for ref in prepared:
        result = await full.discover_prepared(
            DiscoverPreparedRequest(request_id=uuid4(), prepared=ref), ctx
        )
        assert result.status == "complete", result
        async with repo.session(ctx.account_id) as db:
            report = await full._get(db, result.report_id, "discovery_report")
            original = await full.artifacts.get(db, reports[len(full_reports)])
        assert report.shared_extraction == original.shared_extraction
        assert report.history_sha256 == original.history_sha256
        full_reports.append(report.reference())
    assert sum(c["model"] == "extract" for c in models.calls) == extraction_calls == 2
    # Missing first batch cannot masquerade as the full chain.
    with pytest.raises(ContractError, match="handoff_history_not_complete"):
        await selective.finalize_graph(
            FinalizeGraphRequest(
                request_id=uuid4(), reports=(reports[-1],), prepared=(prepared[-1],)
            ),
            ctx,
        )
    forged = prepared[0].model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ContractError, match="artifact_hash_mismatch"):
        await selective.discover_prepared(
            DiscoverPreparedRequest(request_id=uuid4(), prepared=forged), ctx
        )
    async with repo.session(ctx.account_id) as db:
        watches = await db.fetch(
            "SELECT payload FROM knowledge_artifacts WHERE kind='maintenance_watch'"
        )
    assert watches and all(
        w["payload"]["graph"]["id"] == str(handoff.graph.id) for w in watches
    )


@pytest.mark.asyncio
async def test_partial_raw_change_then_filler_then_one_maintenance(phased):
    factory, models, repo, ctx = phased
    service = await factory()
    before, handoff, _, _ = await build(service, ctx)
    raw = "Now the allowance changed. |A: allowance 400|Please delete the old shopping reminder."
    changed = await service.ingest_assertion_change(
        AssertionChangeRequest(
            request_id=uuid4(), content=raw, handoff=handoff.reference()
        ),
        ctx,
    )
    assert changed.location_status == "located", changed
    assert len(changed.event_ids) == 1
    async with repo.session(ctx.account_id) as db:
        original = await db.fetchval(
            "SELECT l2_content FROM contexts WHERE id=$1", handoff.sources[0].context_id
        )
        saved = await db.fetchval(
            "SELECT l2_content FROM contexts WHERE id=$1", changed.source.context_id
        )
        nodes = await db.fetch(
            "SELECT id,l2_content,version,validity_status FROM contexts WHERE id=ANY($1::uuid[])",
            [n.context_id for n in before.nodes],
        )
    assert original == "A: allowance 500|X: unrelated fact"
    assert saved == raw
    assert [
        (n["version"], n["l2_content"])
        for n in nodes
        if n["l2_content"].startswith("A:")
    ] == [(2, "A: allowance 400")]
    assert [
        (n["version"], n["validity_status"])
        for n in nodes
        if n["l2_content"].startswith("X:")
    ] == [(1, "fresh")]
    assert all(
        n["validity_status"] == "stale"
        for n in nodes
        if n["l2_content"].startswith(("B:", "C:"))
    )
    filler = await service.ingest_assertion_change(
        AssertionChangeRequest(
            request_id=uuid4(),
            content="We discussed the weather.",
            handoff=handoff.reference(),
        ),
        ctx,
    )
    assert filler.location_status == "unrelated" and not filler.event_ids, filler
    assert not any(c["model"] in ("base", "strong", "verifier") for c in models.calls)
    maintained = await service.maintain(
        KnowledgeMaintainRequest(
            request_id=uuid4(), event_id=changed.event_ids[0], graph_id=handoff.graph.id
        ),
        ctx,
    )
    assert maintained.status == "complete", maintained
    successful = [w for w in maintained.works if w.status == "updated"]
    assert len(successful) == 2 and len({w.target_id for w in successful}) == 2, (
        maintained
    )
    assert all(n.fresh for n in maintained.nodes), maintained
    async with repo.session(ctx.account_id) as db:
        assert (
            await db.fetchval(
                "SELECT count(DISTINCT payload->>'execution_id') FROM knowledge_artifacts WHERE kind='maintenance_frame'"
            )
            == 1
        )
    count = len(models.calls)
    queried = await service.get_maintenance_result(maintained.operation_id, ctx)
    assert queried == maintained and len(models.calls) == count


@pytest.mark.asyncio
async def test_multiple_assertions_atomic_and_derived_target_rejected(phased):
    factory, models, repo, ctx = phased
    service = await factory()
    before, handoff, _, _ = await build(
        service,
        ctx,
        ("A: allowance 500|D: subscription 20|B: uses A for reimbursement",),
    )
    models.invalid_target = str(
        next(m.node.context_id for m in handoff.mappings if m.text.startswith("B:"))
    )
    failed = await service.ingest_assertion_change(
        AssertionChangeRequest(
            request_id=uuid4(),
            content="Now |A: allowance 400|D: subscription 10",
            handoff=handoff.reference(),
        ),
        ctx,
    )
    assert failed.location_status == "pending" and not failed.event_ids, failed
    async with repo.session(ctx.account_id) as db:
        assert await db.fetchval("SELECT max(version) FROM contexts") == 1
    models.invalid_target = None
    result = await service.ingest_assertion_change(
        AssertionChangeRequest(
            request_id=uuid4(),
            content="Now |A: allowance 400|D: subscription 10",
            handoff=handoff.reference(),
        ),
        ctx,
    )
    assert result.location_status == "located" and len(result.event_ids) == 2, result
    # This tests ingestion, NOT a claim that two independent events can be
    # maintained as one round. No maintain_batch API is added.


@pytest.mark.asyncio
async def test_same_graph_three_database_copies(phased, tmp_path):
    from integrations.memebench.patha.database_snapshot import (
        DatabaseIdentity,
        capture,
        restore,
    )

    factory, models, repo, ctx = phased
    service = await factory()
    before, handoff, _, _ = await build(service, ctx)
    saved = await service.get_graph_handoff(before.operation_id, ctx)
    assert saved.reference() == handoff.reference()
    raw_identity = json.loads(Path(os.environ["S8_TEST_IDENTITY"]).read_text())
    identity = DatabaseIdentity(
        system_identifier=raw_identity["system_identifier"],
        data_directory=raw_identity["data_directory"],
        port=raw_identity["port"],
        database=raw_identity["db"],
        ordinary_role=raw_identity["role"],
        schema_revision="011",
    )
    admin = os.environ["S8_TEST_ADMIN_URL"]
    pg_bin = os.environ["S8_TEST_PG_BIN"]
    snapshot = await capture(
        admin_dsn=admin,
        identity=identity,
        directory=tmp_path / "frozen",
        pg_bin=pg_bin,
        run_id="synthetic-" + str(uuid4()),
        handoff=handoff,
        configuration_sha256=service.config_hash,
    )
    copies = []
    try:
        for i, profile in enumerate(
            ("local/base-only", "full/strong-only", "adaptive")
        ):
            target = "contexthub_s8_copy_" + uuid4().hex
            dsn = await restore(
                admin_dsn=admin,
                identity=identity,
                snapshot=snapshot,
                directory=tmp_path / "frozen",
                target_database=target,
                pg_bin=pg_bin,
                output=tmp_path / f"copy-{i}",
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
                context_service=ContextService(
                    ContextStore(ACLService(), MaskingService()), ACLService()
                ),
                transport=models.transport,
            )
            copies.append((copied, pool))
            read = await copied.get_graph_handoff(before.operation_id, ctx)
            assert read == handoff
            state = await copied.get_maintenance_result(before.operation_id, ctx)
            assert state == before
        # Every strategy starts from the same preserved IDs/hash/ACL/watch/state.
        # The first branch mutation cannot affect either untouched copy or source.
        for i, (copied, _) in enumerate(copies):
            changed = await copied.ingest_assertion_change(
                AssertionChangeRequest(
                    request_id=uuid4(),
                    content="Now |A: allowance 400",
                    handoff=handoff.reference(),
                ),
                ctx,
            )
            assert changed.location_status == "located", changed
            for untouched, _ in copies[i + 1 :]:
                assert (
                    await untouched.get_maintenance_result(before.operation_id, ctx)
                    == before
                )
            maintained = await copied.maintain(
                KnowledgeMaintainRequest(
                    request_id=uuid4(),
                    event_id=changed.event_ids[0],
                    graph_id=handoff.graph.id,
                ),
                ctx,
            )
            assert (
                len({w.target_id for w in maintained.works if w.status == "updated"})
                == 2
            ), maintained
        assert await service.get_maintenance_result(before.operation_id, ctx) == before
    finally:
        for copied, pool in copies:
            await copied.close()
            await pool.close()
    # Copies are preserved; no DROP/TRUNCATE or connection termination.


@pytest.mark.asyncio
async def test_prepared_acl_idempotency_version_and_cancellation(phased):
    import asyncio
    from contexthub.errors import ForbiddenError, ConflictError
    from contexthub.models.context import UpdateContextRequest

    factory, models, repo, ctx = phased
    service = await factory()
    body = PrepareRequest(request_id=uuid4(), content="A: original|X: stays")
    first = await service.prepare(body, ctx)
    calls = len(models.calls)
    again = await service.prepare(body, ctx)
    assert first == again and len(models.calls) == calls
    with pytest.raises(ForbiddenError):
        await service.discover_prepared(
            DiscoverPreparedRequest(request_id=uuid4(), prepared=first.prepared),
            RequestContext(account_id=ctx.account_id, agent_id="outsider"),
        )
    async with repo.session(ctx.account_id) as db:
        await service.context_service.update(
            db,
            first.result.source.uri,
            UpdateContextRequest(l2_content="A: changed"),
            RequestContext(
                account_id=ctx.account_id, agent_id=ctx.agent_id, expected_version=1
            ),
        )
    with pytest.raises(ConflictError, match="prepared_source_version_drift"):
        await service.discover_prepared(
            DiscoverPreparedRequest(request_id=uuid4(), prepared=first.prepared), ctx
        )
    assert len(models.calls) == calls
    # Hold only the offline transport, while the real public operation is saved.
    arrived = asyncio.Event()

    async def blocked(request):
        arrived.set()
        await asyncio.Event().wait()

    service.extractor.client._client._transport = httpx.MockTransport(blocked)
    cancelled = PrepareRequest(request_id=uuid4(), content="A: cancelled")
    task = asyncio.create_task(service.prepare(cancelled, ctx))
    await asyncio.wait_for(arrived.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await service.prepare(cancelled, ctx)
    assert result.status == "error" and result.result.reason == "request_cancelled"
    assert result.prepared is None


@pytest.mark.asyncio
async def test_public_rq1_input_isolation_actual_calls_and_state(phased, tmp_path):
    import re
    from integrations.memebench.patha.public_rq1_runner import (
        PublicRQ1Runner,
        PublicRQ1Configuration,
    )
    from integrations.memebench.patha.database_snapshot import DatabaseIdentity, capture
    from integrations.memebench.patha.schemas import MaintenanceInput, canonical_sha256
    from test_memebench_patha_rq1_runner import runtime

    factory, models, repo, _ = phased
    payload = runtime().model_dump(mode="json")
    for source, text in zip(
        payload["sources"],
        (
            "A: allowance 500|X: unaffected",
            "B: uses A for reimbursement|C: uses B in report",
        ),
    ):
        source["messages"][0]["content"] = text
        source["content_sha256"] = canonical_sha256(source["messages"])
    payload["input_sha256"] = canonical_sha256(
        {k: v for k, v in payload.items() if k != "input_sha256"}
    )
    allowed = MaintenanceInput.model_validate(payload)
    cfg = configuration().model_dump(mode="json")
    cfg["discovery"]["search"].update(strategy="selective", initial_sources=2)
    other = json.loads(json.dumps(cfg))
    other["discovery"]["search"]["strategy"] = "full-history"
    config = PublicRQ1Configuration.model_validate(
        dict(
            selective=dict(mode="synthetic", knowledge=cfg),
            full_history=dict(mode="synthetic", knowledge=other),
        )
    )
    raw_identity = json.loads(Path(os.environ["S8_TEST_IDENTITY"]).read_text())
    identity = DatabaseIdentity(
        system_identifier=raw_identity["system_identifier"],
        data_directory=raw_identity["data_directory"],
        port=raw_identity["port"],
        database=raw_identity["db"],
        ordinary_role=raw_identity["role"],
        schema_revision="011",
    )
    outputs = []
    for i, labels in enumerate(
        (
            dict(gold="wrong", hop=19, task_type="Del", paths=["x"]),
            dict(gold="different", hop=0, task_type="Abs", paths=["y", "z"]),
        )
    ):
        account = "s8-" + str(uuid4())
        run_id = uuid4()
        start = len(models.calls)
        runner = PublicRQ1Runner(
            config,
            repository=repo,
            context_service=ContextService(
                ContextStore(ACLService(), MaskingService()), ACLService()
            ),
            secrets=secrets(),
            transport=models.transport,
        )

        async def seal(handoff, config_hash):
            # At this boundary the full-history arm MUST NOT exist yet.
            async with repo.session(account) as db:
                assert (
                    await db.fetchval(
                        "SELECT count(*) FROM knowledge_artifacts WHERE kind='discovery_report' AND payload->>'strategy'='full-history'"
                    )
                    == 0
                )
            return await capture(
                admin_dsn=os.environ["S8_TEST_ADMIN_URL"],
                identity=identity,
                directory=tmp_path / f"seal-{i}",
                pg_bin=os.environ["S8_TEST_PG_BIN"],
                run_id=str(run_id),
                handoff=handoff,
                configuration_sha256=config_hash,
            )

        # Privileged labels live only in this offline test harness, never in the
        # runtime argument, closure used by core, routing, or persisted trace.
        result = await runner.run(
            allowed,
            account_id=account,
            agent_id="reader",
            run_id=run_id,
            replicate_id=0,
            cutoff_ordinal=1,
            seal_selective=seal,
        )
        assert len(result.handoffs) == 2 and len(result.shared_extractions) == 2
        assert sum(c["model"] == "extract" for c in models.calls[start:]) == 2
        async with repo.session(account) as db:
            reports = await db.fetch(
                "SELECT payload FROM knowledge_artifacts WHERE kind='discovery_report'"
            )
            assert len(reports) == 4
            for shared in result.shared_extractions:
                assert (
                    sum(
                        r["payload"]["shared_extraction"]["id"] == str(shared.id)
                        for r in reports
                    )
                    == 2
                )
            rows = await db.fetch(
                "SELECT l2_content,version,status,validity_status FROM contexts ORDER BY l2_content"
            )
            graph_edges = []
            for ref in result.handoffs:
                handoff = await service_artifact(db, ref)
                graph = await service_artifact(db, handoff.graph)
                names = {m.node: m.text for m in handoff.mappings}
                graph_edges.append(
                    sorted(
                        (names[e.upstream], names[e.downstream], e.semantic_basis)
                        for e in graph.edges
                    )
                )
        calls = models.calls[start:]
        artifact = dict(
            labels=labels,
            calls=calls,
            state=[dict(r) for r in rows],
            edges=graph_edges,
            records=[r.model_dump(mode="json") for r in result.records],
        )
        (tmp_path / f"raw-trace-{i}.json").write_text(json.dumps(artifact, indent=2))
        assert len(
            {(r.identity.call_id, r.attempt_no, r.phase) for r in result.records}
        ) == len(result.records)
        assert all(r.identity.account_id == account for r in result.records)
        physical = [
            r
            for r in result.records
            if r.phase == "finished" and r.requested_model == "extract"
        ]
        assert len(physical) == 2
        assert all(
            r.identity.execution_id in result.shared_operations for r in physical
        )
        logical = [
            [
                r
                for r in physical
                if r.identity.execution_id in (*result.shared_operations, *arm)
            ]
            for arm in result.arm_operations
        ]
        assert [len(rows) for rows in logical] == [2, 2]
        outputs.append((calls, artifact["state"], graph_edges))

    def normalize(value):
        if isinstance(value, dict):
            return {
                k: normalize(v)
                for k, v in value.items()
                if k
                not in (
                    "ingested_at",
                    "cutoff_at",
                    "account_id",
                    "sha256",
                    "evidence_sha256",
                )
            }
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, str):
            # Prompts embed JSON; parse before applying the same field rules.
            marker = next((j for j, ch in enumerate(value) if ch in "[{"), None)
            if marker is not None:
                try:
                    return (value[:marker], normalize(json.loads(value[marker:])))
                except ValueError:
                    pass
            value = re.sub(
                r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", "<uuid>", value
            )
            return re.sub(r"[0-9a-f]{64}", "<reference-hash>", value)
        return value

    assert normalize(outputs[0][0]) == normalize(outputs[1][0])
    assert outputs[0][1:] == outputs[1][1:]


async def service_artifact(db, ref):
    from contexthub.services.artifact_service import ArtifactService

    return await ArtifactService().get(db, ref)


@pytest.mark.asyncio
async def test_multi_assertion_write_failure_rolls_back_all(phased, monkeypatch):
    factory, models, repo, ctx = phased
    service = await factory()
    _, handoff, _, _ = await build(
        service,
        ctx,
        ("A: allowance 500|D: subscription 20|B: uses A for reimbursement",),
    )
    update = service.context_service.update
    writes = 0

    async def fail_second(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("synthetic storage failure")
        return await update(*args, **kwargs)

    monkeypatch.setattr(service.context_service, "update", fail_second)
    request = AssertionChangeRequest(
        request_id=uuid4(),
        content="Now |A: allowance 400|D: subscription 10",
        handoff=handoff.reference(),
    )
    result = await service.ingest_assertion_change(request, ctx)
    assert result.status == "pending" and result.reason == "operation_failed"
    assert result.location_status == "pending" and not result.event_ids
    async with repo.session(ctx.account_id) as db:
        assert await db.fetchval("SELECT max(version) FROM contexts") == 1
        assert (
            await db.fetchval(
                "SELECT count(*) FROM change_events WHERE account_id=$1 AND change_type='modified'",
                ctx.account_id,
            )
            == 0
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM knowledge_artifacts WHERE kind='assertion_change'"
            )
            == 1
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM context_invalidations i JOIN contexts c ON c.id=i.context_id WHERE c.account_id=$1",
                ctx.account_id,
            )
            == 0
        )
    calls = len(models.calls)
    again = await service.ingest_assertion_change(request, ctx)
    assert again == result and len(models.calls) == calls and writes == 2
