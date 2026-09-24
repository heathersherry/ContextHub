"""S6 real core/transactions; explicit new ordinary-role database only."""

import os
import json
import asyncio
from uuid import uuid4
import asyncpg
import httpx
import pytest
import pytest_asyncio

from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.models.knowledge import canonical_hash, ContractError, text_hash
from contexthub.propagation.candidate import CandidateGenerator, Content, seal_proposal
from contexthub.propagation.evidence import EvidenceBuilder
from contexthub.propagation.hard_checks import HardChecks, passed
from contexthub.propagation.verifier import FixedVerifier
from contexthub.services.execution_ledger import ExecutionLedger
from test_integration_maintenance_stale_first import prepared
from s6_helpers import make_data, response, verdict_payload, model_config
from contexthub.propagation.profiles import MaintenanceRunner, ProfileConfig
from contexthub.propagation.candidate import Cause
from test_maintenance_planner import planner
from contexthub.propagation.commit import GuardedCommit

DSN = "postgresql://s2_s6_app@127.0.0.1:55487/contexthub_s2_s6"


@pytest_asyncio.fixture
async def s6_repo():
    assert os.environ.get("S6_TEST_DATABASE_URL") == DSN
    pool = await asyncpg.create_pool(
        DSN, min_size=2, max_size=10, init=init_pg_connection
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
        assert not row["rolsuper"] and not row["rolbypassrls"]
        assert await conn.fetchval("SELECT current_database()") == "contexthub_s2_s6"
        assert await conn.fetchval("SHOW port") == "55487"
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "011"
        assert await conn.fetchval("SELECT current_schema()") == "public"
    try:
        yield PgRepository(pool)
    finally:
        await pool.close()


async def proposal_chain(repo, *, verdict="PASS", outcome="updated"):
    executor, ctx, watch, event, unrelated, frames = await prepared(repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    receipt = await EvidenceBuilder(executor).build_evidence(frame.reference(), "M1")
    checks = HardChecks(executor)
    cfg = model_config()
    ledger = ExecutionLedger(repo)
    async with repo.session(frame.account_id) as db:
        binding = await checks.bind(
            db,
            frame.reference(),
            receipt.reference(),
            profile="adaptive",
            action="M1",
            configuration_sha256=canonical_hash(cfg),
        )
        bundle = await executor.artifacts.get(db, receipt.bundle)
        sources = await checks.sources(db, frame, bundle)
        evidence = tuple(
            r.evidence for r in bundle.read if r.evidence.provenance == ("after",)
        )
        data = make_data(
            binding.original
            if outcome == "unchanged"
            else Content(
                l0_content="new",
                l1_content="new summary",
                l2_content="new complete conclusion",
            ),
            evidence,
            binding.causes,
            outcome,
        )
    calls = []

    def send(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "base":
            return response(
                dict(proposal=data.model_dump(mode="json"), unresolved_reason=None)
            )
        payload = verdict_payload(data)
        if isinstance(verdict, tuple):
            payload[verdict[0]]["verdict"] = verdict[1]
        else:
            payload = verdict_payload(data, verdict)
        return response(payload)

    transport = httpx.MockTransport(send)
    generator = CandidateGenerator(
        cfg, api_key="synthetic", recorder=ledger, transport=transport
    )
    verifier = FixedVerifier(
        model_config("verifier"),
        checks=checks,
        api_key="synthetic",
        recorder=ledger,
        transport=transport,
    )
    try:
        generated, ident = await generator.generate(frame, binding, bundle, sources)
        async with repo.session(frame.account_id) as db:
            proposal = await seal_proposal(
                executor.artifacts, db, binding, generated.proposal, ident
            )
            check = await checks.check(db, proposal.reference())
        assert passed(check), check
        result = await verifier.verify(
            proposal.reference(), check.reference(), frame=frame, binding=binding
        )
    finally:
        await generator.close()
    return executor, frame, binding, proposal, check, result, verifier, calls


@pytest.mark.asyncio
async def test_checkpoint1_real_candidate_checks_and_separate_verifier(s6_repo):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo)
    try:
        assert len(calls) == 2
        assert calls[0]["model"] == "base" and calls[1]["model"] == "verifier"
        payload = json.loads(calls[1]["messages"][-1]["content"].removeprefix("Input "))
        assert set(payload) == {"candidate", "bundle", "allowed_sources", "hard_checks"}
        async with s6_repo.session(frame.account_id) as db:
            snap = await executor.artifacts.get(db, frame.snapshot)
            assert (
                await db.fetchval(
                    "SELECT validity_status FROM contexts WHERE id=$1",
                    snap.target.context_id,
                )
                == "stale"
            )
            assert (
                await executor.artifacts.get_work(db, frame.account_id, snap.id)
            ).status == "pending"
            assert not await db.fetch("SELECT * FROM context_refresh_proofs")
    finally:
        await verifier.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["applicability", "complete_support", "conflicts_resolved"]
)
@pytest.mark.parametrize("value", ["FAIL", "UNKNOWN"])
async def test_each_nonpass_cannot_grant_commit(s6_repo, name, value):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo, verdict=(name, value))
    try:
        with pytest.raises(ContractError, match="live_verifier_grant_required"):
            verifier.consume(
                result.reference(),
                proposal.reference(),
                check.reference(),
                binding.reference(),
            )
    finally:
        await verifier.close()


