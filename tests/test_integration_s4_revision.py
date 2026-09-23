"""S4-R1/R2 regression through real core and isolated ordinary-role PostgreSQL.

Four-node chain fixture derives from the independent S4 review reproductions;
only model HTTP responses are substituted. No production database fallback.
"""

import copy
import json
from datetime import datetime, timezone
from uuid import uuid4
import httpx
import pytest
from contexthub.discovery.extraction import ExtractionService
from contexthub.discovery.adjudication import AdjudicationService
from contexthub.discovery.search import SearchService, HistorySnapshot, HistoryEntry
from contexthub.discovery.service import (
    DiscoveryService,
    DiscoveryConfig,
    read_graph_handoff,
)
from contexthub.services.evidence_service import EvidenceService
from contexthub.services.artifact_service import ArtifactService
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.models.knowledge import ContextVersion, canonical_hash, text_hash
from contexthub.models.execution import AttemptRecord
from discovery_helpers import config, search_policy, call_identity
from s4_helpers import adjudication_config, judgment
from integrations.memebench.patha.rq1_scorer import (
    load_graph_evidence,
    PathReview,
    StepReview,
    score_rq1,
)
from integrations.memebench.patha.scoring_contract import build_scoring_contract
from integrations.memebench.patha.schemas import canonical_sha256
from test_integration_knowledge_storage import s2_repo as isolated_repo

s2_repo = isolated_repo


