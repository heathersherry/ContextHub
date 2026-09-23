import pytest
from contexthub.discovery.service import DiscoveryService, DiscoveryConfig
from contexthub.discovery.search import SearchService
from contexthub.discovery.adjudication import AdjudicationService
from contexthub.services.artifact_service import ArtifactService
from discovery_helpers import (
    source,
    extract,
    response,
    history,
    search_policy,
    call_identity,
)
from s4_helpers import NodeDB, adjudication_config, judgment, judge_transport


async def flow(mode="normal", page_size=1, price=10):
    db = NodeDB()
    a = await source(db, "A.")
    b = await source(db, "B.")
    raw = response(a, "A.")
    raw["propositions"] += response(b, "B.")["propositions"]
    artifact, sink, _ = await extract(db, (a, b), [raw])
    hist = history((a, b))
    req = []
    cfg = DiscoveryConfig(
        timeout_seconds=30,
        search=search_policy(page_size=page_size, lambda_p1=price),
        adjudication=adjudication_config(lambda_p1=price),
    )

    def respond(p, n):
        if mode == "missing":
            return judgment(p, missing=["missing rule"])
        if mode == "cycle":
            return judgment(p)
        if mode == "invalid":
            return judgment(p, citations=[], steps=[])
        return judgment(p, "DEPENDS" if p["left"] == "A." else "INDEPENDENT")

    adj = AdjudicationService(
        cfg.adjudication,
        api_key="fixture",
        recorder=sink,
        transport=judge_transport(req, respond),
    )
    core = DiscoveryService(
        cfg, search=SearchService(recorder=sink), adjudicator=adj, recorder=sink
    )
    try:
        report = await core.discover(
            db, artifact.reference(), hist, identity=call_identity(cfg)
        )
    finally:
        await adj.close()
    graph = await ArtifactService().get(db, report.graph)
    return report, graph, req, db, sink


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [1, 2, 20])
async def test_full_batch_paginated_real_core_chain(page_size):
    report, graph, req, db, sink = await flow(page_size=page_size)
    assert report.status == "complete" and len(graph.edges) >= 1
    assert len({m.node for m in report.mappings if m.proposition_id}) == 2
    assert await ArtifactService().get(db, report.reference()) == report
    assert len(req) >= 4
    assert any(r.local_work for r in sink.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "cycle", "invalid"])
async def test_pending_not_negative_or_success(mode):
    report, graph, *_ = await flow(mode)
    assert report.status == "pending" and report.reasons
    assert graph.pending and not graph.edges


@pytest.mark.asyncio
async def test_adjudication_expansion_restarts_same_evidence_round():
    db = NodeDB()
    older = await source(db, "Rule R.")
    current = await source(db, "A.")
    artifact, sink, _ = await extract(db, (current,), [response(current, "A.")])
    hist = history((older, current))
    req = []
    cfg = DiscoveryConfig(
        timeout_seconds=30,
        search=search_policy(initial_sources=1, lambda_p1=10),
        adjudication=adjudication_config(),
    )
    # Create two propositions in incoming batch so local scope has a candidate pair.
    raw = response(current, "A.")
    raw["propositions"] *= 2
    artifact, sink, _ = await extract(db, (current,), [raw], sink=sink)

    def respond(p, n):
        if not any(e["excerpt"] == "Rule R." for e in p["evidence"]):
            return judgment(p, missing=["Rule R"])
        return judgment(p, "INDEPENDENT")

    adj = AdjudicationService(
        cfg.adjudication,
        api_key="fixture",
        recorder=sink,
        transport=judge_transport(req, respond),
    )
    core = DiscoveryService(
        cfg, search=SearchService(recorder=sink), adjudicator=adj, recorder=sink
    )
    try:
        report = await core.discover(
            db, artifact.reference(), hist, identity=call_identity(cfg)
        )
    finally:
        await adj.close()
    assert report.status == "complete" and report.pages[-1].at_max_scope
    assert req[0]["role"] == "normal" and req[1]["role"] == "normal"
    assert req[0]["evidence_sha256"] != req[1]["evidence_sha256"]
    assert all(
        d.result.generation == 1
        for d in report.decisions
        if d.result.action == "complete"
    )