async def run_profile(
    repo,
    *,
    profile="adaptive",
    results=(),
    verifier_error=False,
    price="0",
    local_omit=False,
):
    executor, ctx, watch, event, unrelated, frames = await prepared(repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    return await run_on_frame(
        executor,
        frame,
        profile=profile,
        results=results,
        verifier_error=verifier_error,
        price=price,
        local_omit=local_omit,
    )


async def run_on_frame(
    executor,
    frame,
    *,
    profile="adaptive",
    results=(),
    verifier_error=False,
    price="0",
    local_omit=False,
    rules=(),
    verifier_delay=0,
    verifier_results=(),
):
    calls = []
    generated = []
    verifications = []

    def send(request):
        body = json.loads(request.content)
        calls.append(body)
        p = json.loads(body["messages"][-1]["content"].removeprefix("Input "))
        if body["model"] == "verifier":
            if verifier_error:
                return httpx.Response(503, json={})
            verifications.append(p)
            result = (
                verifier_results[len(verifications) - 1]
                if len(verifications) <= len(verifier_results)
                else "PASS"
            )
            return response(verdict_payload(p["candidate"], result))
        generated.append(p)
        result = (
            results[len(generated) - 1]
            if len(generated) <= len(results)
            else "unresolved"
        )
        if result == "schema":
            return response({"PASS": True})
        if result == "unresolved":
            return response(dict(proposal=None, unresolved_reason="synthetic unknown"))
        from contexthub.models.knowledge import EvidenceRef

        cause_versions = [c["source"] for c in p["binding"]["causes"]]
        source_ids = {
            s["source"]["id"]
            for s in p["sources"]
            if s["source"]["source"] in cause_versions
        }
        refs = tuple(
            EvidenceRef.model_validate(e["evidence"])
            for e in p["bundle"]["read"]
            if e["evidence"]["source"]["id"] in source_ids
        )
        causes = tuple(Cause.model_validate(c) for c in p["binding"]["causes"])
        content = (
            Content.model_validate(p["binding"]["original"])
            if result == "unchanged"
            else Content(
                l0_content="new",
                l1_content="new summary",
                l2_content="new complete conclusion",
            )
        )
        data = make_data(content, refs, causes, result)
        return response(
            dict(proposal=data.model_dump(mode="json"), unresolved_reason=None)
        )

    checks = HardChecks(executor, rules)

    async def delayed(request):
        if json.loads(request.content)["model"] == "verifier" and verifier_delay:
            await asyncio.sleep(verifier_delay)
        return send(request)

    transport = httpx.MockTransport(delayed)
    base = CandidateGenerator(
        model_config("base"),
        api_key="synthetic",
        recorder=executor.recorder,
        transport=transport,
    )
    strong = CandidateGenerator(
        model_config("strong"),
        api_key="synthetic",
        recorder=executor.recorder,
        transport=transport,
    )
    verifier = FixedVerifier(
        model_config("verifier"),
        checks=checks,
        api_key="synthetic",
        recorder=executor.recorder,
        transport=transport,
    )
    runner = MaintenanceRunner(
        executor,
        ProfileConfig(
            profile=profile,
            planner=planner(price),
            action_timeout_seconds=20,
            rule_timeout_seconds=2,
        ),
        base=base,
        strong=strong,
        verifier=verifier,
        rules=rules,
    )
    try:
        state = await runner.run(
            frame.reference(), local_sources=() if local_omit else None
        )
    finally:
        await base.close()
        await strong.close()
        await verifier.close()
    return state, calls, generated, executor, frame


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,models",
    [
        ("local/base-only", ["base"]),
        ("full/strong-only", ["strong"]),
        ("adaptive", ["base", "base", "strong"]),
    ],
)
async def test_profiles_real_failure_paths_and_same_m2_m3(s6_repo, profile, models):
    state, calls, generated, executor, frame = await run_profile(
        s6_repo, profile=profile
    )
    assert state.status == "unresolved" and state.reason == "profile_exhausted"
    assert [c["model"] for c in calls] == models
    if profile == "adaptive":
        assert generated[1]["bundle"] == generated[2]["bundle"]


