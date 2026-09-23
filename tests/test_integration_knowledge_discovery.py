"""Real S3/S2 storage and ledger. Reuses only the explicitly isolated S2 fixture."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from contexthub.discovery.extraction import ExtractionService
from contexthub.discovery.search import SearchService
from contexthub.models.knowledge import PropositionArtifact, ContractError
from contexthub.services.artifact_service import ArtifactService
from contexthub.services.evidence_service import EvidenceService
from contexthub.services.execution_ledger import ExecutionLedger
from discovery_helpers import (
    config,
    response,
    transport,
    call_identity,
    history,
    search_policy,
    pages,
)
from test_integration_knowledge_storage import s2_repo as isolated_repo, seed

s2_repo = isolated_repo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["normal", "empty", "malformed", "bad_span", "exhausted", "missing_source"]
)
async def test_s3_real_storage_and_attempts(s2_repo, mode):
    account = "s3-" + str(uuid4())
    artifacts = ArtifactService()
    ledger = ExecutionLedger(s2_repo)
    now = datetime.now(timezone.utc) + timedelta(seconds=2)
    async with s2_repo.session(account) as db:
        first, second, _ = await seed(db, account)
        refs = tuple(
            [
                await EvidenceService().snapshot(
                    db,
                    account_id=account,
                    source=cv,
                    content_level="l2_content",
                    cutoff_at=now,
                )
                for cv in (first, second)
            ]
        )
    cfg, requests = config(), []
    raw = response(refs[0], "政策：上限500元。")
    raw["propositions"] += response(refs[1], "450元可以报销。")["propositions"]
    if mode == "empty":
        raw = {"propositions": []}
    elif mode == "malformed":
        raw = "broken JSON"
    elif mode == "bad_span":
        raw["propositions"][0]["evidence"][0]["start"] = 1
    outputs = [(503, raw)] if mode == "exhausted" else [raw]
    if mode == "missing_source":
        refs = (refs[0].model_copy(update={"id": uuid4()}), refs[1])
    ident = call_identity(cfg, account_id=account)
    core = ExtractionService(
        cfg, api_key="fixture", recorder=ledger, transport=transport(outputs, requests)
    )
    try:
        async with s2_repo.session(account) as db:
            obj = await core.extract(db, refs, identity=ident)
    finally:
        await core.close()
    assert isinstance(obj, PropositionArtifact)
    assert obj.status == ("complete" if mode in ("normal", "empty") else "pending")
    async with s2_repo.session(account) as db:
        assert await artifacts.get(db, obj.reference()) == obj
        assert (
            await db.fetchval(
                "SELECT count(*) FROM dependencies d JOIN contexts c ON c.id=d.dependent_id WHERE c.account_id=$1",
                account,
            )
            == 0
        )
    recorded = await ledger.records(account, ident.call_id)
    attempts = 3 if mode == "exhausted" else 0 if mode == "missing_source" else 1
    assert len(requests) == attempts and len(recorded) == attempts * 2
    assert all(
        (r.usage.input_tokens, r.usage.output_tokens) == (13, 7)
        for r in recorded
        if r.phase == "finished"
    )
    hist = history(refs, cutoff_at=now)
    for strategy in ("full-history", "selective"):
        async with s2_repo.session(account) as db:
            result = await pages(
                SearchService(recorder=ledger),
                db,
                obj.reference(),
                hist,
                search_policy(strategy=strategy, page_size=1),
            )
        assert all(p.artifact == obj.reference() for p in result)
        assert result[-1].complete == (mode in ("normal", "empty"))
        if mode == "normal":
            candidates = [
                c for p in result for c in p.candidates if c.kind == "batch_proposition"
            ]
            assert {c.id for c in candidates} == {p.id for p in obj.propositions}
    assert len(requests) == attempts  # Search owns no model client.
    async with s2_repo.session(account) as db:
        rows = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE account_id=$1", account
        )
        assert any(
            any(w["metric"] == "retrieval_pages" for w in r["payload"]["local_work"])
            for r in rows
        )
        assert any(
            any(
                w["metric"] == "retrieval_bytes" and w["count"] > 0
                for w in r["payload"]["local_work"]
            )
            for r in rows
        )


@pytest.mark.asyncio
async def test_s3_business_rollback_does_not_erase_call_cost(s2_repo):
    account = "s3-" + str(uuid4())
    now = datetime.now(timezone.utc) + timedelta(seconds=2)
    async with s2_repo.session(account) as db:
        first, _, _ = await seed(db, account)
        ref = await EvidenceService().snapshot(
            db,
            account_id=account,
            source=first,
            content_level="l2_content",
            cutoff_at=now,
        )
    ledger, cfg, req = ExecutionLedger(s2_repo), config(), []
    ident = call_identity(cfg, account_id=account)
    core = ExtractionService(
        cfg,
        api_key="fixture",
        recorder=ledger,
        transport=transport([{"propositions": []}], req),
    )
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            async with s2_repo.session(account) as db:
                obj = await core.extract(db, (ref,), identity=ident)
                raise RuntimeError("rollback")
    finally:
        await core.close()
    async with s2_repo.session(account) as db:
        with pytest.raises(ContractError, match="artifact_missing"):
            await ArtifactService().get(db, obj.reference())
    assert len(await ledger.records(account, ident.call_id)) == 2


@pytest.mark.asyncio
async def test_s3_database_expansion_and_persistent_read_error(s2_repo):
    from contexthub.discovery.search import IndexSelection
    from contexthub.models.knowledge import ContextVersion

    account = "s3-" + str(uuid4())
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=2)
    refs = []
    async with s2_repo.session(account) as db:
        for i in range(5):
            ident = uuid4()
            await db.execute(
                """INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content)
                VALUES($1,$2,$3,'memory','user',$4)""",
                ident,
                account,
                "ctx://" + str(ident),
                "来源 " + str(i),
            )
            refs.append(
                await EvidenceService().snapshot(
                    db,
                    account_id=account,
                    source=ContextVersion(context_id=ident, version=1),
                    content_level="l2_content",
                    cutoff_at=cutoff,
                )
            )
    cfg, req, ledger = config(), [], ExecutionLedger(s2_repo)
    core = ExtractionService(
        cfg,
        api_key="fixture",
        recorder=ledger,
        transport=transport([response(refs[-1], "来源 4")], req),
    )
    try:
        async with s2_repo.session(account) as db:
            obj = await core.extract(
                db, (refs[-1],), identity=call_identity(cfg, account_id=account)
            )
    finally:
        await core.close()

    class TruncatedIndex:
        async def select(self, db, artifact, scope):
            return IndexSelection(sources=scope[:-1], complete=False, truncated=True)

    hist = history(tuple(refs), cutoff_at=cutoff)
    async with s2_repo.session(account) as db:
        result = await pages(
            SearchService(recorder=ledger, index=TruncatedIndex()),
            db,
            obj.reference(),
            hist,
            search_policy(page_size=1),
        )
        assert result[-1].complete and result[-1].at_max_scope
        assert set(result[-1].read_sources) == set(refs)
        assert "index_truncated" in result[-1].expansion_reasons
        # Make only synthetic fixture content unavailable, after successful seal.
        src = await ArtifactService().get(db, refs[-1])
        await db.execute(
            "UPDATE context_versions SET l2_content=NULL WHERE context_id=$1 AND version=$2",
            src.source.context_id,
            src.source.version,
        )
    async with s2_repo.session(account) as db:
        result = await pages(
            SearchService(recorder=ledger),
            db,
            obj.reference(),
            hist,
            search_policy(page_size=1),
        )
        assert (
            not result[-1].complete
            and result[-1].at_max_scope
            and result[-1].cursor is None
        )
        assert "source_content_missing" in result[-1].expansion_reasons
        assert any(i.code == "source_content_missing" for i in result[-1].issues)
    assert len(req) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["full-history", "selective"])
@pytest.mark.parametrize("page_size", [1, 50])
@pytest.mark.parametrize("future", [False, True])
async def test_s3_metadata_recovery_obeys_real_history_cutoff(
    s2_repo, strategy, page_size, future
):
    from contexthub.discovery.search import SearchCursor

    account = "s3-r1-" + str(uuid4())
    ledger, cfg, requests = ExecutionLedger(s2_repo), config(), []
    async with s2_repo.session(account) as db:
        old_cv, _, _ = await seed(db, account)
        old = await EvidenceService().snapshot(
            db,
            account_id=account,
            source=old_cv,
            content_level="l2_content",
            cutoff_at=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
    boundary = datetime.now(timezone.utc)
    async with s2_repo.session(account) as db:
        target_cv, _, _ = await seed(db, account)
        target = await EvidenceService().snapshot(
            db,
            account_id=account,
            source=target_cv,
            content_level="l2_content",
            cutoff_at=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
        snap = await ArtifactService().get(db, target)
        assert snap.ingested_at > boundary
    if not future:
        boundary = datetime.now(timezone.utc) + timedelta(seconds=2)
    core = ExtractionService(
        cfg,
        api_key="fixture",
        recorder=ledger,
        transport=transport([response(old, "政策：上限500元。")], requests),
    )
    try:
        async with s2_repo.session(account) as db:
            obj = await core.extract(
                db, (old,), identity=call_identity(cfg, account_id=account)
            )
    finally:
        await core.close()

    class OneFault:
        def __init__(self, db):
            self.db, self.failed, self.body_reads = db, False, []

        async def fetchval(self, sql, *args):
            return await self.db.fetchval(sql, *args)

        async def fetchrow(self, sql, *args):
            if (
                "knowledge_artifacts" in sql
                and args[1] == target.id
                and not self.failed
            ):
                self.failed = True
                raise TimeoutError("controlled metadata fault before query dispatch")
            if "context_versions" in sql:
                self.body_reads.append((args[1], args[2]))
            return await self.db.fetchrow(sql, *args)

    async with s2_repo.session(account) as db:
        adapter = OneFault(db)
        hist = history((old, target), cutoff_at=boundary)
        pol = search_policy(strategy=strategy, page_size=page_size, lambda_p1=1e100)
        svc, result, cursor = SearchService(recorder=ledger), [], None
        for _ in range(30):
            page = await svc.search(
                adapter,
                obj.reference(),
                hist,
                pol,
                identity=call_identity(pol, account_id=account),
                cursor=cursor,
            )
            result.append(page)
            if page.cursor is None:
                break
            cursor = SearchCursor.model_validate_json(page.cursor.model_dump_json())
        else:
            pytest.fail("metadata recovery did not terminate")
        assert adapter.failed and len(requests) == 1
        assert all(p.artifact == obj.reference() for p in result)
        assert any(
            i.code == "TimeoutError" and i.source == target for i in result[0].issues
        )
        assert "source_metadata_read_failed" in result[-1].expansion_reasons
        assert result[-1].complete is (not future)
        if future:
            assert (target_cv.context_id, target_cv.version) not in adapter.body_reads
            assert all(target not in c.sources for p in result for c in p.candidates)
            assert all(
                target not in p.read_sources and target not in p.allowed_sources
                for p in result
            )
            assert any(
                i.code == "source_after_cutoff" and i.source == target
                for i in result[-1].issues
            )
        else:
            assert set(result[-1].read_sources) == {old, target}
            assert {
                c.id
                for p in result
                for c in p.candidates
                if c.kind == "batch_proposition"
            } == {p.id for p in obj.propositions}
        assert (
            await db.fetchval(
                "SELECT count(*) FROM dependencies d JOIN contexts c ON c.id=d.dependent_id WHERE c.account_id=$1",
                account,
            )
            == 0
        )
