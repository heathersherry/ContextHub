"""Synthetic CLI plans never grant real execution or choose experiment defaults."""

from uuid import uuid4
from pathlib import Path
import json
import httpx
import pytest
from contexthub.models.knowledge import ContractError
from integrations.memebench.patha.cli import (
    StagePlan,
    preflight_plan,
    fixture_transport,
    execute_stage,
)
from integrations.memebench.patha.artifacts import file_hash
from integrations.memebench.patha.run_gates import FileIdentity
from integrations.memebench.patha.system_factory import SystemConfiguration
from integrations.memebench.patha.public_rq1_runner import PublicRQ1Configuration
from examples.knowledge_fixture import configuration
from s8_helpers import materials


def plan(tmp_path, database=None):
    allowed, boundary = materials(tmp_path / "questions")
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_text(allowed.model_dump_json() + "\n")
    boundaries = tmp_path / "boundaries.json"
    boundaries.write_text(
        json.dumps({allowed.episode_id: boundary.model_dump(mode="json")})
    )
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "import httpx\nfrom test_integration_knowledge_phases import Models\ndef build_transport():\n    return httpx.MockTransport(Models().send)\n"
    )

    def ref(path):
        return FileIdentity(path=str(path), sha256=file_hash(path))

    a = configuration()
    a = a.model_copy(
        update={
            "discovery": a.discovery.model_copy(
                update={
                    "search": a.discovery.search.model_copy(
                        update={"strategy": "selective"}
                    )
                }
            )
        }
    )
    b = a.model_copy(
        update={
            "discovery": a.discovery.model_copy(
                update={
                    "search": a.discovery.search.model_copy(
                        update={"strategy": "full-history"}
                    )
                }
            )
        }
    )
    db = database or dict(
        admin_dsn="postgresql://admin@127.0.0.1:55721/contexthub_s8_synthetic",
        identity=dict(
            system_identifier="synthetic-not-a-real-instance",
            data_directory="/tmp/synthetic",
            port=55721,
            database="contexthub_s8_synthetic",
            ordinary_role="s8_app",
            schema_revision="011",
        ),
        pg_bin="/not/a/real/binary",
    )
    return StagePlan(
        run_id=uuid4(),
        rq1=PublicRQ1Configuration(
            selective=SystemConfiguration(mode="synthetic", knowledge=a),
            full_history=SystemConfiguration(mode="synthetic", knowledge=b),
        ),
        answer=a.base,
        statistics=dict(
            repeats=1,
            aggregate="pooled_counts",
            bootstrap_seed=7,
            bootstrap_resamples=10000,
            provider_seed_supported=False,
            provider_seed=None,
            wall_relative_tolerance=None,
            local_relative_tolerances=None,
        ),
        database=db,
        maintenance_inputs=ref(inputs),
        boundaries=ref(boundaries),
        question_directory=str(tmp_path / "questions"),
        episode_ids=(allowed.episode_id,),
        accounts={allowed.episode_id: ("synthetic-" + uuid4().hex,)},
        agent_id="reader",
        main_runs={},
        credential_environment={},
        synthetic_responses=ref(fixture),
    )


def test_synthetic_plan_and_dynamic_fixture(tmp_path):
    value = plan(tmp_path)
    preflight_plan(value, "rq1")
    assert isinstance(fixture_transport(value), httpx.MockTransport)
    with pytest.raises(ContractError, match="main_runs_incomplete"):
        preflight_plan(value, "rq2")
    bad = value.model_copy(update={"synthetic_responses": None})
    with pytest.raises(ContractError, match="synthetic_responses_required"):
        preflight_plan(bad, "rq1")
    Path(value.synthetic_responses.path).write_text("changed")
    with pytest.raises(ContractError, match="identity_mismatch"):
        preflight_plan(value, "rq1")


@pytest.mark.asyncio
async def test_missing_credentials_before_connections(tmp_path, monkeypatch):
    import integrations.memebench.patha.cli as cli

    value = plan(tmp_path)
    calls = []

    async def forbidden(*a, **k):
        calls.append(1)
        raise AssertionError("connection forbidden")

    monkeypatch.setattr(cli.asyncpg, "connect", forbidden)
    with pytest.raises(ContractError, match="credential_environment_missing"):
        await execute_stage(value, "rq1", tmp_path / "out")
    assert calls == []