@pytest.mark.asyncio
async def test_verifier_exhaustion_stops_no_strong_upgrade(s6_repo):
    state, calls, *_ = await run_profile(
        s6_repo, results=("updated",), verifier_error=True
    )
    assert state.reason == "verifier_unavailable"
    assert [c["model"] for c in calls] == ["base", "verifier", "verifier", "verifier"]


@pytest.mark.asyncio
@pytest.mark.parametrize("price,expected", [("1", 3), ("1.1", 1)])
async def test_actual_price_gate(s6_repo, price, expected):
    state, calls, *_ = await run_profile(s6_repo, price=price)
    assert len(calls) == expected
    assert state.reason == ("price_stopped" if expected == 1 else "profile_exhausted")


@pytest.mark.asyncio
async def test_actual_m1_omission_skips_generation_but_allows_m2(s6_repo):
    state, calls, generated, *_ = await run_profile(s6_repo, local_omit=True)
    assert [c["model"] for c in calls] == ["base", "strong"]
    assert generated[0]["bundle"]["scope_id"] == "M2"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["updated", "unchanged"])
async def test_atomic_commit_and_normal_read(s6_repo, outcome):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo, outcome=outcome)
    try:
        state = await GuardedCommit(executor, verifier.checks, verifier).commit(
            result.reference()
        )
        assert state.status == outcome
        async with s6_repo.session(frame.account_id) as db:
            snap = await executor.artifacts.get(db, frame.snapshot)
            target = await db.fetchrow(
                "SELECT * FROM contexts WHERE id=$1", snap.target.context_id
            )
            assert target["status"] == "active" and target["validity_status"] == "fresh"
            assert target["version"] == snap.target.version + (outcome == "updated")
            for level in ("l0_content", "l1_content", "l2_content"):
                assert target[level] == getattr(proposal.data.content, level)
            proof = await executor.artifacts.get(db, state.proof)
            assert proof.schema_version == 2 and proof.authority == "audit_only"
            assert (
                proof.commit_event_id is not None
                if outcome == "updated"
                else proof.commit_event_id is None
            )
            assert not await db.fetch(
                "SELECT * FROM context_invalidations WHERE context_id=$1 AND resolved_at IS NULL",
                snap.target.context_id,
            )
            from contexthub.store.context_store import ContextStore
            from contexthub.services.acl_service import ACLService
            from contexthub.services.masking_service import MaskingService
            from contexthub.models.context import ContextLevel
            from contexthub.models.request import RequestContext

            assert (
                await ContextStore(ACLService(), MaskingService()).read(
                    db,
                    target["uri"],
                    ContextLevel.L2,
                    RequestContext(account_id=frame.account_id, agent_id="system"),
                )
                == proposal.data.content.l2_content
            )
        with pytest.raises(ContractError, match="live_verifier_grant_required"):
            await GuardedCommit(executor, verifier.checks, verifier).commit(
                result.reference()
            )
    finally:
        await verifier.close()


async def independent_change(frame, executor, kind):
    conn = await asyncpg.connect(DSN)
    await init_pg_connection(conn)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.account_id',$1,true)", frame.account_id
            )
            async with executor.repo.session(frame.account_id) as db:
                assert await conn.fetchval(
                    "SELECT pg_backend_pid()"
                ) != await db.fetchval("SELECT pg_backend_pid()")
                snap = await executor.artifacts.get(db, frame.snapshot)
            if kind == "target":
                await conn.execute(
                    "UPDATE contexts SET l2_content='external',version=version+1 WHERE id=$1",
                    snap.target.context_id,
                )
            elif kind == "upstream":
                await conn.execute(
                    "UPDATE contexts SET l2_content='external root',version=version+1 WHERE id=$1",
                    frame.new.context_id,
                )
            elif kind == "evidence":
                ident = next(
                    s.source.context_id
                    for s in frame.history
                    if s.source.context_id
                    not in (snap.target.context_id, frame.new.context_id)
                )
                await conn.execute(
                    "UPDATE contexts SET l2_content='external evidence',version=version+1 WHERE id=$1",
                    ident,
                )
            elif kind == "cause":
                await conn.execute(
                    "UPDATE context_invalidations SET source_version=source_version+1 WHERE context_id=$1 AND resolved_at IS NULL",
                    snap.target.context_id,
                )
            elif kind == "work":
                await conn.execute(
                    "UPDATE maintenance_work_items SET revision=revision+1,state=jsonb_set(state,'{revision}',to_jsonb(revision+1)) WHERE id=$1",
                    snap.id,
                )
            elif kind == "permission":
                await conn.execute(
                    "INSERT INTO access_policies(account_id,resource_uri_pattern,principal,effect,actions,created_by) VALUES($1,'ctx://*','s5-test','deny',ARRAY['read'],'test')",
                    frame.account_id,
                )
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["target", "upstream", "evidence", "cause", "work", "permission"]
)
async def test_independent_connection_drift_rejects_commit(s6_repo, kind):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo)
    try:
        await independent_change(frame, executor, kind)
        with pytest.raises(ContractError):
            await GuardedCommit(executor, verifier.checks, verifier).commit(
                result.reference()
            )
        async with s6_repo.session(frame.account_id) as db:
            snap = await executor.artifacts.get(db, frame.snapshot)
            assert (
                await db.fetchval(
                    "SELECT validity_status FROM contexts WHERE id=$1",
                    snap.target.context_id,
                )
                == "stale"
            )
            assert not await db.fetch("SELECT * FROM context_refresh_proofs")
            assert (
                await executor.artifacts.get_work(db, frame.account_id, snap.id)
            ).status == "unresolved"
    finally:
        await verifier.close()


