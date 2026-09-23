from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from contexthub.discovery.search import (
    SearchService,
    IndexSelection,
    ExplicitProvenance,
    HistoryEntry,
    SearchPolicy,
)
from contexthub.models.knowledge import ContractError
from discovery_helpers import (
    MemoryDB,
    source,
    response,
    extract,
    search_policy,
    history,
    pages,
    call_identity,
)
from knowledge_helpers import NOW, MemorySink


async def setup():
    db = MemoryDB()
    refs = tuple([await source(db, text=f"历史 {i}") for i in range(5)])
    text = "如果A变化，B和C也变化。"
    batch = await source(db, text=text)
    raw = response(batch, text)
    raw["propositions"] *= 3
    obj, sink, req = await extract(db, (batch,), [raw])
    return db, refs + (batch,), obj, sink, req


@pytest.mark.asyncio
async def test_two_arms_reuse_same_sealed_artifact_and_complete_batch():
    db, refs, obj, sink, requests = await setup()
    service = SearchService(recorder=sink)
    hist = history(refs)
    for strategy in ("full-history", "selective"):
        pol = search_policy(strategy=strategy, page_size=1)
        result = await pages(service, db, obj.reference(), hist, pol)
        assert all(p.artifact == obj.reference() for p in result)
        assert all(len(p.candidates) <= 1 for p in result)
        assert all(not p.truncated for p in result)
        assert all(not p.complete and p.cursor for p in result[:-1])
        assert result[-1].complete
        batch = [
            c for p in result for c in p.candidates if c.kind == "batch_proposition"
        ]
        assert {c.id for c in batch} == {p.id for p in obj.propositions}
        assert [c.text for c in batch] == [p.text for p in obj.propositions]
        if strategy == "full-history":
            assert set(result[-1].read_sources) == set(refs)
        else:
            assert result[-1].scope_sources == (refs[-1],)
    assert len(requests) == 1
    assert (
        len([r for r in sink.rows if r.requested_model and r.phase == "finished"]) == 1
    )
    local = [
        r for r in sink.rows if r.local_work and r.identity.operation != "extract_local"
    ]
    assert all(
        any(w.metric == "retrieval_pages" and w.count == 1 for w in r.local_work)
        for r in local
    )


