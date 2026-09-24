import copy
from uuid import uuid4
import pytest
from pydantic import ValidationError
from contexthub.models.knowledge import ContractError
from contexthub.models.knowledge_phases import (
    PrepareRequest,
    AssertionChangeRequest,
)
from contexthub.services.assertion_change import AssertionLocation, validate_location
from integrations.memebench.patha.system_factory import (
    SystemConfiguration,
    create_system,
)
from integrations.memebench.patha.public_rq1_runner import PublicRQ1Configuration
from examples.knowledge_fixture import configuration


def fixture():
    carrier, original, new = map(str, (uuid4(), uuid4(), uuid4()))
    ident = str(uuid4())

    def span(source, text, start=0):
        return dict(
            source_id=source,
            start=start,
            end=start + len(text),
            excerpt=text,
            provenance=["synthetic"],
        )

    raw = dict(
        context_id=str(uuid4()), version=1, snapshot_id=new, text="Now A is 2. Keep X."
    )
    old = dict(
        context_id=ident,
        version=1,
        snapshot_id=carrier,
        text="A is 1",
        assertions=[dict(source=dict(id=original), start=8, end=14, excerpt="A is 1")],
    )
    match = dict(
        carrier_id=ident,
        version=1,
        old=span(carrier, "A is 1"),
        original_assertion=span(original, "A is 1", 8),
        replacement=span(new, "A is 2", 4),
        identity_basis=span(new, "A is 2", 4),
        temporal_basis=span(new, "Now"),
        relation="same_subject_same_attribute_direct_assertion_replacement",
    )
    return raw, [old], dict(status="located", reason="synthetic", matches=[match])


def test_partial_original_source_supported_without_rewriting():
    raw, candidates, data = fixture()
    value = AssertionLocation.model_validate(data)
    assert validate_location(value, raw, candidates) == value
    assert value.matches[0].original_assertion.start == 8


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "version",
        "quote",
        "snapshot",
        "partial_carrier",
        "old_evidence",
        "unchanged",
        "duplicate",
        "overlap",
        "ambiguous",
        "derived_relation",
    ],
)
def test_assertion_fail_closed(fault):
    raw, candidates, data = fixture()
    m = data["matches"][0]
    if fault == "unknown":
        m["carrier_id"] = str(uuid4())
    elif fault == "version":
        m["version"] = 2
    elif fault == "quote":
        m["replacement"]["excerpt"] = "made up"
    elif fault == "snapshot":
        m["replacement"]["source_id"] = str(uuid4())
    elif fault == "partial_carrier":
        m["old"].update(start=2, excerpt="is 1")
    elif fault == "old_evidence":
        m["original_assertion"]["start"] = 0
    elif fault == "unchanged":
        candidates[0]["text"] = "A is 2"
        m["old"]["excerpt"] = "A is 2"
    elif fault == "duplicate":
        data["matches"].append(copy.deepcopy(m))
    elif fault == "overlap":
        other = copy.deepcopy(candidates[0])
        other.update(context_id=str(uuid4()), snapshot_id=str(uuid4()), text="B is 1")
        candidates.append(other)
        n = copy.deepcopy(m)
        n["carrier_id"] = other["context_id"]
        n["old"].update(source_id=other["snapshot_id"], excerpt="B is 1")
        data["matches"].append(n)
    elif fault == "ambiguous":
        other = copy.deepcopy(candidates[0])
        other["context_id"] = str(uuid4())
        candidates.append(other)
    elif fault == "derived_relation":
        m["relation"] = "derived_recompute"
    with pytest.raises((ContractError, ValidationError)):
        validate_location(AssertionLocation.model_validate(data), raw, candidates)


@pytest.mark.parametrize(
    "field", ["task_type", "hop", "gold", "question", "root_change", "path_labels"]
)
def test_public_phase_allowlists(field):
    with pytest.raises(ValidationError):
        PrepareRequest(request_id=uuid4(), content="intact raw", **{field: "forbidden"})
    ref = dict(account_id="test", id=uuid4(), kind="graph_handoff", sha256="0" * 64)
    with pytest.raises(ValidationError):
        AssertionChangeRequest(
            request_id=uuid4(),
            content="intact raw",
            handoff=ref,
            **{field: "forbidden"},
        )


@pytest.mark.asyncio
async def test_real_mode_or_non_mock_transport_zero_client_construction(monkeypatch):
    import integrations.memebench.patha.system_factory as module

    calls = []

    async def forbidden(*a, **k):
        calls.append(1)
        raise AssertionError("core construction forbidden")

    monkeypatch.setattr(module, "create_knowledge_service", forbidden)
    with pytest.raises(ValidationError):
        SystemConfiguration(mode="formal", knowledge=configuration())
    with pytest.raises(ContractError, match="synthetic_transport_required"):
        await create_system(
            SystemConfiguration(mode="synthetic", knowledge=configuration()),
            repository=None,
            context_service=None,
            secrets=None,
            transport=None,
        )
    assert not calls


def test_rq1_fairness_preserves_all_other_settings():
    selective = configuration().model_dump(mode="json")
    selective["discovery"]["search"]["strategy"] = "selective"
    full = copy.deepcopy(selective)
    full["discovery"]["search"]["strategy"] = "full-history"
    pair = dict(
        selective=dict(mode="synthetic", knowledge=selective),
        full_history=dict(mode="synthetic", knowledge=full),
    )
    assert PublicRQ1Configuration.model_validate(pair)
    full["extraction"]["max_tokens"] += 1
    with pytest.raises(ValidationError, match="rq1_frozen_conditions_mismatch"):
        PublicRQ1Configuration.model_validate(pair)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://s8_app@localhost:55619/contexthub_s8_test",
        "postgresql://s8_app@127.0.0.1:5432/contexthub_s8_test",
        "postgresql://s8_app@127.0.0.1:55619/business",
        "postgresql://s8_app:secret@127.0.0.1:55619/contexthub_s8_test",
        "postgresql://s8_app@127.0.0.1:55619/contexthub_s8_test?host=external",
    ],
)
def test_snapshot_rejects_non_dedicated_or_secret_dsn(dsn):
    from integrations.memebench.patha.database_snapshot import (
        DatabaseIdentity,
        local_dsn,
    )

    identity = DatabaseIdentity(
        system_identifier="synthetic",
        data_directory="/private/tmp/synthetic",
        port=55619,
        database="contexthub_s8_test",
        ordinary_role="s8_app",
        schema_revision="011",
    )
    with pytest.raises(ContractError, match="dedicated_local_database_required"):
        local_dsn(dsn, identity)
