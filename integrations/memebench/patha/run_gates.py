"""Versioned S8 run gates, additive to the immutable S1 validator identity."""

import math
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    Hash,
    Name,
    canonical_hash,
)
from .artifacts import file_hash
from contexthub.llm.request_parameters import SeedPolicy

STAGES = Literal["rq1", "rq2", "answer", "judge"]


class FileIdentity(Contract):
    path: str
    sha256: Hash

    def verify(self):
        if not Path(self.path).is_absolute() or file_hash(self.path) != self.sha256:
            raise ContractError("run_file_identity_mismatch")


class StatisticsConfiguration(Contract):
    repeats: int = Field(strict=True, ge=1)
    aggregate: Literal["pooled_counts", "mean_episode_rates"]
    bootstrap_seed: int = Field(strict=True, ge=0)
    bootstrap_resamples: Literal[10000]
    seed_protocol: Literal["legacy-v1", "per-model-v2"] = "legacy-v1"
    model_seeds: dict[str, SeedPolicy] | None = None
    provider_seed_supported: bool | None
    provider_seed: int | None
    wall_relative_tolerance: float | None = Field(ge=0, allow_inf_nan=False)
    local_relative_tolerances: dict[str, float] | None

    @model_validator(mode="after")
    def seed(self):
        if self.seed_protocol == "per-model-v2":
            if (
                self.provider_seed_supported is not None
                or self.provider_seed is not None
                or not self.model_seeds
            ):
                raise ContractError("per_model_seed_configuration_required")
        elif self.model_seeds is not None or self.provider_seed_supported is None:
            raise ContractError("legacy_seed_configuration_invalid")
        elif self.provider_seed_supported != (self.provider_seed is not None):
            raise ContractError("provider_seed_support_mismatch")
        if self.local_relative_tolerances is not None and any(
            v < 0 or not math.isfinite(v)
            for v in self.local_relative_tolerances.values()
        ):
            raise ContractError("local_cost_tolerance_invalid")
        return self


class FiniteDevelopmentManifest(Contract):
    id: Name
    episode_ids: tuple[Name, ...] = Field(min_length=1)
    candidate_configuration_sha256: tuple[Hash, ...] = Field(min_length=1)
    lambda_p1_grid: tuple[Decimal, ...] = Field(min_length=1)
    lambda_p2_grid: tuple[Decimal, ...] = Field(min_length=1)
    tie_break: Literal["lower_cost_then_smaller_lambda"]
    fallback: Literal["smallest_frozen_lambda"]

    @model_validator(mode="after")
    def finite(self):
        for grid in (self.lambda_p1_grid, self.lambda_p2_grid):
            if len(set(grid)) != len(grid) or any(
                not v.is_finite() or v < 0 for v in grid
            ):
                raise ContractError("development_grid_invalid")
        if len(set(self.episode_ids)) != len(self.episode_ids) or len(
            set(self.candidate_configuration_sha256)
        ) != len(self.candidate_configuration_sha256):
            raise ContractError("development_manifest_duplicates")
        return self


class MainGraphIdentity(Contract):
    run_id: UUID
    replicate_id: Literal[0]
    strategy: Literal["selective"]
    snapshot_file: FileIdentity
    graph_sha256: Hash
    handoff_sha256: Hash


class RunManifest(Contract):
    version: Literal["patha-s8-run-v1"]
    run_id: UUID
    mode: Literal["development", "formal"]
    s1_root: str
    s1_material_version: Literal["v2"]
    s1_contract: FileIdentity
    frozen_files: tuple[FileIdentity, ...] = Field(min_length=1)
    code_files: tuple[FileIdentity, ...] = Field(min_length=1)
    code_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dirty_diff: FileIdentity
    configuration_sha256: tuple[Hash, ...] = Field(min_length=1)
    scorer_identity: FileIdentity
    development: FiniteDevelopmentManifest
    statistics: StatisticsConfiguration
    episode_ids: tuple[Name, ...] = Field(min_length=1)
    main_graphs: dict[str, MainGraphIdentity]
    model_configuration: FileIdentity
    price_identity: FileIdentity
    timeout_identity: FileIdentity
    runtime_dependencies: FileIdentity
    two_episode_token_only_authorization: Name | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def token_only_scope(self):
        if self.two_episode_token_only_authorization is not None:
            expected = {"ep_8185eda1823fe081718f7d90", "ep_a6132508d3bb0eeae1c5b899"}
            if (self.mode != "development" or set(self.episode_ids) != expected
                or set(self.development.episode_ids) != expected
                or self.statistics.repeats != 1
                or self.development.lambda_p1_grid != (Decimal(0),)
                or self.development.lambda_p2_grid != (Decimal(0),)):
                raise ContractError("token_only_exception_scope_mismatch")
        return self