@pytest.mark.asyncio
async def test_proof_failure_rolls_back_content_causes_work_cost_survives(
    s6_repo, monkeypatch
):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo)

    async def fail(*args):
        raise ContractError("injected_proof_write_failure")

    monkeypatch.setattr(executor.artifacts, "archive_commit_proof", fail)
    try:
        with pytest.raises(ContractError, match="injected_proof_write_failure"):
            await GuardedCommit(executor, verifier.checks, verifier).commit(
                result.reference()
            )
        async with s6_repo.session(frame.account_id) as db:
            snap = await executor.artifacts.get(db, frame.snapshot)
            row = await db.fetchrow(
                "SELECT version,validity_status FROM contexts WHERE id=$1",
                snap.target.context_id,
            )
            assert (
                row["version"] == snap.target.version
                and row["validity_status"] == "stale"
            )
            assert await db.fetchval(
                "SELECT count(*) FROM context_invalidations WHERE context_id=$1 AND resolved_at IS NULL",
                snap.target.context_id,
            ) == len(binding.causes)
            attempts = await db.fetch(
                "SELECT payload FROM execution_attempts WHERE execution_id=$1 AND phase='finished'",
                frame.execution_id,
            )
            assert (
                len(
                    [
                        a
                        for a in attempts
                        if a["payload"]["identity"]["model_role"]
                        in ("generator", "verifier")
                    ]
                )
                == 2
            )
            assert any(a["payload"]["stop_reason"] == "non_retryable" for a in attempts)
    finally:
        await verifier.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results,models",
    [
        (("updated",), ["base", "verifier"]),
        (("unresolved", "updated"), ["base", "base", "verifier"]),
        (
            ("unresolved", "unresolved", "updated"),
            ["base", "base", "strong", "verifier"],
        ),
    ],
)
async def test_actual_success_early_exit(s6_repo, results, models):
    state, calls, *_ = await run_profile(s6_repo, results=results)
    assert state.status == "updated", state
    assert [c["model"] for c in calls] == models


@pytest.mark.asyncio
async def test_check_write_interval_is_fenced_against_independent_writer(
    s6_repo, monkeypatch
):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo)
    checked = asyncio.Event()
    writer_started = asyncio.Event()
    finished = asyncio.Event()

    async def writer():
        await checked.wait()
        conn = await asyncpg.connect(DSN)
        await init_pg_connection(conn)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id',$1,true)", frame.account_id
                )
                writer_started.set()
                await conn.execute(
                    "UPDATE contexts SET l2_content='concurrent root',version=version+1 WHERE id=$1",
                    frame.new.context_id,
                )
                await conn.execute(
                    "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','external')",
                    frame.new.context_id,
                    frame.account_id,
                )
            finished.set()
        finally:
            await conn.close()

    task = asyncio.create_task(writer())
    original = verifier.checks.check

    async def interleave(*args, **kwargs):
        actual = await original(*args, **kwargs)
        checked.set()
        await writer_started.wait()
        await asyncio.sleep(0.03)
        assert not finished.is_set(), (
            "writer entered the protected check/write interval"
        )
        return actual

    monkeypatch.setattr(verifier.checks, "check", interleave)
    try:
        state = await GuardedCommit(executor, verifier.checks, verifier).commit(
            result.reference()
        )
        await asyncio.wait_for(task, 5)
        assert state.status == "updated"
        async with s6_repo.session(frame.account_id) as db:
            snap = await executor.artifacts.get(db, frame.snapshot)
            # Later serial change installs its own barrier, so old proof never
            # overrides the new event's non-fresh state.
            assert (
                await db.fetchval(
                    "SELECT validity_status FROM contexts WHERE id=$1",
                    snap.target.context_id,
                )
                == "stale"
            )
            assert (
                await db.fetchval(
                    "SELECT count(*) FROM context_invalidations WHERE context_id=$1 AND resolved_at IS NULL",
                    snap.target.context_id,
                )
                == 1
            )
    finally:
        if not task.done():
            task.cancel()
        await verifier.close()


