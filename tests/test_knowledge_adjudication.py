import pytest
from contexthub.discovery.adjudication import AdjudicationService, EvidenceSet
from contexthub.discovery.stage0 import Mention
from discovery_helpers import MemoryDB, call_identity
from knowledge_helpers import MemorySink
from s4_helpers import evidence, adjudication_config, judgment, judge_transport


async def run(
    responder=None, *, kind="dependency", at_max=True, provenance=False, cfg=None
):
    db = MemoryDB()
    e = await evidence(db)
    e2 = await evidence(db, "B follows A.")
    cfg = cfg or adjudication_config()
    req = []
    sink = MemorySink()
    core = AdjudicationService(
        cfg, api_key="fixture", recorder=sink, transport=judge_transport(req, responder)
    )
    bundle = EvidenceSet(
        evidence=(e, e2), complete=True, at_max_scope=at_max, issues=()
    )
    try:
        out = await core.judge(
            db,
            kind=kind,
            left="A",
            right="B",
            bundle=bundle,
            identity=call_identity(cfg),
            explicit_provenance=provenance,
            mentions=(Mention(text="A", evidence=e), Mention(text="B", evidence=e2))
            if kind == "entity"
            else None,
        )
    finally:
        await core.close()
    return out, req, sink


@pytest.mark.asyncio
async def test_payload_isolation_entity_order_and_evidence_identity():
    out, req, _ = await run(lambda p, n: judgment(p, "SAME"), kind="entity")
    assert out.verdict == "SAME" and len(req) == 2
    assert req[1]["left"] == "B" and req[1]["right"] == "A"
    assert req[1]["evidence"] == list(reversed(req[0]["evidence"]))
    assert req[1]["evidence_sha256"] == req[0]["evidence_sha256"]
    assert "prior_judgments" not in req[1] and "verdict" not in req[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("at_max,action", [(False, "read_more"), (True, "pending")])
async def test_first_missing_does_not_call_second_or_strong(at_max, action):
    out, req, _ = await run(
        lambda p, n: judgment(p, missing=["premise"]), at_max=at_max
    )
    assert out.action == action and len(req) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem",
    [
        "conflict",
        "unknown",
        "steps",
        "bad_hash",
        "bad_version",
        "exclusion",
        "provenance",
    ],
)
async def test_hard_failures_force_strong_even_high_price(problem):
    def respond(p, n):
        if p["role"] == "strong":
            return judgment(p)
        if problem == "conflict":
            return judgment(p, "DEPENDS" if n == 1 else "INDEPENDENT")
        if problem == "unknown":
            return judgment(p, "UNKNOWN")
        if problem == "steps":
            return judgment(p, steps=[])
        if problem == "bad_hash":
            return judgment(p, evidence_sha256="0" * 64)
        if problem == "bad_version":
            refs = [dict(p["evidence"][0])]
            refs[0]["source"] = dict(refs[0]["source"], sha256="0" * 64)
            return judgment(p, citations=refs)
        return judgment(
            p, "INDEPENDENT", exclusion=[] if problem == "exclusion" else p["evidence"]
        )

    out, req, _ = await run(
        respond,
        provenance=problem == "provenance",
        cfg=adjudication_config(lambda_p1=1e100),
    )
    assert out.verdict == "DEPENDS" and [p["role"] for p in req] == [
        "normal",
        "challenge",
        "strong",
    ]
    assert out.reason == "mandatory_strong"


@pytest.mark.asyncio
async def test_strong_unknown_pending_optional_price_and_empty_menu():
    out, req, _ = await run(lambda p, n: judgment(p, "UNKNOWN"))
    assert out.action == "pending" and len(req) == 3
    out, req, _ = await run(cfg=adjudication_config(lambda_p1=0))
    assert out.reason == "optional_strong" and len(req) == 3
    out, req, _ = await run(cfg=adjudication_config(plans=()))
    assert out.reason == "no_legal_complete_plan" and len(req) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error,count", [(503, 3), ({"bad": "schema"}, 1)])
async def test_retry_and_parse_boundary(error, count):
    out, req, sink = await run(lambda p, n: error)
    assert out.action == "pending" and len(req) == count
    model_records = [
        r for r in sink.rows if r.requested_model and r.phase == "finished"
    ]
    assert len(model_records) == count


@pytest.mark.asyncio
async def test_alternative_source_alone_is_not_independence():
    out, req, _ = await run(
        lambda p, n: judgment(p, "INDEPENDENT", negative_basis="alternative_support")
    )
    assert len(req) == 3 and out.action == "pending"


@pytest.mark.asyncio
async def test_invented_intermediate_or_forward_step_cannot_pass():
    def respond(p, n):
        out = judgment(p)
        out["steps"][0]["previous_steps"] = [1]
        return out

    out, req, _ = await run(respond)
    assert len(req) == 3 and out.action == "pending"
