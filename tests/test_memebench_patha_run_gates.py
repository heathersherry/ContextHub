"""Gate-only checks against immutable S1 ID manifests; never construct models.

No episode conversations, questions or labels are opened in these tests.
The in-memory development grant below is a test fixture, not run authorization.
"""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import pytest
from contexthub.models.knowledge import ContractError, canonical_hash
from integrations.memebench.patha.artifacts import file_hash
from integrations.memebench.patha.run_gates import (
    FileIdentity,
    RunManifest,
    RunAuthorization,
    stage_preflight,
)
from integrations.memebench.patha.reporting import dependency_identity
from test_memebench_patha_bootstrap import protocol

S1 = Path(
    "/Users/sherrylin/Documents/PythonProjects/ContextHub-research-plan/research/proposal/experiments/pathA-rq1-rq2"
)


def manifest_fixture(tmp_path):
    def ref(path):
        return FileIdentity(path=str(path), sha256=file_hash(path))

    contract_path = S1 / "manifests/scorer-contract-v2.json"
    contract = json.loads(contract_path.read_text())
    lock = json.loads((S1 / "manifests/forward-lock-v1.json").read_text())
    identity = dependency_identity(ref(contract_path), output=tmp_path / "identity")
    artifact = tmp_path / "explicit-test-identity.json"
    artifact.write_text('{"fixture":"GATE ONLY; NOT EXECUTABLE CONFIGURATION"}')
    config = "a" * 64
    dev = lock["development_episodes"][0]["episode_id"]
    manifest = RunManifest(
        version="patha-s8-run-v1",
        run_id=uuid4(),
        mode="development",
        s1_root=str(S1),
        s1_material_version="v2",
        s1_contract=ref(contract_path),
        frozen_files=tuple(
            FileIdentity(path=str(S1 / p), sha256=h)
            for p, h in contract["artifact_hashes"].items()
        ),
        code_files=tuple(
            FileIdentity(path=p, sha256=h) for p, h in identity["code_files"].items()
        ),
        code_commit=identity["code_commit"],
        dirty_diff=ref(tmp_path / "identity/dirty.diff"),
        configuration_sha256=(config,),
        scorer_identity=ref(tmp_path / "identity/identity.json"),
        development=dict(
            id="synthetic-gate-test-only",
            episode_ids=(dev,),
            candidate_configuration_sha256=(config,),
            lambda_p1_grid=(0,),
            lambda_p2_grid=(0,),
            tie_break="lower_cost_then_smaller_lambda",
            fallback="smallest_frozen_lambda",
        ),
        statistics=protocol(),
        episode_ids=(dev,),
        main_graphs={},
        model_configuration=ref(artifact),
        price_identity=ref(artifact),
        timeout_identity=ref(artifact),
        runtime_dependencies=ref(tmp_path / "identity/runtime-dependencies.json"),
    )

    def authorization(value, stages=("rq1", "rq2", "answer", "judge")):
        return RunAuthorization(
            run_id=value.run_id,
            manifest_sha256=canonical_hash(value),
            mode=value.mode,
            stages=stages,
            user_authorization_reference="SYNTHETIC UNIT TEST ONLY; NO REAL RUN PERMISSION",
        )

    return manifest, authorization


def test_rq1_needs_no_future_graph_but_rq2_requires_it_and_no_early_questions(
    tmp_path, monkeypatch
):
    manifest, grant = manifest_fixture(tmp_path)
    original_text, original_bytes = Path.read_text, Path.read_bytes

    def deny_privileged(path):
        assert not (
            str(path).startswith(str(S1))
            and path.parent.name in ("answers", "labels", "inputs")
        ), "runtime gate read episode payload"

    def text(path, *a, **k):
        deny_privileged(path)
        return original_text(path, *a, **k)

    def binary(path, *a, **k):
        deny_privileged(path)
        return original_bytes(path, *a, **k)

    monkeypatch.setattr(Path, "read_text", text)
    monkeypatch.setattr(Path, "read_bytes", binary)
    assert (
        stage_preflight(manifest, grant(manifest), "rq1", configuration_sha256="a" * 64)
        == manifest
    )
    with pytest.raises(ContractError, match="main_graphs_not_frozen"):
        stage_preflight(manifest, grant(manifest), "rq2", configuration_sha256="a" * 64)
    with pytest.raises(ContractError, match="configuration_not_frozen"):
        stage_preflight(manifest, grant(manifest), "rq1", configuration_sha256="b" * 64)
    with pytest.raises(ContractError, match="binding_mismatch"):
        stage_preflight(
            manifest, grant(manifest, ("rq1",)), "answer", configuration_sha256="a" * 64
        )
    # No persisted formal manifest is fabricated, even in the positive gate test.
    assert manifest.mode == "development" and not manifest.main_graphs


@pytest.mark.parametrize(
    "fault",
    ["code", "frozen", "scorer", "provider_seed", "locked_episode", "candidates"],
)
def test_precise_frozen_identity_rejections(tmp_path, fault):
    manifest, grant = manifest_fixture(tmp_path)
    updates = {}
    if fault == "code":
        updates["code_files"] = manifest.code_files[:-1]
    if fault == "frozen":
        updates["frozen_files"] = manifest.frozen_files[:-1]
    if fault == "scorer":
        updates["scorer_identity"] = manifest.model_configuration
    if fault == "provider_seed":
        updates["statistics"] = manifest.statistics.model_copy(
            update={"provider_seed_supported": True, "provider_seed": 5}
        )
    if fault == "locked_episode":
        lock = json.loads((S1 / "manifests/forward-lock-v1.json").read_text())
        updates["episode_ids"] = (lock["forward_locked_episodes"][0]["episode_id"],)
    if fault == "candidates":
        updates["development"] = manifest.development.model_copy(
            update={"candidate_configuration_sha256": ("b" * 64,)}
        )
    bad = manifest.model_copy(update=updates)
    with pytest.raises(ContractError):
        stage_preflight(bad, grant(bad), "rq1", configuration_sha256="a" * 64)


def test_missing_prices_reject_before_public_factory_and_decimal_grid(
    tmp_path, monkeypatch
):
    from integrations.memebench.patha.system_factory import (
        SystemConfiguration,
        preflight_system,
    )
    from examples.knowledge_fixture import configuration

    config = configuration().model_copy(update={"extraction_price": None})
    manifest, grant = manifest_fixture(tmp_path)
    digest = canonical_hash(config)
    manifest = manifest.model_copy(
        update={
            "configuration_sha256": (digest,),
            "development": manifest.development.model_copy(
                update={
                    "candidate_configuration_sha256": (digest,),
                    "lambda_p1_grid": (
                        Decimal(str(config.discovery.search.lambda_p1)),
                    ),
                    "lambda_p2_grid": (config.profile.planner.lambda_p2,),
                }
            ),
        }
    )
    value = SystemConfiguration(
        mode="development",
        knowledge=config,
        manifest=manifest,
        authorization=grant(manifest),
    )
    with pytest.raises(ContractError, match="price_configuration_missing"):
        preflight_system(value, transport=None)
