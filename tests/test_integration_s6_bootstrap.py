"""S6-R3: bootstrap work survives read/parse/cancel and physical SQL failure."""

import asyncio
import contextvars
import json

import asyncpg
import pytest
import test_integration_maintenance_commit as h
from test_integration_s6_revision import export, read_assert
from contexthub.db.repository import ScopedRepo
from contexthub.discovery._work import MeteredDB
from contexthub.models.knowledge import ContractError
from contexthub.propagation.verifier import FixedVerifier

s6_repo = h.s6_repo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,index",
    [("read", n) for n in (1, 2, 3, 4)]
    + [("parse", n) for n in (1, 2, 3)]
    + [("physical", 3), ("timeout", 3), ("identity", 3)],
)
async def test_bootstrap_work_is_durable(s6_repo, monkeypatch, fault, index):
    ex, _, _, _, _, frames = await h.prepared(s6_repo)
    frame = next(f for f in frames if not f.expected_upstreams)
    active = contextvars.ContextVar("bootstrap_verifier", default=False)
    observed = {"rows": 0, "bytes": 0, "attempts": 0, "owners": set()}
    original_verify, fetchrow = FixedVerifier.verify, ScopedRepo.fetchrow
    original_get, observe = ex.artifacts.get, MeteredDB._observe
    invocations = []

    async def verify(self, *args, **kwargs):
        token = active.set(True)
        invocations.append(self)
        if fault == "identity":
            # Valid same-account identity hint, but not the sealed action.
            # It must never authorize a verifier HTTP call or grant.
            kwargs["binding"] = kwargs["binding"].model_copy(
                update={"profile": "full/strong-only"}
            )
        try:
            async with asyncio.timeout(0.1 if fault == "timeout" else 10):
                return await original_verify(self, *args, **kwargs)
        finally:
            active.reset(token)

    async def read(self, sql, *args):
        relevant = active.get() and sql.startswith(
            "SELECT payload, sha256 FROM knowledge_artifacts"
        )
        if relevant:
            observed["attempts"] += 1
            if observed["attempts"] == index:
                if fault == "read":
                    raise asyncpg.ConnectionDoesNotExistError("synthetic read loss")
                if fault == "timeout":
                    await asyncio.Event().wait()
                if fault == "physical":
                    observed["victim_pid"] = self._conn.get_server_pid()
                    killer = await asyncpg.connect(h.DSN)
                    try:
                        observed["killer_pid"] = killer.get_server_pid()
                        observed["killer_role"] = await killer.fetchval(
                            "SELECT current_user"
                        )
                        assert observed["victim_pid"] != observed["killer_pid"]
                        observed["terminated"] = await killer.fetchval(
                            "SELECT pg_terminate_backend($1)", observed["victim_pid"]
                        )
                        assert observed["terminated"]
                        await asyncio.sleep(0.02)
                    finally:
                        await killer.close()
        try:
            row = await fetchrow(self, sql, *args)
        except Exception as exc:
            if relevant:
                observed["read_exception"] = type(exc).__name__
            raise
        if relevant and row is not None:
            observed["rows"] += 1
            observed["bytes"] += len(
                json.dumps(dict(row), ensure_ascii=False, default=str).encode("utf-8")
            )
        return row

    async def get(db, ref):
        result = await original_get(db, ref)
        if active.get() and fault == "parse" and observed["attempts"] == index:
            # Completed real read followed by deterministic decode/contract error.
            raise ContractError("synthetic_bootstrap_parse_failed")
        return result

    def owner(self, row):
        if active.get() and row is not None:
            identity = getattr(self.work, "identity", None)
            if identity:
                observed["owners"].add(str(identity.call_id))
        return observe(self, row)

    monkeypatch.setattr(FixedVerifier, "verify", verify)
    monkeypatch.setattr(ScopedRepo, "fetchrow", read)
    monkeypatch.setattr(ex.artifacts, "get", get)
    monkeypatch.setattr(MeteredDB, "_observe", owner)
    state, calls, *_ = await h.run_on_frame(
        ex, frame, profile="adaptive", results=("updated",)
    )
    assert state.status == "unresolved" and state.reason == "verifier_unavailable"
    assert state.proof is None and [x["model"] for x in calls] == ["base"]
    assert len(invocations) == 1 and not invocations[0]._grants
    await read_assert(ex, frame, False)
    async with s6_repo.session(frame.account_id) as db:
        records = [
            r["payload"]
            for r in await db.fetch(
                "SELECT payload FROM execution_attempts WHERE execution_id=$1",
                frame.execution_id,
            )
        ]
        snap = await ex.artifacts.get(db, frame.snapshot)
        assert (
            await db.fetchval(
                "SELECT version FROM contexts WHERE id=$1", snap.target.context_id
            )
            == 1
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM context_invalidations WHERE context_id=$1 AND resolved_at IS NULL",
                snap.target.context_id,
            )
            == 1
        )
        assert (
            await ex.artifacts.get(db, frame.reference())
        ).deadline_at == frame.deadline_at
    local = [r for r in records if r["identity"]["operation"] == "verifier-local"]
    observed["owners"] = sorted(observed["owners"])
    await export(
        ex, frame, f"bootstrap-{fault}-{index}", {"observed": observed, "local": local}
    )
    assert len(local) == 2 and {r["phase"] for r in local} == {"started", "finished"}
    done = next(r for r in local if r["phase"] == "finished")
    counts = {r["metric"]: r["count"] for r in done["local_work"]}
    assert (
        counts.get("retrieval_rows", 0)
        == observed["rows"]
        == (index if fault in ("parse", "identity") else index - 1)
    )
    assert counts.get("retrieval_bytes", 0) == observed["bytes"]
    # A buffered implementation can also be correct if it durably transfers
    # every count. Do not require a particular owner construction time here.
    assert set(observed["owners"]) <= {done["identity"]["call_id"]}
    assert done["identity"]["execution_id"] == str(frame.execution_id)
    assert done["identity"]["event_id"] == str(frame.event_id)
    assert done["identity"]["account_id"] == frame.account_id
    assert done["identity"]["work_item_id"] == str(frame.snapshot.id)
    assert done["outcome"] == "error" and done["attempt_no"] == 1
    assert done["usage"]["input_tokens"] is None and done["cost"]["amount"] is None
    if fault == "physical":
        assert observed["read_exception"] in (
            "InterfaceError",
            "ConnectionDoesNotExistError",
        )
    if fault == "timeout":
        assert done["error"]["code"] == "verifier_cancelled"
