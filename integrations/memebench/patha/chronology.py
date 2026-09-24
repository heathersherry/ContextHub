"""S8 boundaries and lazy question release; S1 files and identities are unchanged."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from contexthub.models.knowledge import Contract, ContractError, Hash
from .artifacts import file_hash
from .schemas import AnswerBundle, MaintenanceInput, canonical_sha256


def prefix_hash(runtime, end):
    return canonical_sha256(
        [
            dict(
                source_id=s.source_id,
                source_version=s.source_version,
                content_sha256=s.content_sha256,
            )
            for s in runtime.sources[: end + 1]
        ]
    )


class QuestionReference(Contract):
    phase: Literal["before", "after"]
    ordinal: int = Field(ge=0)
    source_id: str
    prefix_sha256: Hash
    file_sha256: Hash
    bundle_sha256: Hash
    filename: str


class EpisodeBoundary(Contract):
    episode_id: str
    input_sha256: Hash
    before: QuestionReference
    after: QuestionReference

    @model_validator(mode="after")
    def order(self):
        if (
            self.before.phase != "before"
            or self.after.phase != "after"
            or self.before.ordinal >= self.after.ordinal
        ):
            raise ContractError("episode_boundary_order")
        return self

    def check(self, runtime):
        if type(runtime) is not MaintenanceInput or (
            self.episode_id,
            self.input_sha256,
        ) != (runtime.episode_id, runtime.input_sha256):
            raise ContractError("episode_input_identity_mismatch")
        for ref in (self.before, self.after):
            if (
                ref.ordinal >= len(runtime.sources)
                or ref.source_id != runtime.sources[ref.ordinal].source_id
                or ref.prefix_sha256 != prefix_hash(runtime, ref.ordinal)
            ):
                raise ContractError("episode_source_boundary_mismatch")


class QuestionVault:
    """Only release() opens a bundle; construction never reads question bytes."""

    def __init__(self, directory, boundary):
        self.directory, self.boundary = Path(directory), boundary
        self.released = set()

    def release(self, phase, sealed_state):
        ref = getattr(self.boundary, phase)
        if (
            phase in self.released
            or sealed_state["phase"] != phase
            or sealed_state["episode_id"] != self.boundary.episode_id
            or sealed_state["prefix_sha256"] != ref.prefix_sha256
        ):
            raise ContractError("question_release_boundary")
        state = dict(sealed_state)
        digest = state.pop("sha256")
        if canonical_sha256(state) != digest:
            raise ContractError("question_state_hash_mismatch")
        if Path(ref.filename).name != ref.filename:
            raise ContractError("question_filename_invalid")
        path = self.directory / ref.filename
        if file_hash(path) != ref.file_sha256:
            raise ContractError("question_file_hash_mismatch")
        bundle = AnswerBundle.model_validate_json(path.read_text())
        if (
            bundle.episode_id,
            bundle.phase,
            bundle.release_after_ordinal,
            bundle.release_after_source_id,
            bundle.source_prefix_sha256,
            bundle.bundle_sha256,
        ) != (
            self.boundary.episode_id,
            phase,
            ref.ordinal,
            ref.source_id,
            ref.prefix_sha256,
            ref.bundle_sha256,
        ):
            raise ContractError("question_bundle_identity_mismatch")
        self.released.add(phase)
        return bundle


def export_question_packages(answer_rows, directory):
    """Offline S1 material packaging, never called by maintenance or routing.

    New files/reference identities preserve every question and old S1 bytes.
    position_after_session is inclusive, as in official run_agent.py (+1).
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    references = {}
    for row in answer_rows:
        bundle = AnswerBundle.model_validate(row)
        name = bundle.bundle_id + ".json"
        path = directory / name
        with path.open("x") as handle:
            json.dump(bundle.model_dump(mode="json"), handle, ensure_ascii=False)
        key = (bundle.episode_id, bundle.phase)
        if key in references:
            raise ContractError("duplicate_question_bundle")
        references[key] = QuestionReference(
            phase=bundle.phase,
            ordinal=bundle.release_after_ordinal,
            source_id=bundle.release_after_source_id,
            prefix_sha256=bundle.source_prefix_sha256,
            file_sha256=file_hash(path),
            bundle_sha256=bundle.bundle_sha256,
            filename=name,
        )
    return references


def package_s1_questions(plan, output):
    """Explicit offline packaging command; never called from a runtime gate."""
    from .run_gates import FileIdentity
    from .firewall import validate_maintenance_payload
    from .artifacts import ArtifactStore

    if set(plan) != {"maintenance_inputs", "answer_bundles"}:
        raise ContractError("question_packaging_plan_invalid")
    input_ref, answer_ref = (
        FileIdentity.model_validate(plan[k])
        for k in ("maintenance_inputs", "answer_bundles")
    )
    input_ref.verify()
    answer_ref.verify()
    inputs = [
        validate_maintenance_payload(json.loads(line))
        for line in Path(input_ref.path).read_text().split("\n")
        if line
    ]
    rows = [
        json.loads(line)
        for line in Path(answer_ref.path).read_text().split("\n")
        if line
    ]
    store = ArtifactStore(output)
    refs = export_question_packages(rows, store.directory / "questions")
    if len(refs) != 2 * len(inputs):
        raise ContractError("question_package_episode_count")
    boundaries = {}
    for runtime in inputs:
        boundary = EpisodeBoundary(
            episode_id=runtime.episode_id,
            input_sha256=runtime.input_sha256,
            before=refs[(runtime.episode_id, "before")],
            after=refs[(runtime.episode_id, "after")],
        )
        boundary.check(runtime)
        if runtime.episode_id in boundaries:
            raise ContractError("duplicate_input_episode")
        boundaries[runtime.episode_id] = boundary.model_dump(mode="json")
    store.put("boundaries.json", boundaries)
    store.put("source-identities.json", plan)
    return dict(
        directory=str(store.directory),
        episodes=len(inputs),
        question_files=len(refs),
        boundaries_sha256=file_hash(store.directory / "boundaries.json"),
        real_execution_authorized=False,
    )