async def small_chain(repo):
    from maintenance_helpers import make_graph
    from contexthub.propagation.executor import MaintenanceExecutor
    from contexthub.models.request import RequestContext
    from test_integration_maintenance_stale_first import config

    account, source, report = await make_graph(
        repo, labels="ABC", edges=(("A", "B"), ("B", "C"))
    )
    executor = MaintenanceExecutor(repo)
    async with repo.session(account) as db:
        refs = await executor.register_graph(
            db,
            report.reference(),
            config(),
            RequestContext(account_id=account, agent_id="system"),
        )
        watches = [await executor.artifacts.get(db, r) for r in refs]
        watch = next(w for w in watches if w.source == source)
        await db.execute(
            "UPDATE contexts SET l2_content='Changed chain source',version=version+1 WHERE id=$1",
            source.context_id,
        )
        event = await db.fetchval(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','system') RETURNING event_id",
            source.context_id,
            account,
        )
    refs = await executor.begin_maintenance(event, watch.reference())
    async with repo.session(account) as db:
        frames = [await executor.artifacts.get(db, r) for r in refs]
        mapped = {}
        for f in frames:
            snap = await executor.artifacts.get(db, f.snapshot)
            label = await db.fetchval(
                "SELECT l2_content FROM contexts WHERE id=$1", snap.target.context_id
            )
            mapped[label] = f
    return executor, mapped


@pytest.mark.asyncio
async def test_real_two_hop_committed_versions_and_bounded_new_frames(s6_repo):
    executor, frames = await small_chain(s6_repo)
    blocked, calls, *_ = await run_on_frame(executor, frames["C"])
    assert blocked.status == "unresolved" and not calls
    a, acalls, *_ = await run_on_frame(executor, frames["A"], results=("updated",))
    assert a.status == "updated"
    b_ref = await executor.continue_after_commits(frames["B"].reference(), (a.proof,))
    async with s6_repo.session(frames["B"].account_id) as db:
        b_frame = await executor.artifacts.get(db, b_ref)
        assert b_frame.deadline_at == frames["B"].deadline_at
        assert b_frame.expected_upstreams[0].version == 2
    b, bcalls, *_ = await run_on_frame(executor, b_frame, results=("updated",))
    assert b.status == "updated", b
    c_ref = await executor.continue_after_commits(
        frames["C"].reference(), (a.proof, b.proof)
    )
    async with s6_repo.session(frames["C"].account_id) as db:
        c_frame = await executor.artifacts.get(db, c_ref)
        assert c_frame.expected_upstreams[0].version == 2
        assert c_frame.deadline_at == frames["C"].deadline_at
    c, ccalls, *_ = await run_on_frame(executor, c_frame, results=("updated",))
    assert c.status == "updated", c
    async with s6_repo.session(c_frame.account_id) as db:
        proof = await executor.artifacts.get(db, c.proof)
        assert len(proof.checked_causes) == 3
        assert proof.checked_upstreams == c_frame.expected_upstreams
        assert (
            await executor.artifacts.get_work(
                db, c_frame.account_id, frames["C"].snapshot.id
            )
        ).status == "unresolved"
    with pytest.raises(ContractError, match="continuation_limit"):
        await executor.continue_after_commits(
            frames["C"].reference(), (a.proof, b.proof)
        )


@pytest.mark.asyncio
async def test_continuation_external_change_stops(s6_repo):
    executor, frames = await small_chain(s6_repo)
    a, *_ = await run_on_frame(executor, frames["A"], results=("updated",))
    assert a.status == "updated"
    await independent_change(frames["B"], executor, "upstream")
    with pytest.raises(ContractError, match="continuation_external_drift"):
        await executor.continue_after_commits(frames["B"].reference(), (a.proof,))