@pytest.mark.asyncio
async def test_compatibility_entry_runs_only_s4():
    from contexthub.services.dependency_discovery_service import (
        DependencyDiscoveryService,
    )

    class OldChat:
        async def complete(self, *a, **k):
            raise AssertionError("legacy must not run")

    db = NodeDB()
    ref = await source(db, "A.")
    artifact, sink, _ = await extract(db, (ref,), [response(ref, "A.")])
    cfg = DiscoveryConfig(
        timeout_seconds=30,
        search=search_policy(lambda_p1=10),
        adjudication=adjudication_config(),
    )
    adj = AdjudicationService(
        cfg.adjudication,
        api_key="fixture",
        recorder=sink,
        transport=judge_transport([]),
    )
    core = DiscoveryService(
        cfg, search=SearchService(recorder=sink), adjudicator=adj, recorder=sink
    )
    try:
        report = await DependencyDiscoveryService(OldChat()).discover_snapshot(
            db,
            artifact.reference(),
            history((ref,)),
            core=core,
            identity=call_identity(cfg),
        )
    finally:
        await adj.close()
    assert report.status == "complete"


@pytest.mark.asyncio
async def test_nested_local_work_has_one_owner():
    from contexthub.discovery._work import MeteredDB

    class Counter:
        def __init__(self):
            self.counts = {}

        def add(self, k, n=1):
            self.counts[k] = self.counts.get(k, 0) + n

    db = NodeDB()
    ref = await source(db, "A.")
    outer, inner = Counter(), Counter()
    wrapped = MeteredDB(MeteredDB(db, outer), inner)
    await wrapped.fetchrow(
        "SELECT payload FROM knowledge_artifacts", db.account, ref.id
    )
    assert not outer.counts and inner.counts["retrieval_rows"] == 1


@pytest.mark.asyncio
async def test_handoff_source_versions_seed_distinct_propositions():
    from contexthub.discovery.service import read_graph_handoff

    report, graph, _, db, _ = await flow()
    reread, frozen, seeds, downstream = await read_graph_handoff(db, report.reference())
    assert reread == report and frozen == graph and seeds
    assert set(n for v in seeds.values() for n in v) == set(graph.nodes)
    edge = graph.edges[0]
    assert edge.downstream in downstream[edge.upstream]


@pytest.mark.asyncio
async def test_complete_core_plan_price_changes_optional_work_only():
    high, gh, rh, _, _ = await flow(price=1e100)
    low, gl, rl, _, _ = await flow(price=0)
    tie, gt, rt, _, _ = await flow(price=1)
    assert high.status == low.status == tie.status == "complete"
    assert len(gh.edges) == len(gl.edges) == len(gt.edges) == 1
    assert len(rh) == 4 and len(rl) == len(rt) == 6
    assert all(d.result.reason == "dual_weak" for d in high.decisions)
    assert all(d.result.reason == "optional_strong" for d in low.decisions)


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [10.0, 1e20, 1e100, 1e-100, -0.0])
async def test_report_configuration_preserves_numeric_types_and_legacy_hash(price):
    import json
    from uuid import uuid4
    from contexthub.discovery.contracts import DiscoveryReport
    from contexthub.models.knowledge import canonical_hash

    report, _, _, db, _ = await flow(price=price)
    original = report.configuration_payload()
    assert report.configuration["encoding"] == "canonical-json-text-v1"
    assert original["search"]["lambda_p1"] == price
    assert type(original["search"]["lambda_p1"]) is float
    assert canonical_hash(original) == report.config_sha256
    assert DiscoveryConfig.model_validate(original).model_dump(mode="json") == original
    assert (
        DiscoveryReport.model_validate_json(report.model_dump_json()).reference()
        == report.reference()
    )

    # Pre-revision reports used a bare dict. Reading them must not rewrite their
    # payload or hash; the compatibility path does not re-encode existing seals.
    legacy_payload = dict(
        report.model_dump(mode="json"), id=str(uuid4()), configuration=original
    )
    legacy = DiscoveryReport.model_validate(legacy_payload)
    assert legacy.model_dump(mode="json") == legacy_payload
    assert legacy.reference().sha256 == canonical_hash(legacy_payload)
    await ArtifactService().seal(db, legacy)
    assert (
        await ArtifactService().get(db, legacy.reference())
    ).configuration_payload() == original

    broken = report.model_dump(mode="json")
    original["search"]["lambda_p1"] = price + 1 if price < 1e20 else 0.0
    broken["configuration"]["payload"] = json.dumps(original)
    with pytest.raises(ValueError, match="configuration_hash_mismatch"):
        DiscoveryReport.model_validate(broken)
    broken["configuration"]["encoding"] = "unknown-v2"
    with pytest.raises(ValueError, match="configuration_encoding_unknown"):
        DiscoveryReport.model_validate(broken)
