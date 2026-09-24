import json
from types import SimpleNamespace
import httpx
import pytest
from contexthub.models.knowledge import ContractError
from contexthub.propagation.candidate import ModelCall
from contexthub.propagation.verifier import parse_verdict
from s6_helpers import sample_data, verdict_payload, model_config, clock_frame, response
from knowledge_helpers import MemorySink


@pytest.mark.parametrize(
    "name", ["applicability", "complete_support", "conflicts_resolved"]
)
@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
def test_each_verdict_retained_independently(name, verdict):
    data = sample_data()
    payload = verdict_payload(data)
    payload[name]["verdict"] = verdict
    bundle = SimpleNamespace(
        read=[SimpleNamespace(evidence=e) for e in data.claims[0].evidence]
    )
    result = parse_verdict(json.dumps(payload), SimpleNamespace(data=data), bundle)
    assert getattr(result, name).verdict == verdict


def test_verifier_wrong_reference_rejected():
    data = sample_data()
    payload = verdict_payload(data)
    payload["applicability"]["evidence"] = sample_data().model_dump(mode="json")[
        "claims"
    ][0]["evidence"]
    with pytest.raises(ContractError, match="verifier_evidence_not_read"):
        parse_verdict(
            json.dumps(payload),
            SimpleNamespace(data=data),
            SimpleNamespace(
                read=[SimpleNamespace(evidence=e) for e in data.claims[0].evidence]
            ),
        )


@pytest.mark.asyncio
async def test_actual_recorded_verifier_call_independent_identity():
    sink = MemorySink()
    calls = []
    data = sample_data()

    def send(request):
        calls.append(json.loads(request.content))
        return response(verdict_payload(data))

    service = ModelCall(
        model_config("fixed-verifier"),
        api_key="synthetic",
        recorder=sink,
        transport=httpx.MockTransport(send),
    )
    try:
        value, ident = await service.invoke(
            clock_frame(),
            SimpleNamespace(profile="adaptive", action="M1"),
            {"candidate": data.model_dump(mode="json")},
            json.loads,
            "verifier",
        )
    finally:
        await service.close()
    assert len(calls) == 1 and ident.model_role == "verifier"
    assert sink.rows[-1].usage.input_tokens == 11
    assert value["complete_support"]["verdict"] == "PASS"