@pytest.mark.asyncio
@pytest.mark.parametrize("produce", [True, False])
async def test_registered_rule_real_execution_same_verifier_no_direct_fresh(
    s6_repo, produce
):
    from test_integration_maintenance_stale_first import changed
    from test_maintenance_rules import SyntheticRule
    from contexthub.models.knowledge import ContextVersion

    executor, ctx, watch, event, _ = await changed(s6_repo)
    async with s6_repo.session(ctx.account_id) as db:
        graph = await executor.artifacts.get(db, watch.graph)
        target = next(
            n for n in graph.nodes if not any(e.downstream == n for e in graph.edges)
        )
        rule = SyntheticRule(
            ContextVersion(context_id=watch.source.context_id, version=2),
            produce=produce,
        )
        await executor.register_rule_inputs(
            db, account_id=ctx.account_id, target=target, provider=rule
        )
    refs = await executor.begin_maintenance(event["event_id"], watch.reference())
    async with s6_repo.session(ctx.account_id) as db:
        frames = [await executor.artifacts.get(db, r) for r in refs]
        frame = next(f for f in frames if not f.expected_upstreams)
    state, calls, *_ = await run_on_frame(
        executor, frame, profile="local/base-only", rules=(rule,)
    )
    assert state.status == ("updated" if produce else "unresolved"), state
    assert [c["model"] for c in calls] == (["verifier"] if produce else ["base"])
    async with s6_repo.session(ctx.account_id) as db:
        records = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE execution_id=$1",
            frame.execution_id,
        )
        rules = [
            r["payload"]
            for r in records
            if r["payload"]["identity"]["operation"] == "deterministic-rule"
        ]
        assert len(rules) == 2 and rules[-1]["requested_model"] is None
        assert rules[-1]["usage"]["input_tokens"] is None


@pytest.mark.asyncio
async def test_known_conflict_and_unrelated_reason_block(s6_repo):
    from contexthub.propagation.contracts import InputRegistration
    from test_integration_maintenance_stale_first import changed

    executor, ctx, watch, event, _ = await changed(s6_repo)
    async with s6_repo.session(ctx.account_id) as db:
        for target in watch.affected:
            await executor.register_inputs(
                db,
                InputRegistration(
                    account_id=ctx.account_id,
                    id=uuid4(),
                    target_id=target.context_id,
                    inputs=(),
                    unresolved_conflict_ids=(uuid4(),),
                ),
            )
    refs = await executor.begin_maintenance(event["event_id"], watch.reference())
    async with s6_repo.session(ctx.account_id) as db:
        frame = await executor.artifacts.get(db, refs[0])
    state, calls, *_ = await run_on_frame(executor, frame, results=("updated",))
    assert state.reason == "registered_conflict_unresolved" and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,validity",
    [("deleted", "stale"), ("stale", "superseded"), ("stale", "invalid")],
)
async def test_inapplicable_targets_not_revived(s6_repo, status, validity):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo)
    try:
        async with s6_repo.session(frame.account_id) as db:
            snap = await executor.artifacts.get(db, frame.snapshot)
            await db.execute(
                "UPDATE contexts SET status=$2,validity_status=$3 WHERE id=$1",
                snap.target.context_id,
                status,
                validity,
            )
        with pytest.raises(ContractError):
            await GuardedCommit(executor, verifier.checks, verifier).commit(
                result.reference()
            )
        async with s6_repo.session(frame.account_id) as db:
            row = await db.fetchrow(
                "SELECT status,validity_status FROM contexts WHERE id=$1",
                snap.target.context_id,
            )
            assert row["status"] == status and row["validity_status"] == validity
    finally:
        await verifier.close()


@pytest.mark.asyncio
async def test_cross_candidate_and_archived_proof_do_not_authorize(s6_repo):
    (
        executor,
        frame,
        binding,
        proposal,
        check,
        result,
        verifier,
        calls,
    ) = await proposal_chain(s6_repo)
    try:
        from contexthub.propagation.verifier import Verdict

        other_receipt = await EvidenceBuilder(executor).build_evidence(
            frame.reference(), "M2"
        )
        async with s6_repo.session(frame.account_id) as db:
            row = await db.fetchrow(
                "SELECT payload FROM knowledge_artifacts WHERE kind='maintenance_frame' AND id<>$1 LIMIT 1",
                frame.id,
            )
            other_frame = type(frame).model_validate(row["payload"])

        altered = Verdict(**{**result.model_dump(), "id": uuid4()})
        async with s6_repo.session(frame.account_id) as db:
            await executor.artifacts.seal(db, altered)
        with pytest.raises(ContractError, match="live_verifier_grant_required"):
            await GuardedCommit(executor, verifier.checks, verifier).commit(
                altered.reference()
            )
        state = await GuardedCommit(executor, verifier.checks, verifier).commit(
            result.reference()
        )
        async with s6_repo.session(frame.account_id) as db:
            proof = await executor.artifacts.get(db, state.proof)
            archived = type(proof)(**{**proof.model_dump(), "id": uuid4()})
            await executor.artifacts.archive_commit_proof(db, archived)
            for field, wrong in (
                ("frame", other_frame.reference()),
                ("receipt", other_receipt.reference()),
            ):
                forged = type(proof)(
                    **{**proof.model_dump(), "id": uuid4(), field: wrong}
                )
                with pytest.raises(
                    ContractError, match="guarded_proof_binding_mismatch"
                ):
                    await executor.artifacts.archive_commit_proof(db, forged)
            with pytest.raises(ContractError):
                await executor.artifacts.update_work(
                    db,
                    frame.account_id,
                    frame.snapshot.id,
                    state.revision,
                    __import__(
                        "contexthub.models.maintenance", fromlist=["WorkState"]
                    ).WorkState(
                        revision=state.revision + 1,
                        status="updated",
                        proof=archived.reference(),
                    ),
                )
    finally:
        await verifier.close()


