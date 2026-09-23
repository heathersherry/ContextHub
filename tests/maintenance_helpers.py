"""S4-R1/R2 regression through real core and isolated ordinary-role PostgreSQL.

Four-node chain fixture derives from the independent S4 review reproductions;
only model HTTP responses are substituted. No production database fallback.
"""

import copy
import json
from datetime import datetime, timezone
from uuid import uuid4
import httpx
from contexthub.discovery.extraction import ExtractionService
from contexthub.discovery.adjudication import AdjudicationService
from contexthub.discovery.search import SearchService, HistorySnapshot, HistoryEntry
from contexthub.discovery.service import (
    DiscoveryService,
    DiscoveryConfig,
)
from contexthub.services.evidence_service import EvidenceService
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.models.knowledge import ContextVersion, text_hash
from discovery_helpers import config, search_policy, call_identity
from s4_helpers import adjudication_config, judgment


async def make_graph(repo, narrow=False, price=10, mandatory=False, pending=False):
    account = "s4-review-" + str(uuid4())
    ledger = ExecutionLedger(repo)
    calls = []
    content = "A supports B. B supports C. A supports D. D supports C. Unrelated weather note. All other pairs are unused."
    original = json.dumps([{"role": "user", "content": content}], ensure_ascii=False)
    node = ContextVersion(context_id=uuid4(), version=1)
    async with repo.session(account) as db:
        await db.execute(
            "INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content) VALUES($1,$2,$3,'memory','datalake',$4)",
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
                if (mandatory and p["role"] != "strong")
                or (pending and p["left"] == "A" and p["right"] == "B")
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
    return account, node, r
