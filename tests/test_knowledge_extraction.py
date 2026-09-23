import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from contexthub.discovery.extraction import ExtractionConfig, ExtractionService
from contexthub.models.knowledge import ContractError, SealRef
from contexthub.services.artifact_service import ArtifactService
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from discovery_helpers import (
    MemoryDB,
    source,
    config,
    response,
    extract,
    transport,
    call_identity,
)
from knowledge_helpers import MemorySink, HASH


@pytest.mark.asyncio
async def test_real_extraction_round_trip_unicode_duplicates_and_conditions():
    db = MemoryDB()
    text = "  2026年😀 如果 A 变，B 也变。\n"
    ref = await source(db, text)
    raw = response(ref, text)
    raw["propositions"] *= 2
    obj, sink, requests = await extract(db, (ref,), [raw])
    assert obj.status == "complete" and len(obj.propositions) == 2
    assert obj.propositions[0].id != obj.propositions[1].id
    assert all(
        p.text == text and p.evidence[0].excerpt == text for p in obj.propositions
    )
    assert obj == await ArtifactService().get(db, obj.reference())
    sent = requests[0]["messages"][0]["content"]
    payload = json.loads(sent[sent.index("[") :])
    assert (
        payload[0]["text"] == text and payload[0]["snapshot"]["source"]["version"] == 1
    )
    model = [r for r in sink.rows if r.requested_model and r.phase == "finished"]
    assert len(model) == 1 and (
        model[0].usage.input_tokens,
        model[0].usage.output_tokens,
    ) == (13, 7)
    assert any(r.local_work for r in sink.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,status,reason",
    [
        ({"propositions": []}, "complete", None),
        ("", "pending", "ResponseSchemaError"),
        ("not JSON", "pending", "response_schema_invalid"),
        ({}, "pending", "response_schema_invalid"),
        (
            {"propositions": [], "commentary": "oops"},
            "pending",
            "response_schema_invalid",
        ),
    ],
)
async def test_empty_success_is_not_parse_failure(raw, status, reason):
    db = MemoryDB()
    ref = await source(db)
    obj, sink, req = await extract(db, (ref,), [raw])
    assert (
        obj.status == status and obj.failure_reason == reason and obj.propositions == ()
    )
    assert len(req) == 1
    model = [r for r in sink.rows if r.requested_model and r.phase == "finished"]
    assert model[0].usage.input_tokens == 13
    assert model[0].outcome == ("success" if status == "complete" else "error")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"source_id": str(uuid4())}, "undeclared_source"),
        ({"start": 1}, "source_span_mismatch"),
        ({"end": 1000}, "source_span_mismatch"),
        ({"excerpt": "trimmed"}, "source_span_mismatch"),
        ({"start": "0"}, "response_schema_invalid"),
    ],
)
async def test_invalid_reference_is_pending_once(change, reason):
    db = MemoryDB()
    ref = await source(db, " 中文😀 ")
    raw = response(ref, " 中文😀 ")
    raw["propositions"][0]["evidence"][0].update(change)
    obj, sink, req = await extract(db, (ref,), [raw])
    assert obj.failure_reason == reason and obj.status == "pending" and len(req) == 1
    assert [r for r in sink.rows if r.requested_model][-1].outcome == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["missing_seal", "missing_version", "changed_text", "wrong_hash"]
)
async def test_source_read_failure_can_be_sealed(failure):
    db = MemoryDB()
    ref = await source(db)
    if failure == "missing_seal":
        db.artifacts.pop(ref.id)
    elif failure == "missing_version":
        db.versions.clear()
    elif failure == "changed_text":
        next(iter(db.versions.values()))["l2_content"] = "changed"
    else:
        ref = ref.model_copy(update={"sha256": "0" * 64})
    obj, _, req = await extract(db, (ref,), [{"propositions": []}])
    assert obj.status == "pending" and obj.failure_reason and not req
    assert obj.sources == (ref,)
    assert await ArtifactService().get(db, obj.reference()) == obj
    if failure in ("missing_seal", "wrong_hash"):
        with pytest.raises(ContractError):
            await ArtifactService().seal(
                db,
                obj.model_copy(
                    update={
                        "id": uuid4(),
                        "status": "complete",
                        "failure_reason": None,
                    }
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,expected", [([503, 503, 503], "pending"), ([429, 200], "complete")]
)
async def test_recorded_retry_owner(statuses, expected):
    db = MemoryDB()
    ref = await source(db)
    obj, sink, req = await extract(
        db, (ref,), [(status, {"propositions": []}) for status in statuses]
    )
    assert obj.status == expected and len(req) == len(statuses)
    finishes = [r for r in sink.rows if r.requested_model and r.phase == "finished"]
    assert [r.attempt_no for r in finishes] == list(range(1, len(statuses) + 1))
    assert sum(r.usage.input_tokens for r in finishes) == 13 * len(statuses)
    assert finishes[-1].stop_reason == (
        "success" if expected == "complete" else "retry_exhausted"
    )


@pytest.mark.asyncio
async def test_compatibility_entry_delegates_once_and_old_entry_works():
    db, sink, requests = MemoryDB(), MemorySink(), []
    ref = await source(db)
    cfg = config()
    core = ExtractionService(
        cfg,
        api_key="fixture",
        recorder=sink,
        transport=transport([{"propositions": []}], requests),
    )
    try:
        obj = await ConversationExtractionService.extract_snapshot(
            db, (ref,), core=core, identity=call_identity(cfg)
        )
        assert obj.status == "complete" and len(requests) == 1
    finally:
        await core.close()

    class OldChat:
        async def complete(self, prompt, max_tokens):
            return "- one\n- two"

    assert [
        p.text for p in await ConversationExtractionService(OldChat()).extract("hello")
    ] == ["one", "two"]


def test_explicit_configuration_load_and_template_validation(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(config().model_dump_json())
    assert ExtractionConfig.load(path) == config()
    for template in (
        "no sources",
        "{conversation}",
        "{sources_json!r}",
        "{sources_json}{sources_json}",
    ):
        with pytest.raises((ValidationError, ValueError)):
            config(prompt_template=template)
    with pytest.raises(ValidationError):
        ExtractionConfig.model_validate({})


@pytest.mark.asyncio
async def test_pending_exception_does_not_allow_foreign_tenant():
    db = MemoryDB()
    ref = SealRef(account_id="other", id=uuid4(), kind="source", sha256=HASH)
    with pytest.raises(ContractError, match="invalid_source_identity"):
        await extract(db, (ref,), [{"propositions": []}])


@pytest.mark.asyncio
async def test_partial_unicode_span_and_identical_text_from_two_sources():
    db = MemoryDB()
    text = " \n前😀条件：如果A，B。尾 "
    refs = (await source(db, text), await source(db, text))
    start, end = text.index("条件"), text.index("尾")
    raw = {"propositions": []}
    for ref in refs:
        prop = response(ref, text)["propositions"][0]
        prop["text"] = "条件：如果A，B。"
        prop["evidence"][0].update(start=start, end=end, excerpt=text[start:end])
        raw["propositions"].append(prop)
    obj, _, _ = await extract(db, refs, [raw])
    assert obj.status == "complete" and len(obj.propositions) == 2
    assert {p.evidence[0].source for p in obj.propositions} == set(refs)
    assert all(
        p.evidence[0].start == start and p.evidence[0].excerpt == text[start:end]
        for p in obj.propositions
    )


@pytest.mark.asyncio
async def test_outer_same_call_cannot_send_again():
    db, sink, requests = MemoryDB(), MemorySink(), []
    ref = await source(db)
    cfg = config()
    core = ExtractionService(
        cfg,
        api_key="fixture",
        recorder=sink,
        transport=transport([(503, {"propositions": []})], requests),
    )
    ident = call_identity(cfg)
    try:
        obj = await core.extract(db, (ref,), identity=ident)
        assert obj.status == "pending"
        with pytest.raises(ContractError, match="attempt_already_recorded"):
            await core.extract(db, (ref,), identity=ident)
        assert len(requests) == 3
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_timeout_exhaustion_retains_unknown_usage():
    import httpx

    db = MemoryDB()
    ref = await source(db)
    obj, sink, requests = await extract(db, (ref,), [httpx.ReadTimeout("fixture")])
    assert (
        obj.status == "pending"
        and obj.failure_reason == "ReadTimeout"
        and len(requests) == 3
    )
    finishes = [r for r in sink.rows if r.requested_model and r.phase == "finished"]
    assert all(
        r.usage.input_tokens is None and r.usage.output_tokens is None for r in finishes
    )
    assert finishes[-1].stop_reason == "retry_exhausted"
