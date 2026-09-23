"""Synthetic storage adapter for unit tests; production services run unchanged."""

import json
from uuid import uuid4

import httpx

from contexthub.discovery.extraction import ExtractionConfig, ExtractionService
from contexthub.discovery.search import HistorySnapshot, HistoryEntry, SearchPolicy
from contexthub.models.knowledge import ContextVersion, canonical_hash
from contexthub.services.evidence_service import EvidenceService
from knowledge_helpers import NOW, identity, policy, MemorySink


class MemoryDB:
    def __init__(self, account="s2-a"):
        self.account = account
        self.artifacts = {}
        self.versions = {}
        self.reads = []

    async def fetchval(self, sql, *args):
        if "current_setting" in sql:
            return self.account
        if "context_versions" in sql:
            return 1 if (args[1], args[2]) in self.versions else None
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "knowledge_artifacts" in sql:
            return self.artifacts.get(args[1])
        if "context_versions" in sql:
            self.reads.append((args[1], args[2]))
            return self.versions.get((args[1], args[2]))
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        assert "INSERT INTO knowledge_artifacts" in sql
        self.artifacts.setdefault(args[1], {"sha256": args[4], "payload": args[5]})


async def source(
    db,
    text="  中文😀：如果上限变为400，450不能报销。\n",
    version=1,
    created=NOW,
    context_id=None,
):
    cv = ContextVersion(context_id=context_id or uuid4(), version=version)
    db.versions[cv.context_id, version] = {"created_at": created, "l2_content": text}
    return await EvidenceService().snapshot(
        db,
        account_id=db.account,
        source=cv,
        content_level="l2_content",
        cutoff_at=created,
    )


def config(**updates):
    fields = dict(
        id="fixture-extract",
        model="fixture-model",
        base_url="https://fixture.invalid/v1",
        temperature=0,
        max_tokens=400,
        prompt_template="Extract JSON preserving conditions and provenance. {sources_json}",
        retry=policy(),
    )
    fields.update(updates)
    return ExtractionConfig(**fields)


def call_identity(cfg, **updates):
    return identity().model_copy(
        update={"config_sha256": canonical_hash(cfg), **updates}
    )


def response(ref, text, **updates):
    data = {
        "propositions": [
            {
                "text": text,
                "evidence": [
                    {
                        "source_id": str(ref.id),
                        "start": 0,
                        "end": len(text),
                        "excerpt": text,
                        "provenance": ["stated condition: if limit changes"],
                    }
                ],
            }
        ]
    }
    data.update(updates)
    return data


def transport(responses, requests):
    def handle(request):
        requests.append(json.loads(request.content))
        item = responses[min(len(requests) - 1, len(responses) - 1)]
        status, value = item if isinstance(item, tuple) else (200, item)
        if isinstance(value, Exception):
            raise value
        return httpx.Response(
            status,
            json={
                "model": "fixture-model",
                "choices": [
                    {
                        "message": {
                            "content": value
                            if isinstance(value, str)
                            else json.dumps(value, ensure_ascii=False)
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 13,
                    "completion_tokens": 7,
                    "total_tokens": 20,
                },
            },
            headers={"x-request-id": ""},
        )

    return httpx.MockTransport(handle)


async def extract(db, refs, outputs, *, sink=None, cfg=None):
    cfg, sink, requests = cfg or config(), sink or MemorySink(), []
    service = ExtractionService(
        cfg, api_key="fixture", recorder=sink, transport=transport(outputs, requests)
    )
    ident = call_identity(cfg, account_id=db.account)
    try:
        result = await service.extract(db, refs, identity=ident)
    finally:
        await service.close()
    return result, sink, requests


def search_policy(**updates):
    args = dict(
        id="fixture-search",
        strategy="selective",
        initial_sources=1,
        expansion_factor=2,
        page_size=2,
        ordering="ingestion_desc",
        lambda_p1=1e20,
    )
    args.update(updates)
    return SearchPolicy(**args)


def history(refs, **updates):
    data = dict(
        account_id=refs[0].account_id,
        entries=tuple(
            HistoryEntry(source=s, ingestion_ordinal=i) for i, s in enumerate(refs)
        ),
        cutoff_at=NOW,
        cutoff_ordinal=len(refs) - 1,
    )
    data.update(updates)
    return HistorySnapshot(**data)


async def pages(service, db, artifact, hist, pol):
    result, cursor = [], None
    for _ in range(100):
        page = await service.search(
            db,
            artifact,
            hist,
            pol,
            identity=call_identity(pol, account_id=hist.account_id),
            cursor=cursor,
        )
        result.append(page)
        if page.cursor is None:
            return result
        cursor = page.cursor
    raise AssertionError("unbounded search")