async def sealed_graph(repo, narrow=False, price=10, mandatory=False):
    account = "s4-review-" + str(uuid4())
    ledger = ExecutionLedger(repo)
    calls = []
    content = "A supports B. B supports C. A supports D. D supports C. Unrelated weather note. All other pairs are unused."
    original = json.dumps([{"role": "user", "content": content}], ensure_ascii=False)
    node = ContextVersion(context_id=uuid4(), version=1)
    async with repo.session(account) as db:
        await db.execute(
            "INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content) VALUES($1,$2,$3,'memory','user',$4)",
            node.context_id,
            account,
            "ctx://independent/" + str(node.context_id),
            original,
        )
        source_ref = await EvidenceService().snapshot(
            db,
            account_id=account,
            source=node,
            content_level="l2_content",
            cutoff_at=datetime.now(timezone.utc),
        )

    def send(request):
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        calls.append(body)
        if '"snapshot"' in prompt:
            payload = json.loads(prompt[prompt.index("[") :])
            s = payload[0]
            raw = {
                "propositions": [
                    {
                        "text": label,
                        "evidence": [
                            {
                                "source_id": s["snapshot"]["id"],
                                "start": 0,
                                "end": len(s["text"]),
                                "excerpt": s["text"],
                                "provenance": ["synthetic original turn"],
                            }
                        ],
                    }
                    for label in "ABCD"
                ]
            }
        else:
            p = json.loads(prompt[prompt.index("{") :])
            positive = (p["left"], p["right"]) in [
                ("A", "B"),
                ("B", "C"),
                ("A", "D"),
                ("D", "C"),
            ]
            raw = judgment(
                p,
                "UNKNOWN"
                if mandatory and p["role"] != "strong"
                else "DEPENDS"
                if positive
                else "INDEPENDENT",
                semantic_basis=f"{p['left']} to {p['right']}",
            )
            if narrow and positive:
                ref = copy.deepcopy(p["evidence"][0])
                sentence = f"{p['left']} supports {p['right']}."
                start = original.index(sentence)
                ref.update(
                    start=start,
                    end=start + len(sentence),
                    excerpt=sentence,
                    excerpt_sha256=text_hash(sentence),
                )
                raw.update(
                    citations=[ref],
                    steps=[
                        {"premises": [ref], "previous_steps": [], "conclusion": ref}
                    ],
                )
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "choices": [{"message": {"content": json.dumps(raw)}}],
                "usage": {
                    "prompt_tokens": 23,
                    "completion_tokens": 9,
                    "total_tokens": 32,
                },
            },
        )

    transport = httpx.MockTransport(send)
    excfg = config()
    extractor = ExtractionService(
        excfg, api_key="synthetic", recorder=ledger, transport=transport
    )
    try:
        async with repo.session(account) as db:
            artifact = await extractor.extract(
                db, (source_ref,), identity=call_identity(excfg, account_id=account)
            )
    finally:
        await extractor.close()
    cfg = DiscoveryConfig(
        search=search_policy(strategy="full-history", page_size=1, lambda_p1=price),
        adjudication=adjudication_config(lambda_p1=price),
        timeout_seconds=30,
    )
    adj = AdjudicationService(
        cfg.adjudication, api_key="synthetic", recorder=ledger, transport=transport
    )
    hist = HistorySnapshot(
        account_id=account,
        entries=(HistoryEntry(source=source_ref, ingestion_ordinal=0),),
        cutoff_at=datetime.now(timezone.utc),
        cutoff_ordinal=0,
    )
    try:
        async with repo.session(account) as db:
            r = await DiscoveryService(
                cfg,
                search=SearchService(recorder=ledger),
                adjudicator=adj,
                recorder=ledger,
            ).discover(
                db,
                artifact.reference(),
                hist,
                identity=call_identity(cfg, account_id=account),
            )
    finally:
        await adj.close()
    async with repo.session(account) as db:
        reread, g, seeds, down = await read_graph_handoff(db, r.reference())
        row = await db.fetchrow(
            "SELECT payload,sha256 FROM knowledge_artifacts WHERE account_id=$1 AND id=$2",
            account,
            r.id,
        )
        assert row["payload"] == r.model_dump(mode="json")
        assert canonical_hash(row["payload"]) == row["sha256"] == r.reference().sha256
        assert reread.configuration_payload() == cfg.model_dump(mode="json")
        assert canonical_hash(reread.configuration_payload()) == canonical_hash(cfg)
        assert (
            r == reread
            and r.status == "complete"
            and len(g.nodes) == 4
            and len(g.edges) == 4
        )
        assert len(seeds[node]) == 4
        assert all(e.downstream in down[e.upstream] for e in g.edges)
        delivered = await load_graph_evidence(
            db,
            graph_ref=g.reference(),
            report_refs=(r.reference(),),
            source_map=(("source", source_ref),),
            status=r.status,
        )
        rows = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE account_id=$1", account
        )
    async with repo.session(account + "-other") as db:
        with pytest.raises(ValueError, match="tenant_mismatch"):
            await ArtifactService().get(db, r.reference())
        assert not await db.fetch(
            "SELECT payload FROM knowledge_artifacts WHERE account_id=$1", account
        )
    assert len(calls) == 25 + (12 if mandatory or price <= 1 else 0)
    if mandatory:
        assert all(d.result.reason == "mandatory_strong" for d in r.decisions)
    records = [AttemptRecord.model_validate(x["payload"]) for x in rows]
    assert len(
        [x for x in records if x.phase == "finished" and x.requested_model]
    ) == len(calls)
    assert all(x.identity.account_id == account for x in records)
    names = {m.text: m.node for m in r.mappings}
    paths = []
    for id_, labels in [("via_B", "ABC"), ("via_D", "ADC")]:
        paths.append(
            {
                "path_id": id_,
                "entities": list(labels),
                "direction": "event_root_to_target",
                "p2_traversable_if_all_edges_delivered": True,
                "edges": [
                    {
                        "source_entity": a,
                        "target_entity": b,
                        "dialogue_evidence": [
                            {
                                "source_id": "source",
                                "turn_index": 0,
                                "role": "user",
                                "excerpt": content,
                                "excerpt_sha256": text_hash(content),
                            }
                        ],
                    }
                    for a, b in zip(labels, labels[1:])
                ],
            }
        )
    ref = {
        "record_id": "independent-record",
        "episode_id": "independent-episode",
        "necessary_path_candidates": paths,
        "data_quality_notes": [],
    }
    ref["record_sha256"] = canonical_sha256(ref)
    contract = build_scoring_contract(
        [
            dict(
                record_id="independent-record",
                answer_pair_status="scoreable",
                path_status="scoreable",
            )
        ],
        [ref],
        artifact_hashes={},
        contract_validator_code={},
    )
    reviews = []
    for path in paths:
        ns = tuple(names[x] for x in path["entities"])
        steps = []
        for a, b, step in zip(ns, ns[1:], path["edges"]):
            edge = next(e for e in g.edges if e.upstream == a and e.downstream == b)
            steps.append(
                StepReview(
                    upstream=a,
                    downstream=b,
                    edge_sha256=canonical_hash(edge),
                    reference_step_sha256=canonical_sha256(step),
                    evidence=edge.evidence,
                    verdict="PASS",
                    rationale="Synthetic exact supports relation checked against the original turn.",
                )
            )
        reviews.append(
            PathReview(
                record_id=ref["record_id"],
                path_id=path["path_id"],
                reference_record_sha256=ref["record_sha256"],
                graph=g.reference(),
                reviewer_id="independent-s4-review",
                review_method="offline_human_semantic_review",
                review_version="synthetic-1",
                nodes=ns,
                steps=tuple(steps),
            )
        )
    return contract, ref, delivered, tuple(reviews)


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [10.0, 1e20, 1e100, 1e-100, 1.7976931348623157e308])
async def test_legal_prices_survive_real_seal_and_reread(s2_repo, price):
    await sealed_graph(s2_repo, price=price)