@pytest.mark.asyncio
async def test_new_prepare_cannot_reset_deadline_or_replay_work(s6_repo):
    state, calls, generated, executor, frame = await run_profile(
        s6_repo, profile="local/base-only"
    )
    refs = await executor.begin_maintenance(frame.event_id, frame.watch)
    async with s6_repo.session(frame.account_id) as db:
        replacements = [await executor.artifacts.get(db, r) for r in refs]
        replacement = next(
            f for f in replacements if f.expected_upstreams == frame.expected_upstreams
        )
    with pytest.raises(ContractError, match="event_target_already_attempted"):
        await run_on_frame(executor, replacement, results=("updated",))


@pytest.mark.asyncio
async def test_late_verifier_result_cannot_commit_or_upgrade(s6_repo):
    from datetime import datetime, timezone, timedelta

    executor, ctx, watch, event, _, frames = await prepared(s6_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    frame = type(frame)(
        **{
            **frame.model_dump(),
            "id": uuid4(),
            "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=0.5),
        }
    )
    async with s6_repo.session(frame.account_id) as db:
        await executor.artifacts.seal(db, frame)
    state, calls, *_ = await run_on_frame(
        executor, frame, results=("updated",), verifier_delay=1
    )
    assert state.status == "unresolved" and state.reason == "deadline_exhausted", state
    assert [c["model"] for c in calls] == ["base"]
    async with s6_repo.session(frame.account_id) as db:
        assert not await db.fetch("SELECT * FROM context_refresh_proofs")
        records = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE execution_id=$1",
            frame.execution_id,
        )
        assert any(r["payload"]["outcome"] == "cancelled" for r in records)


