"""Synthetic S6 responses. Only the HTTP model boundary is substituted."""

import json
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from contexthub.models.knowledge import ContextVersion, EvidenceRef, SealRef, text_hash
from contexthub.propagation.candidate import Content, Cause, ProposalData, ModelConfig
from knowledge_helpers import policy, HASH


def model_config(name="base"):
    return ModelConfig(
        id=name,
        model=name,
        base_url="https://synthetic.invalid/v1",
        temperature=0,
        max_tokens=1000,
        prompt_template="Input {input_json}",
        rules_version="synthetic-v1",
        retry=policy(),
        price=None,
    )


def sample_data():
    ref = EvidenceRef(
        source=SealRef(account_id="fixture", id=uuid4(), kind="source", sha256=HASH),
        start=0,
        end=3,
        excerpt="new",
        excerpt_sha256=text_hash("new"),
        provenance=("after",),
    )
    cause = Cause(
        event_id=uuid4(),
        source=ContextVersion(context_id=uuid4(), version=2),
        reason_sha256=HASH,
    )
    return make_data(
        Content(l0_content=None, l1_content=None, l2_content="new conclusion"),
        (ref,),
        (cause,),
    )


def make_data(content, evidence, causes, outcome="updated"):
    return ProposalData(
        outcome=outcome,
        content=content,
        claims=tuple(
            dict(
                level=level,
                start=0,
                end=len(getattr(content, level)),
                conclusion=getattr(content, level),
                evidence=evidence,
                derivation="synthetic derivation",
            )
            for level in ("l0_content", "l1_content", "l2_content")
            if getattr(content, level)
        ),
        causes=tuple(
            dict(cause=c, evidence=evidence, rationale="synthetic resolution")
            for c in causes
        ),
    )


def response(body):
    import httpx

    return httpx.Response(
        200,
        json={
            "model": "fixture",
            "choices": [{"message": {"content": json.dumps(body)}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        },
    )


def verdict_payload(data, verdict="PASS"):
    d = data.model_dump(mode="json") if hasattr(data, "model_dump") else data
    item = dict(
        verdict=verdict,
        reason="synthetic evidence judgment",
        evidence=d["claims"][0]["evidence"],
        claim_indexes=list(range(len(d["claims"]))),
        cause_ids=[c["cause"]["event_id"] for c in d["causes"]],
    )
    return {
        k: dict(item)
        for k in ("applicability", "complete_support", "conflicts_resolved")
    }


def clock_frame():
    return SimpleNamespace(
        account_id="fixture",
        execution_id=uuid4(),
        event_id=uuid4(),
        snapshot=SimpleNamespace(id=uuid4()),
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
