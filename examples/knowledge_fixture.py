"""Offline provider fixtures ONLY: no production model or prompt defaults.

This substitutes HTTP model responses, never discovery, S5/S6, or persistence.
"""

import json
from pathlib import Path
import httpx
from contexthub.knowledge_config import KnowledgeConfig
from contexthub.services.knowledge_factory import KnowledgeSecrets
from contexthub.propagation.candidate import Content, Cause, ProposalData
from contexthub.models.knowledge import EvidenceRef


def configuration(profile="adaptive"):
    data = json.loads(Path(__file__).with_name("knowledge-fixture.json").read_text())
    data["profile"]["profile"] = profile
    return KnowledgeConfig.model_validate(data)


def secrets():
    return KnowledgeSecrets(
        **{role: "synthetic-not-a-secret" for role in KnowledgeSecrets.__annotations__}
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


def judgment(payload, verdict="DEPENDS", **updates):
    refs = payload["evidence"]
    out = dict(
        verdict=verdict,
        evidence_sha256=payload["evidence_sha256"],
        citations=refs,
        steps=[
            {"premises": [ref], "previous_steps": [], "conclusion": ref} for ref in refs
        ],
        negative_basis=(
            "identity_exclusion"
            if verdict == "DIFFERENT"
            else "explicit_non_use" if verdict == "INDEPENDENT" else "none"
        ),
        exclusion=refs if verdict in ("INDEPENDENT", "DIFFERENT") else [],
        conflicts=[],
        missing=[],
        semantic_basis="fixture supported relationship",
    )
    out.update(updates)
    return out


class SyntheticModels:
    def __init__(
        self,
        *,
        outcome="updated",
        verdict="PASS",
        chain=False,
        location="unrelated",
        pending=False,
    ):
        self.calls = []
        self.outcome, self.verdict, self.chain, self.location, self.pending = (
            outcome,
            verdict,
            chain,
            location,
            pending,
        )
        self.transport = httpx.MockTransport(self.send)

    def send(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        prompt = body["messages"][-1]["content"]
        model = body["model"]
        if model == "extract":
            sources = json.loads(prompt[prompt.index("[") :])
            props = []
            for source in sources:
                # Fixture grammar is raw "A|B|C"; no external edge/answer sidecar.
                for sentence in source["text"].split("|"):
                    start = source["text"].index(sentence)
                    props.append(
                        dict(
                            text=sentence,
                            evidence=[
                                dict(
                                    source_id=source["snapshot"]["id"],
                                    start=start,
                                    end=start + len(sentence),
                                    excerpt=sentence,
                                    provenance=["original text"],
                                )
                            ],
                        )
                    )
            return response({"propositions": props})
        payload = json.loads(prompt[prompt.index("{") :])
        if model == "fixture-model":
            left, right = payload["left"], payload["right"]
            # Relationships are stated explicitly in ordinary content.
            positive = (left.startswith("A:") and right.startswith("B: uses A")) or (
                left.startswith("B:") and right.startswith("C: uses B")
            )
            return response(
                judgment(
                    payload,
                    (
                        "UNKNOWN"
                        if self.pending
                        else "DEPENDS" if positive else "INDEPENDENT"
                    ),
                )
            )
        if model == "locator":
            raw = payload["change"]
            candidates = payload["candidates"]
            if self.location != "located":
                return response(
                    dict(
                        status=self.location,
                        matches=[],
                        reason="synthetic location result",
                    )
                )
            old = candidates[0]

            def span(s, text):
                start = s["text"].index(text)
                return dict(
                    source_id=s["snapshot_id"],
                    start=start,
                    end=start + len(text),
                    excerpt=text,
                    provenance=["raw statement"],
                )

            replacement = raw["text"].split(" now ")[-1]
            return response(
                dict(
                    status="located",
                    reason="explicit same-source update",
                    matches=[
                        dict(
                            source_id=old["context_id"],
                            version=old["version"],
                            old=span(old, old["text"]),
                            replacement=span(raw, replacement),
                            identity_basis=span(raw, raw["text"]),
                            temporal_basis=span(raw, raw["text"]),
                            relation="same_subject_same_attribute_replacement",
                        )
                    ],
                )
            )
        if model == "verifier":
            return response(verdict_payload(payload["candidate"], self.verdict))
        if self.outcome == "unresolved":
            return response(
                dict(proposal=None, unresolved_reason="synthetic insufficient evidence")
            )
        cause_versions = [c["source"] for c in payload["binding"]["causes"]]
        source_ids = {
            s["source"]["id"]
            for s in payload["sources"]
            if s["source"]["source"] in cause_versions
        }
        evidence = tuple(
            EvidenceRef.model_validate(r["evidence"])
            for r in payload["bundle"]["read"]
            if r["evidence"] and r["evidence"]["source"]["id"] in source_ids
        )
        causes = tuple(Cause.model_validate(c) for c in payload["binding"]["causes"])
        original = Content.model_validate(payload["binding"]["original"])
        content = (
            original
            if self.outcome == "unchanged"
            else Content(
                l0_content=None,
                l1_content=None,
                l2_content=(original.l2_content or "") + " revised",
            )
        )
        data = make_data(content, evidence, causes, self.outcome)
        return response(
            dict(proposal=data.model_dump(mode="json"), unresolved_reason=None)
        )