@pytest.mark.asyncio
async def test_legacy_bad_receipt_cannot_bind_to_s6(s6_repo):
    executor, ctx, watch, event, _, frames = await prepared(s6_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    receipt = await EvidenceBuilder(executor).build_evidence(frame.reference(), "M2")
    async with s6_repo.session(frame.account_id) as db:
        bundle = await executor.artifacts.get(db, receipt.bundle)
        # A historical old-writer fixture: both lists damaged identically, all
        # original frame/receipt progress retained. No existing seal is mutated.
        damaged = type(bundle)(
            **{
                **bundle.model_dump(),
                "id": uuid4(),
                "required": bundle.required[:-1],
                "read": bundle.read[:-1],
            }
        )
        old_receipt = type(receipt)(
            **{**receipt.model_dump(), "id": uuid4(), "bundle": damaged.reference()}
        )
        for obj in (damaged, old_receipt):
            await db.execute(
                "INSERT INTO knowledge_artifacts(account_id,id,kind,schema_version,sha256,payload) VALUES($1,$2,$3,1,$4,$5)",
                obj.account_id,
                obj.id,
                obj.kind,
                obj.reference().sha256,
                obj.model_dump(mode="json"),
            )
    async with s6_repo.session(frame.account_id) as db:
        with pytest.raises(ContractError, match="bundle_manifest_mismatch"):
            await HardChecks(executor).bind(
                db,
                frame.reference(),
                old_receipt.reference(),
                profile="adaptive",
                action="M2",
                configuration_sha256=canonical_hash(model_config()),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["s5:source_changed", "other:cause"])
async def test_extra_unexplained_cause_cannot_be_cleared(s6_repo, reason):
    from test_integration_maintenance_stale_first import changed

    executor, ctx, watch, event, unrelated = await changed(s6_repo)
    async with s6_repo.session(ctx.account_id) as db:
        extra = await db.fetchval(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','test') RETURNING event_id",
            unrelated,
            ctx.account_id,
        )
        for target in watch.affected:
            await db.execute(
                "INSERT INTO context_invalidations(context_id,cause_event_id,source_context_id,source_version,reason_hash) VALUES($1,$2,$3,1,$4)",
                target.context_id,
                extra,
                unrelated,
                text_hash(reason),
            )
    refs = await executor.begin_maintenance(event["event_id"], watch.reference())
    async with s6_repo.session(ctx.account_id) as db:
        frames = [await executor.artifacts.get(db, r) for r in refs]
        frame = next(f for f in frames if not f.expected_upstreams)
    state, calls, *_ = await run_on_frame(executor, frame, results=("updated",))
    assert state.status == "unresolved" and state.reason == (
        "cause_resolution_not_authorized"
        if reason == "s5:source_changed"
        else "cause_kind_not_supported"
    ), state
    assert [c["model"] for c in calls] == ["base"]
    async with s6_repo.session(ctx.account_id) as db:
        assert not await db.fetch("SELECT * FROM context_refresh_proofs")


@pytest.mark.asyncio
async def test_same_tenant_distinct_executions_and_cross_tenant_isolation(s6_repo):
    from maintenance_helpers import make_graph
    from contexthub.propagation.executor import MaintenanceExecutor
    from contexthub.models.request import RequestContext
    from test_integration_maintenance_stale_first import config

    account, source, report = await make_graph(s6_repo, labels="AB", edges=())
    executor = MaintenanceExecutor(s6_repo)
    async with s6_repo.session(account) as db:
        refs = await executor.register_graph(
            db,
            report.reference(),
            config(),
            RequestContext(account_id=account, agent_id="system"),
        )
        watches = [await executor.artifacts.get(db, r) for r in refs]
        watch = next(w for w in watches if w.source == source)
        await db.execute(
            "UPDATE contexts SET l2_content='changed',version=version+1 WHERE id=$1",
            source.context_id,
        )
        event = await db.fetchval(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','system') RETURNING event_id",
            source.context_id,
            account,
        )
    refs = await executor.begin_maintenance(event, watch.reference())
    async with s6_repo.session(account) as db:
        a, b = [await executor.artifacts.get(db, r) for r in refs]
        b = type(b)(**{**b.model_dump(), "id": uuid4(), "execution_id": uuid4()})
        await executor.artifacts.seal(db, b)
    outcomes = await asyncio.gather(
        run_on_frame(executor, a, profile="local/base-only"),
        run_on_frame(executor, b, profile="full/strong-only"),
    )
    assert all(r[0].status == "unresolved" for r in outcomes)
    async with s6_repo.session(account) as db:
        for frame in (a, b):
            records = await db.fetch(
                "SELECT payload FROM execution_attempts WHERE execution_id=$1",
                frame.execution_id,
            )
            model_records = [
                r["payload"]
                for r in records
                if r["payload"]["identity"]["model_role"] == "generator"
            ]
            assert len(model_records) == 2
            assert all(
                r["identity"]["work_item_id"] == str(frame.snapshot.id)
                for r in model_records
            )
    async with s6_repo.session("other-" + account) as db:
        assert not await db.fetch(
            "SELECT * FROM execution_attempts WHERE execution_id=$1", a.execution_id
        )
        with pytest.raises(ContractError, match="tenant_mismatch"):
            await executor.artifacts.get(db, a.reference())


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
async def test_semantic_failure_upgrades_once_with_new_independent_verifier(
    s6_repo, verdict
):
    executor, _, _, _, _, frames = await prepared(s6_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    state, calls, generated, *_ = await run_on_frame(
        executor,
        frame,
        results=("updated", "updated"),
        verifier_results=(verdict, "PASS"),
    )
    assert state.status == "updated"
    assert [c["model"] for c in calls] == ["base", "verifier", "base", "verifier"]
    assert [g["binding"]["action"] for g in generated] == ["M1", "M2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0, 0.3])
async def test_claim_lock_wait_is_bounded_by_original_deadline(s6_repo, budget):
    from datetime import datetime, timezone, timedelta

    executor, _, _, _, _, frames = await prepared(s6_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    frame = type(frame)(
        **{
            **frame.model_dump(),
            "id": uuid4(),
            "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=budget),
        }
    )
    async with s6_repo.session(frame.account_id) as db:
        await executor.artifacts.seal(db, frame)
        snap = await executor.artifacts.get(db, frame.snapshot)
    async with s6_repo.session(frame.account_id) as blocker:
        await blocker.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"s6:{frame.account_id}:{frame.event_id}:{snap.target.context_id}",
        )
        state, calls, *_ = await asyncio.wait_for(run_on_frame(executor, frame), 3)
    assert state.status == "unresolved" and state.reason == "deadline_exhausted"
    assert not calls