@pytest.mark.asyncio
@pytest.mark.parametrize("narrow", [False, True])
async def test_real_graph_whole_and_narrow_citations_cover(s2_repo, narrow):
    contract, path, delivered, reviews = await sealed_graph(s2_repo, narrow=narrow)
    score = score_rq1(contract, [path], {"independent-episode": delivered}, reviews)
    assert score["covered"] == score["denominator"] == 1, score
    # Each independently valid alternative also works without the other review.
    for review in reviews:
        score = score_rq1(
            contract, [path], {"independent-episode": delivered}, (review,)
        )
        assert score["covered"] == 1, score


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [10.0, 1e100, 1e-100])
async def test_both_runner_arms_seal_with_legal_prices_and_keep_costs(s2_repo, price):
    from integrations.memebench.patha.rq1_runner import RQ1Runner, RQ1Config
    from test_memebench_patha_rq1_runner import (
        runner_config,
        runtime,
        runtime_transport,
    )

    data = runner_config().model_dump(mode="json")
    for field in ("selective", "full_history", "adjudication"):
        data[field]["lambda_p1"] = price
    cfg = RQ1Config.model_validate(data)
    calls = []
    account = "s4-revision-" + str(uuid4())
    result = await RQ1Runner(
        cfg,
        repository=s2_repo,
        ledger=ExecutionLedger(s2_repo),
        api_key="synthetic",
        transport=runtime_transport(calls),
    ).run(
        runtime(), account_id=account, run_id=uuid4(), replicate_id=0, cutoff_ordinal=1
    )
    assert all(arm.status == "complete" for arm in result.arms), result
    expected = 8 if price <= 1 else 6
    assert len(calls) == result.actual_batch_cost.model_attempts == expected
    assert sum(a.cost.model_attempts for a in result.arms) == expected + 2
    async with s2_repo.session(account) as db:
        for arm in result.arms:
            graph = await ArtifactService().get(db, arm.graph)
            assert len(graph.edges) == 1
            for ref in arm.reports:
                report = await ArtifactService().get(db, ref)
                assert report.reference() == ref
        rows = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE account_id=$1", account
        )
    assert len(rows) == len(result.records)
    assert {canonical_hash(row["payload"]) for row in rows} == {
        canonical_hash(r) for r in result.records
    }


@pytest.mark.asyncio
async def test_high_price_keeps_mandatory_review_and_seals_its_ledger(s2_repo):
    await sealed_graph(s2_repo, price=1e100, mandatory=True)
