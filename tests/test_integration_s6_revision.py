"""S6-R1/R2 regression: actual core, ordinary PG, model HTTP substitutes only."""

import contextvars
import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import test_integration_maintenance_commit as h
from contexthub.models.knowledge import ContractError, text_hash
from contexthub.models.request import RequestContext
from contexthub.propagation.commit import GuardedCommit
from contexthub.propagation.verifier import FixedVerifier
from contexthub.db.repository import ScopedRepo
from contexthub.db.codecs import init_pg_connection
from contexthub.discovery._work import MeteredDB

s6_repo = h.s6_repo


async def export(ex, frame, name, extra):
    async with ex.repo.session(frame.account_id) as db:
        rows = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE account_id=$1 AND execution_id=$2",
            frame.account_id,
            frame.execution_id,
        )
        result = {
            "frame": frame.model_dump(mode="json"),
            "extra": extra,
            "attempts": [r["payload"] for r in rows],
        }
    if os.environ.get("S6_REVIEW_OUT"):
        p = Path(os.environ["S6_REVIEW_OUT"]) / (name + ".json")
        assert not p.exists()
        p.write_text(json.dumps(result, indent=2, default=str))


async def read_assert(ex, frame, fresh):
    from contexthub.models.context import ContextLevel
    from contexthub.store.context_store import ContextStore
    from contexthub.services.acl_service import ACLService
    from contexthub.services.masking_service import MaskingService
    from contexthub.errors import ConflictError

    async with ex.repo.session(frame.account_id) as db:
        snap = await ex.artifacts.get(db, frame.snapshot)
        uri = await db.fetchval(
            "SELECT uri FROM contexts WHERE id=$1", snap.target.context_id
        )
        store = ContextStore(ACLService(), MaskingService())
        ctx = RequestContext(account_id=frame.account_id, agent_id="system")
        if fresh:
            assert await store.read(db, uri, ContextLevel.L2, ctx)
        else:
            with pytest.raises(ConflictError):
                await store.read(db, uri, ContextLevel.L2, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", [False, True])
@pytest.mark.parametrize("real_event", [False, True])
async def test_archived_proof_must_not_authorize_unrelated_cause(
    s6_repo, monkeypatch, forge, real_event
):
    from uuid import UUID
    import maintenance_helpers as mh
    from test_integration_maintenance_stale_first import config

    ex, first, binding, proposal, check, verdict, ver, calls = await h.proposal_chain(
        s6_repo
    )
    try:
        success = await GuardedCommit(ex, ver.checks, ver).commit(verdict.reference())
    finally:
        await ver.close()
    account = first.account_id
    # Generate a second REAL sealed graph in the same tenant. Only the account UUID
    # allocation is fixed; all object identities remain unique, normal S4/S5 calls.
    original = mh.uuid4
    first_uuid = True

    def allocate():
        nonlocal first_uuid
        if first_uuid:
            first_uuid = False
            return UUID(account.removeprefix("s4-review-"))
        return original()

    with monkeypatch.context() as m:
        m.setattr(mh, "uuid4", allocate)
        same_account, source, report = await mh.make_graph(
            s6_repo, labels="Z", edges=()
        )
    assert same_account == account
    filler = uuid4()
    async with s6_repo.session(account) as db:
        refs = await ex.register_graph(
            db,
            report.reference(),
            config(),
            RequestContext(account_id=account, agent_id="system"),
        )
        watches = [await ex.artifacts.get(db, r) for r in refs]
        watch = next(w for w in watches if w.source == source)
        await db.execute(
            "INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content) VALUES($1,$2,$3,'memory','datalake','unrelated source')",
            filler,
            account,
            "ctx://extra/" + str(filler),
        )
        extra = await db.fetchval(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','external') RETURNING event_id",
            filler,
            account,
        )
        proof = await ex.artifacts.get(db, success.proof)
        if real_event:
            extra = proof.commit_event_id
            filler = proof.committed_target.context_id
        await db.execute(
            "INSERT INTO context_invalidations(context_id,cause_event_id,source_context_id,source_version,reason_hash) VALUES($1,$2,$3,$4,$5)",
            watch.affected[0].context_id,
            extra,
            filler,
            2 if real_event else 1,
            text_hash("s5:source_changed"),
        )
        await db.execute(
            "UPDATE contexts SET l2_content='new actual second root',version=version+1 WHERE id=$1",
            source.context_id,
        )
        event = await db.fetchval(
            "INSERT INTO change_events(context_id,account_id,change_type,actor) VALUES($1,$2,'modified','external') RETURNING event_id",
            source.context_id,
            account,
        )
        proof = await ex.artifacts.get(db, success.proof)
        # Public audit-only archive: no private grants, hash tampering, work mutation,
        # or fresh write. Claimed event is NOT the real successful commit event.
        cloned = type(proof)(
            **{**proof.model_dump(), "id": uuid4(), "commit_event_id": extra}
        )
        archived = (
            await ex.artifacts.archive_commit_proof(db, cloned)
            if forge
            else cloned.reference()
        )
        assert not await db.fetchval(
            "SELECT 1 FROM maintenance_work_items WHERE state->'proof'->>'id'=$1",
            str(archived.id),
        )
    refs = await ex.begin_maintenance(
        event, watch.reference(), execution_id=first.execution_id
    )
    async with s6_repo.session(account) as db:
        f = await ex.artifacts.get(db, refs[0])
    state, calls, *_ = await h.run_on_frame(
        ex, f, profile="full/strong-only", results=("updated",)
    )
    await export(
        ex,
        f,
        "archive-cause-authority-" + str(forge) + "-" + str(real_event),
        {
            "archived": archived.model_dump(mode="json"),
            "actual_upstream_event": str(proof.commit_event_id),
            "unrelated_event": str(extra),
            "state": state.model_dump(mode="json"),
            "calls": calls,
        },
    )
    assert state.status == "unresolved", (
        "An audit-only archived proof must not authorize an unrelated cause"
    )

    assert state.reason == "cause_resolution_not_authorized"
    await read_assert(ex, f, False)
    async with s6_repo.session(account) as db:
        assert (
            await db.fetchval(
                "SELECT count(*) FROM context_invalidations WHERE context_id=$1 AND resolved_at IS NULL",
                watch.affected[0].context_id,
            )
            == 2
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "updated",
        "unchanged",
        "FAIL",
        "UNKNOWN",
        "transport",
        "seal_first",
        "seal_second",
    ],
)
async def test_verifier_local_work_has_one_durable_owner(s6_repo, monkeypatch, mode):
    active = contextvars.ContextVar("s6_revision_verifier", default=False)
    observed = {"rows": 0, "bytes": 0, "writes": 0, "owners": set()}
    original_verify = FixedVerifier.verify

    async def verify(self, *args, **kwargs):
        token = active.set(True)
        try:
            return await original_verify(self, *args, **kwargs)
        finally:
            active.reset(token)

    monkeypatch.setattr(FixedVerifier, "verify", verify)
    fetch, fetchrow, execute = ScopedRepo.fetch, ScopedRepo.fetchrow, ScopedRepo.execute
    observe = MeteredDB._observe

    def relevant(sql):
        return "execution_attempts" not in sql and sql.lstrip().startswith("SELECT")

    def observe_actual(row):
        if row is not None:
            observed["rows"] += 1
            observed["bytes"] += len(
                json.dumps(dict(row), ensure_ascii=False, default=str).encode("utf-8")
            )

    async def counted_fetch(self, sql, *args):
        rows = await fetch(self, sql, *args)
        if active.get() and relevant(sql):
            for row in rows:
                observe_actual(row)
        return rows

    async def counted_row(self, sql, *args):
        row = await fetchrow(self, sql, *args)
        if active.get() and relevant(sql):
            observe_actual(row)
        return row

    async def counted_execute(self, sql, *args):
        result = await execute(self, sql, *args)
        if active.get() and sql.lstrip().startswith("INSERT INTO knowledge_artifacts"):
            observed["writes"] += 1
        return result

    def owner(self, row):
        if active.get() and row is not None:
            # The same durable owner covers bootstrap and later reads.
            identity = getattr(self.work, "identity", None)
            if identity:
                observed["owners"].add(identity.call_id)
        return observe(self, row)

    monkeypatch.setattr(ScopedRepo, "fetch", counted_fetch)
    monkeypatch.setattr(ScopedRepo, "fetchrow", counted_row)
    monkeypatch.setattr(ScopedRepo, "execute", counted_execute)
    monkeypatch.setattr(MeteredDB, "_observe", owner)
    ex, _, _, _, _, frames = await h.prepared(s6_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    original_seal = ex.artifacts.seal
    fail_kind = {
        "seal_first": "verification",
        "seal_second": "maintenance_verdict",
    }.get(mode)

    async def seal(db, obj):
        ref = await original_seal(db, obj)
        if active.get() and obj.kind == fail_kind:
            raise ContractError("synthetic_seal_failed_after_insert")
        return ref

    monkeypatch.setattr(ex.artifacts, "seal", seal)
    state, calls, *_ = await h.run_on_frame(
        ex,
        frame,
        profile="local/base-only",
        results=("unchanged" if mode == "unchanged" else "updated",),
        verifier_error=mode == "transport",
        verifier_results=(mode,) if mode in ("FAIL", "UNKNOWN") else (),
    )
    success = mode in ("updated", "unchanged")
    assert state.status == (mode if success else "unresolved")
    await read_assert(ex, frame, success)
    async with s6_repo.session(frame.account_id) as db:
        records = [
            r["payload"]
            for r in await db.fetch(
                "SELECT payload FROM execution_attempts WHERE execution_id=$1",
                frame.execution_id,
            )
        ]
        locals_ = [
            r
            for r in records
            if r["phase"] == "finished"
            and r["identity"]["operation"] == "verifier-local"
        ]
        assert len(locals_) == 1, (
            "verifier needs one local accounting owner, including failure"
        )
        local = locals_[0]
        counts = {v["metric"]: v["count"] for v in local["local_work"]}
        assert counts["retrieval_rows"] == observed["rows"] > 0
        assert counts["retrieval_bytes"] == observed["bytes"] > 0
        assert counts.get("writes", 0) == observed["writes"]
        assert counts["hard_checks"] == 7
        assert observed["owners"] == {
            __import__("uuid").UUID(local["identity"]["call_id"])
        }
        assert local["outcome"] == (
            "error" if mode in ("transport", "seal_first", "seal_second") else "success"
        )
        assert local["usage"]["input_tokens"] is None
        assert local["cost"]["amount"] is None
        model_records = [
            r
            for r in records
            if r["phase"] == "finished" and r["identity"]["model_role"] == "verifier"
        ]
        assert len(model_records) == (3 if mode == "transport" else 1)
        assert all(not r["local_work"] for r in model_records)
        # Rolled-back seals remain charged as performed local work, never as
        # committed artifacts or a usable submission grant.
        sealed = await db.fetchval(
            "SELECT count(*) FROM knowledge_artifacts WHERE kind IN ('verification','maintenance_verdict')"
        )
        assert sealed == (
            0 if mode in ("transport", "seal_first", "seal_second") else 2
        )
    await export(
        ex,
        frame,
        "local-work-" + mode,
        {"observed": observed, "state": state.model_dump(mode="json")},
    )


@pytest.mark.asyncio
async def test_actual_upstream_event_drift_after_verifier_blocks_commit(
    s6_repo, monkeypatch
):
    ex, frames = await h.small_chain(s6_repo)
    a, *_ = await h.run_on_frame(ex, frames["A"], results=("updated",))
    async with s6_repo.session(frames["A"].account_id) as db:
        proof = await ex.artifacts.get(db, a.proof)
    bref = await ex.continue_after_commits(frames["B"].reference(), (a.proof,))
    async with s6_repo.session(bref.account_id) as db:
        b = await ex.artifacts.get(db, bref)
    original = FixedVerifier.verify

    async def verify(self, *args, **kwargs):
        verdict = await original(self, *args, **kwargs)
        conn = await asyncpg.connect(h.DSN)
        await init_pg_connection(conn)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id',$1,true)", b.account_id
                )
                await conn.execute(
                    "UPDATE change_events SET metadata=metadata-'s6_proposal' WHERE event_id=$1",
                    proof.commit_event_id,
                )
        finally:
            await conn.close()
        return verdict

    monkeypatch.setattr(FixedVerifier, "verify", verify)
    result, calls, *_ = await h.run_on_frame(ex, b, results=("updated",))
    assert [c["model"] for c in calls] == ["base", "verifier"]
    assert (
        result.status == "unresolved"
        and result.reason == "cause_resolution_not_authorized"
    )
    await read_assert(ex, b, False)