class BrokenIndex:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    async def select(self, db, artifact, scope):
        self.calls.append(scope)
        if self.mode == "error":
            raise RuntimeError("index offline")
        if self.mode == "outside":
            return IndexSelection(
                sources=(scope[0].model_copy(update={"id": uuid4()}),),
                complete=True,
                truncated=False,
            )
        return IndexSelection(
            sources=scope[:-1],
            complete=self.mode == "silent_omission",
            truncated=self.mode == "truncated",
            issues=(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["error", "truncated", "incomplete", "silent_omission", "outside"]
)
async def test_faults_expand_then_direct_read_all_despite_high_price(mode):
    db, refs, obj, sink, _ = await setup()
    index = BrokenIndex(mode)
    result = await pages(
        SearchService(recorder=sink, index=index),
        db,
        obj.reference(),
        history(refs),
        search_policy(),
    )
    assert result[-1].complete and result[-1].at_max_scope
    assert set(result[-1].read_sources) == set(refs)
    assert result[-1].expansion_reasons and not result[-1].issues
    sizes = {len(s) for s in index.calls}
    assert 1 in sizes and len(refs) in sizes
    candidates = [c.id for p in result for c in p.candidates]
    assert len(candidates) == len(set(candidates))


@pytest.mark.asyncio
async def test_persistent_read_failure_reaches_max_and_stops_incomplete():
    db, refs, obj, sink, _ = await setup()
    # Last source is required by the current batch, so local starts with failure.
    failed = await SearchService(recorder=sink).artifacts.get(db, refs[-1])
    del db.versions[failed.source.context_id, failed.source.version]
    result = await pages(
        SearchService(recorder=sink),
        db,
        obj.reference(),
        history(refs),
        search_policy(),
    )
    assert (
        not result[-1].complete
        and result[-1].at_max_scope
        and result[-1].cursor is None
    )
    assert "source_version_missing" in result[-1].expansion_reasons
    assert refs[-1] not in result[-1].read_sources
    assert any(i.code == "source_version_missing" for i in result[-1].issues)


@pytest.mark.asyncio
async def test_transient_source_failure_is_retried_and_batch_not_lost():
    db, refs, obj, sink, _ = await setup()
    original = db.fetchrow
    snapshot = await SearchService(recorder=sink).artifacts.get(db, refs[-1])
    attempts = 0

    async def read(sql, *args):
        nonlocal attempts
        if "context_versions" in sql and args[1] == snapshot.source.context_id:
            attempts += 1
            if attempts <= 4:
                return None
        return await original(sql, *args)

    db.fetchrow = read
    result = await pages(
        SearchService(recorder=sink),
        db,
        obj.reference(),
        history(refs),
        search_policy(),
    )
    assert result[-1].complete
    batch = [c for p in result for c in p.candidates if c.kind == "batch_proposition"]
    assert {c.id for c in batch} == {p.id for p in obj.propositions}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinal", "timestamp", "unlisted", "wrong_version"])
async def test_future_and_unlisted_provenance_are_reported_never_read(mode):
    db, refs, obj, sink, _ = await setup()
    future = await source(
        db,
        "未来会话不可见",
        created=NOW + timedelta(days=1) if mode == "timestamp" else NOW,
    )
    hist = history(refs)
    if mode != "unlisted":
        ordinal = 100 if mode == "ordinal" else 0
        hist = hist.model_copy(
            update={
                "entries": hist.entries
                + (HistoryEntry(source=future, ingestion_ordinal=ordinal),)
            }
        )
    if mode == "wrong_version":
        # A different seal/version isn't the allowed source, even for same context.
        future = future.model_copy(update={"sha256": "0" * 64})
    hist = hist.model_copy(
        update={
            "provenance": (
                ExplicitProvenance(
                    proposition_id=obj.propositions[0].id,
                    source=future,
                    reason="explicit producer",
                ),
            )
        }
    )
    snap = db.artifacts[future.id]["payload"]["source"]
    db.reads.clear()
    result = await pages(
        SearchService(recorder=sink), db, obj.reference(), hist, search_policy()
    )
    assert not result[-1].complete and result[-1].issues
    if mode != "wrong_version":
        assert not any(str(cid) == snap["context_id"] for cid, _ in db.reads)
    assert any(i.source == future for i in result[-1].issues)
    batch = [
        c
        for p in result
        for c in p.candidates
        if c.proposition_id == obj.propositions[0].id
    ]
    assert batch[0].explicit_provenance == hist.provenance


@pytest.mark.asyncio
async def test_explicit_provenance_forces_older_source_at_local_scope():
    db, refs, obj, sink, _ = await setup()
    prov = ExplicitProvenance(
        proposition_id=obj.propositions[0].id,
        source=refs[0],
        reason="provided by caller",
    )
    result = await pages(
        SearchService(recorder=sink),
        db,
        obj.reference(),
        history(refs, provenance=(prov,)),
        search_policy(),
    )
    assert result[-1].complete and refs[0] in result[-1].read_sources
    assert len(result[-1].scope_sources) == 2
    assert any(prov in c.explicit_provenance for p in result for c in p.candidates)


@pytest.mark.asyncio
async def test_cursor_binds_history_policy_and_artifact():
    db, refs, obj, sink, _ = await setup()
    service, hist, pol = (
        SearchService(recorder=sink),
        history(refs),
        search_policy(page_size=1),
    )
    page = await service.search(
        db, obj.reference(), hist, pol, identity=call_identity(pol)
    )
    with pytest.raises(ContractError, match="cursor_scope_mismatch"):
        await service.search(
            db,
            obj.reference(),
            hist.model_copy(update={"cutoff_ordinal": 100}),
            pol,
            identity=call_identity(pol),
            cursor=page.cursor,
        )


@pytest.mark.asyncio
async def test_pending_extraction_never_complete_search():
    db = MemoryDB()
    ref = await source(db)
    obj, sink, _ = await extract(db, (ref,), ["invalid"])
    result = await pages(
        SearchService(recorder=sink),
        db,
        obj.reference(),
        history((ref,)),
        search_policy(),
    )
    assert not result[-1].complete and result[-1].issues[0].code == "extraction_pending"


def test_explicit_policy_load_and_validation(tmp_path):
    path = tmp_path / "search.json"
    path.write_text(search_policy().model_dump_json())
    assert SearchPolicy.load(path) == search_policy()
    with pytest.raises(ValidationError):
        SearchPolicy.model_validate({})
    with pytest.raises(ValidationError):
        search_policy(expansion_factor=1)


@pytest.mark.asyncio
async def test_explicit_version_manifest_does_not_read_latest_version():
    db = MemoryDB()
    old = await source(db, "v1 原政策")
    snap = await SearchService(recorder=MemorySink()).artifacts.get(db, old)
    new = await source(db, "v2 后来政策", context_id=snap.source.context_id, version=2)
    obj, sink, _ = await extract(db, (old,), [response(old, "v1 原政策")])
    db.reads.clear()
    result = await pages(
        SearchService(recorder=sink),
        db,
        obj.reference(),
        history((old,)),
        search_policy(strategy="full-history"),
    )
    assert result[-1].complete
    assert all(version == 1 for _, version in db.reads)
    assert all(new not in c.sources for p in result for c in p.candidates)


@pytest.mark.asyncio
async def test_empty_complete_history_and_exact_cutoff_are_valid():
    db = MemoryDB()
    ref = await source(db, "")
    obj, sink, _ = await extract(db, (ref,), [{"propositions": []}])
    result = await pages(
        SearchService(recorder=sink),
        db,
        obj.reference(),
        history((ref,), cutoff_at=NOW, cutoff_ordinal=0),
        search_policy(),
    )
    assert result[-1].complete and len(result[-1].candidates) == 1
    assert result[-1].candidates[0].text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["full-history", "selective"])
