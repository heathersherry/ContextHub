"""Preparation contracts: no database, no real HTTP, no model evaluation."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from pydantic import ValidationError
from contexthub.llm.request_parameters import RequestParameters, SeedPolicy
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.propagation.candidate import ModelConfig
from contexthub.discovery.extraction import ExtractionConfig
from integrations.memebench.patha.answer_prompt import (
    OfficialAnswerConfig,
    render_answer,
    load_answer_template,
)
from integrations.memebench.patha.run_gates import (
    StatisticsConfiguration,
    validate_model_seed,
    stage_preflight,
)
from integrations.memebench.patha.episode_deadline import (
    episode_deadline,
    episode_failure,
)
from knowledge_helpers import MemorySink, identity, policy

DRAFT = (
    Path(__file__).resolve().parents[1] / "docs/patha-preparation-2026-09-25/draft-v1"
)


def params(**updates):
    data = dict(
        output_limit_field="max_completion_tokens",
        reasoning_effort="low",
        response_format="json_object",
        temperature_supported=False,
        seed=dict(
            support="supported",
            enabled=True,
            value=17,
            evidence_reference="OFFLINE TEST CONTRACT ONLY",
        ),
    )
    data.update(updates)
    return RequestParameters.model_validate(data)


def cfg(role):
    cls = (
        OfficialAnswerConfig
        if role == "answer"
        else ExtractionConfig
        if role in ("extraction", "p1_weak", "p1_strong")
        else ModelConfig
    )
    return cls.model_validate_json((DRAFT / "models" / f"{role}.json").read_text())


@pytest.mark.asyncio
async def test_wire_and_audit_change_together():
    payloads = []
    hashes = []
    for p in [
        params(),
        params(reasoning_effort="medium"),
        params(output_limit_field="max_tokens"),
        params(
            seed=dict(
                support="supported",
                enabled=False,
                value=None,
                evidence_reference="OFFLINE TEST",
            )
        ),
    ]:

        def send(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "{}"}}],
                    "model": "test",
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                },
            )

        sink = MemorySink()
        client = OpenAIChatClient(
            "not-a-key",
            model="test",
            request_parameters=p,
            transport=httpx.MockTransport(send),
        )
        try:
            await client.complete(
                "JSON please",
                max_tokens=77,
                call=RecordedCall(identity(), policy(), sink),
            )
        finally:
            await client.close()
        hashes.append(sink.rows[-1].request_config_sha256)
        assert sink.rows[-1].input_sha256 == canonical_hash(payloads[-1])
        assert sink.rows[-1].cost.amount is None
    assert len(set(hashes)) == 4
    assert (
        payloads[0]["max_completion_tokens"] == 77 and "max_tokens" not in payloads[0]
    )
    assert payloads[0]["reasoning_effort"] == "low" and payloads[0][
        "response_format"
    ] == {"type": "json_object"}
    assert payloads[0]["seed"] == 17 and "seed" not in payloads[-1]
    assert "temperature" not in payloads[0]


@pytest.mark.parametrize(
    "data",
    [
        dict(support="unknown", enabled=True, value=1),
        dict(support="unsupported", enabled=True, value=1, evidence_reference="x"),
        dict(support="supported", enabled=False, value=1, evidence_reference="x"),
        dict(support="supported", enabled=True, value=None, evidence_reference="x"),
        dict(support="supported", enabled=False, value=None),
        dict(support="supported", enabled=True, value=True, evidence_reference="x"),
    ],
)
def test_invalid_seed_rejected(data):
    with pytest.raises((ValidationError, ContractError)):
        SeedPolicy.model_validate(data)


def test_unknown_and_incompatible_before_http_constructor(monkeypatch):
    def denied(*a, **k):
        raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr(httpx, "AsyncClient", denied)
    with pytest.raises(ContractError, match="temperature_not_supported"):
        OpenAIChatClient("x", model="x", temperature=0, request_parameters=params())
    with pytest.raises(ContractError, match="provider_seed_support_unknown"):
        OpenAIChatClient(
            "x",
            model="x",
            request_parameters=params(seed=dict(support="unknown", enabled=False)),
        )
    with pytest.raises(ValidationError):
        params(output_limit_field="max_output_tokens")


def test_draft_models_and_role_seeds():
    stats = StatisticsConfiguration.model_validate_json(
        (DRAFT / "statistics.draft.json").read_text()
    )
    assert stats.provider_seed_supported is None and stats.repeats == 1
    for role in (
        "extraction",
        "p1_weak",
        "p1_strong",
        "change_location",
        "base",
        "strong",
        "verifier",
        "answer",
        "judge",
    ):
        model = cfg(role)
        assert stats.model_seeds[canonical_hash(model)].support == "unknown"
        with pytest.raises(ContractError, match="provider_seed_support_unknown"):
            validate_model_seed(SimpleNamespace(statistics=stats), model)
    model = cfg("base")
    p = params(
        seed=dict(support="supported", enabled=False, evidence_reference="OFFLINE TEST")
    )
    model = model.model_copy(update={"request_parameters": p, "temperature": None})
    stats = stats.model_copy(update={"model_seeds": {canonical_hash(model): p.seed}})
    validate_model_seed(SimpleNamespace(statistics=stats), model)
    with pytest.raises(ContractError, match="identity_mismatch"):
        validate_model_seed(
            SimpleNamespace(statistics=stats),
            model.model_copy(update={"id": "different"}),
        )


def test_official_template_exact_mapping_and_no_mutation():
    model = cfg("answer")
    contexts = [{"z": "原文 {question}", "a": "keep"}, {"text": "second"}]
    before = canonical_hash(contexts)
    expected = load_answer_template(model.official_source).format(
        context=json.dumps(
            contexts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        question="Question?",
    )
    assert render_answer(model, "Question?", contexts) == expected
    assert canonical_hash(contexts) == before
    for update in (
        {"prompt_template": "changed"},
        {"max_tokens": 501},
        {"temperature": 1},
    ):
        with pytest.raises((ValidationError, ContractError)):
            OfficialAnswerConfig.model_validate(
                {**model.model_dump(mode="json"), **update}
            )


@pytest.mark.asyncio
async def test_deadline_preserves_cancelled_attempt_unknown_cost_and_denominator():
    sink = MemorySink()
    sent = []

    async def send(request):
        sent.append(request)
        await asyncio.Event().wait()

    client = OpenAIChatClient("test", model="test", transport=httpx.MockTransport(send))
    try:
        with pytest.raises(TimeoutError):
            async with episode_deadline(0.01):
                await client.complete(
                    "JSON", call=RecordedCall(identity(), policy(), sink)
                )
    finally:
        await client.close()
    assert len(sent) == 1 and [r.phase for r in sink.rows] == ["started", "finished"]
    assert sink.rows[-1].outcome == "cancelled" and sink.rows[-1].cost.amount is None
    failure = episode_failure("ep", 0, "TimeoutError")
    assert failure["episode_id"] == "ep" and failure["cost_export_complete"] is False


@pytest.mark.asyncio
async def test_new_parameters_still_only_three_attempts():
    sent = []
    sink = MemorySink()

    def send(request):
        sent.append(json.loads(request.content))
        return httpx.Response(503, json={})

    client = OpenAIChatClient(
        "test",
        model="test",
        request_parameters=params(),
        transport=httpx.MockTransport(send),
    )
    call = RecordedCall(identity(), policy(), sink)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("JSON", call=call)
        with pytest.raises(ContractError):
            await client.complete("JSON", call=call)
    finally:
        await client.close()
    assert len(sent) == 3 and sent[0] == sent[1] == sent[2]
    assert all(r.cost.amount is None for r in sink.rows)


def test_no_grant_still_refused():
    with pytest.raises(ContractError, match="separate_run_authorization_required"):
        stage_preflight(None, None, "rq1", configuration_sha256="a" * 64)


@pytest.mark.asyncio
async def test_answer_and_official_judge_real_adapters_with_fake_http(monkeypatch):
    from integrations.memebench.patha.answer_runner import AnswerRunner
    import integrations.memebench.patha.answer_runner as ar
    import integrations.memebench.patha.official_adapter as ja
    from integrations.memebench.patha.official_adapter import (
        OfficialJudgeRunner,
        official_prompt,
    )
    from test_memebench_patha_official_contract import source
    from s8_helpers import scoring_label

    class Ledger(MemorySink):
        def __init__(self, *a):
            super().__init__()

        async def records(self, account, call_id):
            return tuple(r for r in self.rows if r.identity.call_id == call_id)

    monkeypatch.setattr(ar, "ExecutionLedger", Ledger)
    monkeypatch.setattr(ja, "ExecutionLedger", Ledger)
    sent = []

    def send(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": sent[-1]["model"],
                "choices": [
                    {"message": {"content": '{"correct":true,"reason":"fixture"}'}}
                ],
            },
        )

    transport = httpx.MockTransport(send)
    answer = AnswerRunner(
        cfg("answer"),
        repository=None,
        account_id="answer",
        api_key="test",
        transport=transport,
    )
    state = {"state": {"answer_contexts": [{"text": "kept"}]}, "sha256": "a" * 64}
    before = canonical_hash(state)
    try:
        rows, records = await answer.answer(
            SimpleNamespace(
                questions=[SimpleNamespace(question_id="q", text="Question?")]
            ),
            state,
            maintenance_account="maintenance",
        )
    finally:
        await answer.close()
    assert canonical_hash(state) == before and len(rows) == 1 and len(records) == 2
    assert sent[-1]["messages"][0]["content"] == render_answer(
        cfg("answer"), "Question?", state["state"]["answer_contexts"]
    )
    assert sent[-1]["max_tokens"] == 500 and sent[-1]["temperature"] == 0
    assert "tools" not in sent[-1]
    judge = OfficialJudgeRunner(
        cfg("judge"),
        official_source=source(),
        repository=None,
        account_id="judge",
        api_key="test",
        transport=transport,
    )
    try:
        label = scoring_label(kind="Abs")
        judgment, records = await judge.judge(
            sidecar=label,
            phase="after",
            question="Question?",
            answer_row=rows[0],
            run_id="run",
            replicate_id=0,
            profile="adaptive",
        )
        assert judgment.correct is True
        assert sent[-1]["response_format"] == {"type": "json_object"}
        assert sent[-1]["model"] == "gpt-4o" and sent[-1]["temperature"] == 0
        assert sent[-1]["messages"][0]["content"] == official_prompt(
            judge.judge_class,
            sidecar=label,
            phase="after",
            question="Question?",
            answer=rows[0]["answer"],
        )
        assert "MUST NOT SUBSTITUTE THIS GOLD" not in sent[-1]["messages"][0]["content"]
    finally:
        await judge.close()


@pytest.mark.asyncio
async def test_every_public_factory_role_receives_parameters():
    from examples.knowledge_fixture import configuration, secrets
    from contexthub.knowledge_config import KnowledgeConfig
    from contexthub.services.knowledge_factory import create_knowledge_service

    data = configuration().model_dump(mode="json")
    data["extraction"] = cfg("extraction").model_dump(mode="json")
    data["discovery"]["adjudication"]["weak"] = cfg("p1_weak").model_dump(mode="json")
    data["discovery"]["adjudication"]["strong"] = cfg("p1_strong").model_dump(
        mode="json"
    )
    for role in ["base", "strong", "verifier", "change_location"]:
        data[role] = cfg(role).model_dump(mode="json")
    data["rules"] = []
    # No repository method is invoked during composition; only real model objects.
    service = await create_knowledge_service(
        None,
        KnowledgeConfig.model_validate(data),
        secrets(),
        context_service=None,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    try:
        # Inspect all constructed clients without any database operations.
        assert (
            service.extractor.client._request_parameters
            == cfg("extraction").request_parameters
        )
        for role in ("weak", "strong"):
            assert (
                service.discovery.adjudicator.clients[role]._request_parameters
                == cfg("p1_" + role).request_parameters
            )
        for role in ("base", "strong", "verifier"):
            assert (
                getattr(service.runner, role).client._request_parameters
                == cfg(role).request_parameters
            )
        assert (
            service.locator.client._request_parameters
            == cfg("change_location").request_parameters
        )
    finally:
        await service.close()


def test_unknown_seed_real_gate_before_external_calls(tmp_path):
    from test_memebench_patha_run_gates import manifest_fixture

    manifest, grant = manifest_fixture(tmp_path)
    stats = StatisticsConfiguration.model_validate_json(
        (DRAFT / "statistics.draft.json").read_text()
    )
    manifest = manifest.model_copy(update={"statistics": stats})
    with pytest.raises(ContractError, match="provider_seed_support_unknown"):
        stage_preflight(
            manifest,
            grant(manifest),
            "rq1",
            configuration_sha256=manifest.configuration_sha256[0],
        )


def test_incomplete_stage_draft_cannot_be_execution_plan():
    from integrations.memebench.patha.cli import StagePlan

    with pytest.raises(ValidationError):
        StagePlan.model_validate_json((DRAFT / "plans/rq1.draft.json").read_text())
