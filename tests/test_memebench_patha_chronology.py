import pytest
from contexthub.models.knowledge import ContractError
from integrations.memebench.patha.chronology import QuestionVault, prefix_hash
from integrations.memebench.patha.schemas import canonical_sha256
from s8_helpers import materials


def test_inclusive_boundary_and_lazy_release(tmp_path, monkeypatch):
    from pathlib import Path

    runtime, boundary = materials(tmp_path / "questions")
    boundary.check(runtime)
    assert [s.ordinal for s in runtime.sources[: boundary.before.ordinal + 1]] == [0, 1]
    assert [
        s.ordinal
        for s in runtime.sources[
            boundary.before.ordinal + 1 : boundary.after.ordinal + 1
        ]
    ] == [2, 3]
    reads = []
    original = Path.read_text

    def read(p, *a, **k):
        reads.append(p.name)
        return original(p, *a, **k)

    monkeypatch.setattr(Path, "read_text", read)
    vault = QuestionVault(tmp_path / "questions", boundary)
    assert reads == []
    state = dict(
        episode_id=runtime.episode_id,
        phase="before",
        prefix_sha256=prefix_hash(runtime, 1),
    )
    state["sha256"] = canonical_sha256(state)
    with pytest.raises(ContractError):
        vault.release("after", state)
    assert reads == []
    bundle = vault.release("before", state)
    assert len(bundle.questions) == 2 and len(reads) == 1
    with pytest.raises(ContractError):
        vault.release("before", state)


def test_boundary_identity_and_mutated_bundle_rejected(tmp_path):
    runtime, boundary = materials(tmp_path / "q")
    with pytest.raises(ContractError):
        boundary.model_copy(update={"input_sha256": "0" * 64}).check(runtime)
    p = tmp_path / "q" / boundary.before.filename
    p.write_text(p.read_text() + " ")
    state = dict(
        phase="before",
        episode_id=runtime.episode_id,
        prefix_sha256=boundary.before.prefix_sha256,
    )
    state["sha256"] = canonical_sha256(state)
    with pytest.raises(ContractError, match="question_file_hash"):
        QuestionVault(tmp_path / "q", boundary).release("before", state)