@pytest.mark.parametrize("page_size", [1, 50])
@pytest.mark.parametrize("future", [False, True])
@pytest.mark.parametrize("batch_source", [False, True])
async def test_metadata_recovery_rechecks_boundary_and_preserves_pagination(
    strategy, page_size, future, batch_source
):
    from contexthub.discovery.search import SearchCursor
    from contexthub.services.artifact_service import ArtifactService

    db = MemoryDB()
    oldest = await source(db, "更早历史", created=NOW - timedelta(days=2))
    old = await source(db, "允许的历史", created=NOW - timedelta(days=1))
    target = await source(
        db, "恢复读取的来源", created=NOW + timedelta(days=1) if future else NOW
    )
    snapshot = await ArtifactService().get(db, target)
    batch = target if batch_source else old
    text = "恢复读取的来源" if batch_source else "允许的历史"
    raw = response(batch, text)
    raw["propositions"] *= 2
    obj, sink, requests = await extract(db, (batch,), [raw])
    original, failed = db.fetchrow, False

    async def fail_once(sql, *args):
        nonlocal failed
        if "knowledge_artifacts" in sql and args[1] == target.id and not failed:
            failed = True
            raise TimeoutError("metadata unavailable before query dispatch")
        return await original(sql, *args)

    db.fetchrow = fail_once
    db.reads.clear()
    hist = history((oldest, old, target))
    pol = search_policy(strategy=strategy, page_size=page_size, lambda_p1=1e100)
    service, result, cursor = SearchService(recorder=sink), [], None
    for _ in range(50):
        page = await service.search(
            db, obj.reference(), hist, pol, identity=call_identity(pol), cursor=cursor
        )
        result.append(page)
        if page.cursor is None:
            break
        cursor = SearchCursor.model_validate_json(page.cursor.model_dump_json())
    else:
        pytest.fail("metadata recovery did not terminate")
    assert failed and len(requests) == 1
    assert all(p.artifact == obj.reference() for p in result)
    assert any(
        i.code == "TimeoutError" and i.source == target for i in result[0].issues
    )
    assert "source_metadata_read_failed" in result[-1].expansion_reasons
    if future or strategy == "full-history":
        assert result[-1].at_max_scope
    candidates = [c for p in result for c in p.candidates]
    assert len({c.id for c in candidates}) == len(candidates)
    if future:
        assert (snapshot.source.context_id, snapshot.source.version) not in db.reads
        assert all(target not in c.sources for c in candidates)
        assert all(
            target not in p.allowed_sources and target not in p.read_sources
            for p in result
        )
        assert not result[-1].complete
        assert any(
            i.code == "source_after_cutoff" and i.source == target
            for i in result[-1].issues
        )
    else:
        assert result[-1].complete and not result[-1].issues
        assert set(result[-1].read_sources) == set(result[-1].scope_sources)
        assert {old, target} <= set(result[-1].read_sources)
        assert {c.id for c in candidates if c.kind == "batch_proposition"} == {
            p.id for p in obj.propositions
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["full-history", "selective"])
async def test_persistent_metadata_failure_stops_without_body_or_false_complete(
    strategy,
):
    db, refs, obj, sink, requests = await setup()
    original = db.fetchrow
    snapshot = await SearchService(recorder=sink).artifacts.get(db, refs[-1])

    async def unreadable(sql, *args):
        if "knowledge_artifacts" in sql and args[1] == refs[-1].id:
            raise TimeoutError("metadata unavailable")
        return await original(sql, *args)

    db.fetchrow = unreadable
    db.reads.clear()
    result = await pages(
        SearchService(recorder=sink),
        db,
        obj.reference(),
        history(refs),
        search_policy(strategy=strategy, page_size=1),
    )
    assert not result[-1].complete and result[-1].at_max_scope
    assert any(
        i.code == "TimeoutError" and i.source == refs[-1] for i in result[-1].issues
    )
    assert (snapshot.source.context_id, snapshot.source.version) not in db.reads
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_source", [False, True])
async def test_index_never_receives_unadmitted_metadata_recovery_source(batch_source):
    db, refs, obj, sink, _ = await setup()
    future = await source(db, "未来正文", created=NOW + timedelta(days=1))
    if batch_source:
        obj, sink, _ = await extract(db, (future,), [response(future, "未来正文")])
    original, failed = db.fetchrow, False

    async def fail_once(sql, *args):
        nonlocal failed
        if "knowledge_artifacts" in sql and args[1] == future.id and not failed:
            failed = True
            raise TimeoutError("metadata unavailable")
        return await original(sql, *args)

    class ReadingIndex:
        async def select(self, db, artifact, scope):
            assert future not in scope
            # Actual source reads behind this adapter must also remain in scope.
            service = SearchService(recorder=sink)
            for ref in scope:
                await service.evidence.read_source(
                    db, await service.artifacts.get(db, ref)
                )
            return IndexSelection(sources=scope, complete=True, truncated=False)

    db.fetchrow = fail_once
    db.reads.clear()
    result = await pages(
        SearchService(recorder=sink, index=ReadingIndex()),
        db,
        obj.reference(),
        history((*refs, future)),
        search_policy(page_size=1),
    )
    assert failed and not result[-1].complete
    assert any(
        i.code == "source_after_cutoff" and i.source == future
        for i in result[-1].issues
    )

@pytest.mark.asyncio
@pytest.mark.parametrize('future',[False,True])
async def test_s4_explicit_read_metadata_recovery_readmits(future):
    from datetime import timedelta
    from knowledge_helpers import NOW, MemorySink
    from discovery_helpers import MemoryDB, source, history
    from contexthub.models.knowledge import ContractError
    db=MemoryDB(); ref=await source(db,'secret' if future else 'allowed',created=NOW+timedelta(days=1) if future else NOW)
    hist=history((ref,),cutoff_at=NOW)
    base=db.fetchrow; failed=False
    async def fetch(sql,*args):
        nonlocal failed
        if 'knowledge_artifacts' in sql and not failed:
            failed=True; raise TimeoutError()
        return await base(sql,*args)
    db.fetchrow=fetch; db.reads.clear()
    core=SearchService(recorder=MemorySink())
    if future:
        with pytest.raises(ContractError,match='source_after_cutoff'):
            await core.read_evidence(db,(ref,),hist)
        assert not db.reads
    else:
        out=await core.read_evidence(db,(ref,),hist)
        assert out.evidence[0].excerpt=='allowed' and out.recoveries[0].code=='TimeoutError'
