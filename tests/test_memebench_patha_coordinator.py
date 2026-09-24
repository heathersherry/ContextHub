from types import SimpleNamespace
from uuid import uuid4
import pytest
from contexthub.models.knowledge import ContractError, canonical_hash
from integrations.memebench.patha.rq2_coordinator import validate_main
from integrations.memebench.patha.system_factory import (
    SystemConfiguration,
    create_system,
)
from integrations.memebench.patha.answer_runner import AnswerRunner
from integrations.memebench.patha.run_gates import (
    stage_preflight,
    StatisticsConfiguration,
)
from examples.knowledge_fixture import configuration


@pytest.mark.parametrize(
    "fault", ["run", "replicate", "missing", "configuration", "input", "handoff"]
)
def test_main_binding_rejects_wrong_identity(fault):
    run = uuid4()
    cfg = configuration()
    handoff = "sealed"
    snapshot = SimpleNamespace(
        run_id=str(run), handoff=handoff, configuration_sha256=canonical_hash(cfg)
    )
    result = SimpleNamespace(
        run_id=run,
        replicate_id=0,
        selective_snapshot=snapshot,
        handoffs=(handoff, "control"),
        input_sha256="0" * 64,
    )
    if fault == "run":
        result.run_id = uuid4()
    if fault == "replicate":
        result.replicate_id = 1
    if fault == "missing":
        result.selective_snapshot = None
    if fault == "configuration":
        snapshot.configuration_sha256 = "1" * 64
    if fault == "input":
        result.input_sha256 = "1" * 64
    if fault == "handoff":
        snapshot.handoff = "another"
    with pytest.raises(ContractError):
        validate_main(
            result,
            expected_run_id=run,
            runtime=SimpleNamespace(input_sha256="0" * 64),
            configuration=cfg,
        )


@pytest.mark.asyncio
async def test_missing_authorization_and_transport_zero_clients(monkeypatch):
    import integrations.memebench.patha.system_factory as factory
    import integrations.memebench.patha.answer_runner as answers

    constructed = []

    def forbidden(*a, **k):
        constructed.append(True)
        raise AssertionError("client construction forbidden")

    monkeypatch.setattr(factory, "create_knowledge_service", forbidden)
    monkeypatch.setattr(answers, "OpenAIChatClient", forbidden)
    for stage in ("rq1", "rq2", "answer", "judge"):
        with pytest.raises(ContractError, match="separate_run_authorization"):
            stage_preflight(None, None, stage, configuration_sha256="0" * 64)
    with pytest.raises(ValueError):
        SystemConfiguration(mode="formal", knowledge=configuration())
    with pytest.raises(ContractError):
        await create_system(
            SystemConfiguration(mode="synthetic", knowledge=configuration()),
            repository=None,
            context_service=None,
            secrets=None,
            transport=None,
        )
    with pytest.raises(ContractError):
        AnswerRunner(
            configuration().base,
            repository=None,
            account_id="answers",
            api_key="test",
            transport=None,
        )
    assert constructed == []


def test_no_statistics_protocol_defaults():
    with pytest.raises(ValueError):
        StatisticsConfiguration()
