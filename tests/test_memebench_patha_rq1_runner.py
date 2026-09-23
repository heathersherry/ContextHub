import json
from uuid import uuid4
import httpx
import pytest
from integrations.memebench.patha.rq1_runner import RQ1Config, RQ1Runner
from integrations.memebench.patha.schemas import MaintenanceInput, canonical_sha256
from test_memebench_patha_firewall import _payload
from discovery_helpers import config, search_policy
from s4_helpers import adjudication_config, judgment


def runner_config():
    return RQ1Config(
        extraction=config(),
        extraction_price=None,
        adjudication=adjudication_config(),
        selective=search_policy(strategy="selective", lambda_p1=10),
        full_history=search_policy(strategy="full-history", lambda_p1=10),
        batch_size=1,
        timeout_seconds=30,
        discovery_timeout_seconds=10,
        code_version="fixture-s4",
        mode="synthetic",
    )


def runtime():
    p = _payload()
    p["sources"][0]["messages"][0]["content"] = "A."
    p["sources"][0]["content_sha256"] = canonical_sha256(p["sources"][0]["messages"])
    other = json.loads(json.dumps(p["sources"][0]))
    other["source_id"] = "src_" + "3" * 24
    other["ordinal"] = 1
    other["messages"][0]["content"] = "B depends on A."
    other["content_sha256"] = canonical_sha256(other["messages"])
    p["sources"].append(other)
    p["input_sha256"] = canonical_sha256(
        {k: v for k, v in p.items() if k != "input_sha256"}
    )
    return MaintenanceInput.model_validate(p)


def runtime_transport(requests):
    def handle(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if '"snapshot"' in prompt:
            payload = json.loads(prompt[prompt.index("[") :])
            requests.append(("extract", payload))
            raw = {
                "propositions": [
                    {
                        "text": json.loads(p["text"])[0]["content"],
                        "evidence": [
                            {
                                "source_id": p["snapshot"]["id"],
                                "start": 0,
                                "end": len(p["text"]),
                                "excerpt": p["text"],
                                "provenance": ["fixture original"],
                            }
                        ],
                    }
                    for p in payload
                ]
            }
        else:
            payload = json.loads(prompt[prompt.index("{") :])
            requests.append(("judge", payload))
            if not any(
                "A." in e["excerpt"] and "B depends" not in e["excerpt"]
                for e in payload["evidence"]
            ):
                raw = judgment(payload, missing=["A"])
            else:
                raw = judgment(
                    payload, "DEPENDS" if payload["left"] == "A." else "INDEPENDENT"
                )
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "choices": [{"message": {"content": json.dumps(raw)}}],
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 3,
                    "total_tokens": 20,
                },
            },
        )

    return httpx.MockTransport(handle)


def test_explicit_config_and_fairness():
    cfg = runner_config()
    with pytest.raises(ValueError):
        RQ1Config(
            **dict(
                cfg.model_dump(),
                full_history=search_policy(strategy="full-history", lambda_p1=11),
            )
        )
    with pytest.raises(ValueError, match="synthetic_transport_required"):
        RQ1Runner(cfg, repository=None, ledger=None, api_key="fixture")


@pytest.mark.asyncio
async def test_privileged_large_object_rejected_before_any_work():
    runner = RQ1Runner(
        runner_config(),
        repository=None,
        ledger=None,
        api_key="fixture",
        transport=runtime_transport([]),
    )
    with pytest.raises(ValueError, match="maintenance_input_required"):
        await runner.run(
            {"runtime": runtime(), "gold": "leak"},
            account_id="a",
            run_id=uuid4(),
            replicate_id=0,
            cutoff_ordinal=1,
        )