class RunAuthorization(Contract):
    run_id: UUID
    manifest_sha256: Hash
    mode: Literal["development", "formal"]
    stages: tuple[STAGES, ...] = Field(min_length=1)
    user_authorization_reference: Name


def stage_preflight(manifest, authorization, stage, *, configuration_sha256):
    """Before any external client. RQ1 does not require its future output graph."""
    import json
    import hashlib
    import importlib.metadata
    import subprocess

    if manifest is None or authorization is None:
        raise ContractError("separate_run_authorization_required")
    manifest = RunManifest.model_validate(manifest.model_dump(mode="json"))
    authorization = RunAuthorization.model_validate(
        authorization.model_dump(mode="json")
    )
    if (authorization.run_id, authorization.mode, authorization.manifest_sha256) != (
        manifest.run_id,
        manifest.mode,
        canonical_hash(manifest),
    ) or stage not in authorization.stages:
        raise ContractError("run_authorization_binding_mismatch")
    if configuration_sha256 not in manifest.configuration_sha256:
        raise ContractError("run_configuration_not_frozen")
    for ref in (
        *manifest.code_files,
        manifest.s1_contract,
        manifest.dirty_diff,
        manifest.scorer_identity,
        manifest.model_configuration,
        manifest.price_identity,
        manifest.timeout_identity,
        manifest.runtime_dependencies,
    ):
        ref.verify()
    code_root = Path(__file__).resolve().parents[3]
    required_code = {
        str(p.resolve())
        for folder in ("src/contexthub", "sdk/src", "integrations/memebench/patha")
        for p in (code_root / folder).rglob("*.py")
    }
    declared_code = {ref.path for ref in manifest.code_files}
    if len(declared_code) != len(manifest.code_files) or declared_code != required_code:
        raise ContractError("run_code_dependency_closure_incomplete")
    if (
        subprocess.check_output(
            ["git", "-C", str(code_root), "rev-parse", "HEAD"], text=True
        ).strip()
        != manifest.code_commit
        or hashlib.sha256(
            subprocess.check_output(
                ["git", "-C", str(code_root), "diff", "--binary", "HEAD"]
            )
        ).hexdigest()
        != manifest.dirty_diff.sha256
    ):
        raise ContractError("run_checkout_identity_mismatch")
    versions = json.loads(Path(manifest.runtime_dependencies.path).read_text())
    if not {"pydantic", "httpx", "asyncpg", "anyio"} <= set(versions) or any(
        importlib.metadata.version(package) != version
        for package, version in versions.items()
    ):
        raise ContractError("run_package_identity_mismatch")
    # Legacy identities retain their original rejection. New per-model seeds
    # must be resolved here and bound to each actual role before construction.
    if manifest.statistics.seed_protocol == "per-model-v2":
        if any(
            p.support == "unknown" and not (
                manifest.two_episode_token_only_authorization and not p.enabled and p.value is None
            ) for p in manifest.statistics.model_seeds.values()
        ):
            raise ContractError("provider_seed_support_unknown")
    elif manifest.statistics.provider_seed_supported:
        raise ContractError("provider_seed_not_supported_by_current_client")
    root = Path(manifest.s1_root)
    if Path(manifest.s1_contract.path) != root / "manifests/scorer-contract-v2.json":
        raise ContractError("run_s1_parent_mismatch")
    # Runtime gates read identity manifests, never the question/label payloads.
    # The complete S1 material audit remains the separate offline preflight.
    # StagePlan verifies allowed inputs/boundary headers, QuestionVault verifies
    # question bytes only after sealing, and the offline scorer verifies labels.
    from .prepare import _code_hashes
    from .scoring_contract import validate_scoring_contract
    from .forward_lock import validate_forward_lock_manifest

    allowed = json.loads((root / "manifests/allowed-inputs-v2.json").read_text())
    allowed_payload = dict(allowed)
    allowed_hash = allowed_payload.pop("manifest_sha256", None)
    if (
        canonical_hash(allowed_payload) != allowed_hash
        or allowed.get("generator_code_sha256") != _code_hashes()
    ):
        raise ContractError("s1_validator_identity_mismatch")
    parent = json.loads(Path(manifest.s1_contract.path).read_text())
    validate_scoring_contract(parent)
    if parent["contract_version"] != "scorer-contract-v2":
        raise ContractError("run_s1_v2_required")
    required_frozen = {
        str((root / p).resolve()): h for p, h in parent["artifact_hashes"].items()
    }
    declared_frozen = {
        str(Path(ref.path).resolve()): ref.sha256 for ref in manifest.frozen_files
    }
    if not required_frozen.items() <= declared_frozen.items():
        raise ContractError("run_s1_frozen_identity_incomplete")
    for relative in (
        "manifests/allowed-inputs-v2.json",
        "manifests/forward-lock-v1.json",
    ):
        if file_hash(root / relative) != parent["artifact_hashes"].get(relative):
            raise ContractError("s1_identity_manifest_hash_mismatch")
    scorer = json.loads(Path(manifest.scorer_identity.path).read_text())
    required_scorer = {ref.path: ref.sha256 for ref in manifest.code_files}
    if (
        scorer.get("s1_contract_sha256") != manifest.s1_contract.sha256
        or scorer.get("code_files") != required_scorer
    ):
        raise ContractError("run_scorer_identity_incomplete")
    lock = json.loads((root / "manifests/forward-lock-v1.json").read_text())
    validate_forward_lock_manifest(lock)
    development = {r["episode_id"] for r in lock["development_episodes"]}
    locked = {r["episode_id"] for r in lock["forward_locked_episodes"]}
    if (
        len(set(manifest.episode_ids)) != len(manifest.episode_ids)
        or not set(manifest.development.episode_ids) <= development
    ):
        raise ContractError("run_episode_universe_mismatch")
    if manifest.mode == "formal":
        if set(manifest.episode_ids) != development | locked:
            raise ContractError("formal_requires_all_100_episodes")
    elif not set(manifest.episode_ids) <= set(manifest.development.episode_ids):
        raise ContractError("development_episode_not_predeclared")
    if not set(manifest.configuration_sha256) <= set(
        manifest.development.candidate_configuration_sha256
    ):
        raise ContractError("configuration_outside_finite_development_manifest")
    if stage in ("rq2", "answer", "judge"):
        if set(manifest.main_graphs) != set(manifest.episode_ids):
            raise ContractError("selective_main_graphs_not_frozen")
        from .database_snapshot import DatabaseSnapshot

        for main in manifest.main_graphs.values():
            main.snapshot_file.verify()
            snapshot = DatabaseSnapshot.model_validate_json(
                Path(main.snapshot_file.path).read_text()
            )
            if (
                snapshot.run_id,
                snapshot.replicate_id,
                snapshot.strategy,
                snapshot.graph.sha256,
                snapshot.handoff.sha256,
            ) != (
                str(main.run_id),
                0,
                "selective",
                main.graph_sha256,
                main.handoff_sha256,
            ):
                raise ContractError("selective_main_identity_mismatch")
    return manifest


def validate_model_seed(manifest, config):
    """Bind the actually constructed role's seed contract to statistics identity."""
    policy = manifest.statistics
    params = config.request_parameters
    if policy.seed_protocol == "per-model-v2":
        if (
            params is None
            or policy.model_seeds.get(canonical_hash(config)) != params.seed
        ):
            raise ContractError("model_seed_identity_mismatch")
        if params.disabled_unknown_seed_authorization != getattr(manifest, "two_episode_token_only_authorization", None):
            raise ContractError("unknown_seed_exception_not_authorized")
        params.require_resolved()
    elif params is not None:
        params.require_resolved()
        if params.seed.enabled or params.seed.support != "unsupported":
            raise ContractError("per_model_seed_protocol_required")
