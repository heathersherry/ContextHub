import json
from contexthub.discovery.adjudication import AdjudicationConfig
from contexthub.discovery.planner import Plan
from contexthub.discovery.stage0 import Stage0Config
from contexthub.services.evidence_service import EvidenceService
from discovery_helpers import config, source
import httpx


def adjudication_config(**updates):
    data = dict(
        id="fixture-adjudication",
        weak=config(),
        strong=config(),
        stage0=Stage0Config(
            version="fixture-v1",
            rules=(),
            alias_version="fixture-empty",
            aliases=(),
            mention_patterns=(),
        ),
        plans=(
            Plan(id="base", q=1, c=1, optional_strong=False),
            Plan(id="review", q=2, c=2, optional_strong=True),
        ),
        proxy_version="fixture-q-v1",
        cost_formula_version="fixture-c-v1",
        lambda_p1=10,
        weak_price=None,
        strong_price=None,
    )
    data.update(updates)
    return AdjudicationConfig(**data)


async def evidence(db, text="A supplies B under rule R."):
    ref = await source(db, text)
    return await EvidenceService().reference(
        db, ref, start=0, end=len(text), provenance=("fixture original",)
    )


def judgment(payload, verdict="DEPENDS", **updates):
    refs = payload["evidence"]
    out = dict(
        verdict=verdict,
        evidence_sha256=payload["evidence_sha256"],
        citations=refs,
        steps=[
            {"premises": [ref], "previous_steps": [], "conclusion": ref} for ref in refs
        ],
        negative_basis="identity_exclusion"
        if verdict == "DIFFERENT"
        else "explicit_non_use"
        if verdict == "INDEPENDENT"
        else "none",
        exclusion=refs if verdict in ("INDEPENDENT", "DIFFERENT") else [],
        conflicts=[],
        missing=[],
        semantic_basis="fixture supported relationship",
    )
    out.update(updates)
    return out


def judge_transport(requests, responder=None):
    def handle(request):
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        payload = json.loads(prompt[prompt.index("{") :])
        requests.append(payload)
        out = responder(payload, len(requests)) if responder else judgment(payload)
        if isinstance(out, int):
            return httpx.Response(out, json={})
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "choices": [{"message": {"content": json.dumps(out)}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 5,
                    "total_tokens": 16,
                },
            },
        )

    return httpx.MockTransport(handle)


from discovery_helpers import MemoryDB


class NodeDB(MemoryDB):
    async def fetchrow(self, sql, *args):
        if "SELECT c.scope,c.owner_space" in sql:
            return (
                {"scope": "user", "owner_space": None}
                if args[1] in self.artifacts
                else None
            )
        return await super().fetchrow(sql, *args)

    async def execute(self, sql, *args):
        if "INSERT INTO contexts" in sql:
            from knowledge_helpers import NOW

            self.versions.setdefault(
                (args[0], 1), {"created_at": NOW, "l2_content": args[5]}
            )
            return
        return await super().execute(sql, *args)
