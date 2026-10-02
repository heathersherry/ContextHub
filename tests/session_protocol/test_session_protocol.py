import copy

import pytest

from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.services.session_state import (
    Delta, Edge, Edit, Node, SessionState, Verification, apply_delta, release_fresh,
)
from integrations.memebench.patha.chronology import EpisodeBoundary, export_question_packages
from integrations.memebench.patha.prepare import build_answer_bundles, build_maintenance_inputs
from integrations.memebench.patha.schemas import MaintenanceInput, canonical_sha256
from integrations.memebench.patha.session_runner import SessionEpisodeRunner


def node(ident, kind="assertion", **kw):
    return Node(id=ident, version=1, text=ident, kind=kind,
                evidence=("source:1:span:0:1",), status="fresh", **kw)


def edge(a, b, status="confirmed"):
    return Edge(upstream=a, downstream=b, status=status, evidence=("rule:1:span:0:1",))


def base():
    return SessionState(nodes=(node("lead"), node("rule", "rule"), node("recipient", "derived"),
                               node("reminder", "derived"), node("unrelated")),
                        edges=(edge("lead", "recipient"), edge("rule", "recipient"),
                               edge("recipient", "reminder")))


def edit(ident, version=1, **kw):
    return Edit(id=ident, expected_version=version, text="new " + ident,
                kind="assertion", evidence=("source:2:span:0:1",), **kw)


def delta(state, edits=(), searched=(), edges=(), complete=True):
    return Delta(basis_sha256=canonical_hash(state), edits=edits,
                 searched=searched, edges=edges, complete=complete)


def proof(state, ident, **kw):
    nodes = {n.id: n for n in state.nodes}
    data = dict(basis_sha256=canonical_hash(state), target=ident, target_version=nodes[ident].version,
                upstream_versions=tuple(sorted((e.upstream, nodes[e.upstream].version)
                    for e in state.edges if e.downstream == ident)), supported=True, applicable=True,
                conflicts_resolved=True, coverage_complete=True, evidence=("checked:source:2",), receipt_id="verify-1")
    return Verification(**(data | kw))


def statuses(state):
    return {n.id: n.status for n in state.nodes}


def test_old_edges_not_lost_when_new_session_replaces_dependency():
    state = base()
    state, affected = apply_delta(state, delta(state, (edit("lead"), edit("reviewer", 0)),
        ("lead", "reviewer", "recipient"), (edge("reviewer", "recipient"),)))
    assert set(affected) == {"lead", "reviewer", "recipient", "reminder"}
    assert statuses(state)["unrelated"] == "fresh"
    assert statuses(state)["reminder"] == "stale"
    assert next(n.version for n in state.nodes if n.id == "lead") == 2


def test_topological_release_and_rule_update():
    state = base()
    state, _ = apply_delta(state, delta(state, (edit("lead"),), ("lead",)))
    with pytest.raises(ContractError, match="fresh_not_supported"):
        release_fresh(state, proof(state, "recipient"))
    for ident in ("lead", "recipient", "reminder"):
        state = release_fresh(state, proof(state, ident))
    assert all(n.status == "fresh" for n in state.nodes)
    state, _ = apply_delta(state, delta(state, (edit("rule"),), ("rule",)))
    assert statuses(state)["recipient"] == "stale"
    assert statuses(state)["reminder"] == "stale"


@pytest.mark.parametrize("field", ["supported", "applicable", "conflicts_resolved", "coverage_complete"])
def test_semantic_failure_cannot_release(field):
    state = base()
    with pytest.raises(ContractError, match="fresh_not_supported"):
        release_fresh(state, proof(state, "recipient", **{field: False}))


def test_missing_parent_and_old_receipt_rejected():
    state = base()
    with pytest.raises(ContractError, match="upstreams_incomplete"):
        release_fresh(state, proof(state, "recipient", upstream_versions=()))
    receipt = proof(state, "recipient")
    state, _ = apply_delta(state, delta(state, (edit("lead"),), ("lead",)))
    with pytest.raises(ContractError, match="basis_drift"):
        release_fresh(state, receipt)


def test_incomplete_search_preserves_edges_and_blocks_release():
    state = base()
    state, _ = apply_delta(state, delta(state, searched=("recipient",), complete=False))
    assert edge("lead", "recipient") in state.edges
    with pytest.raises(ContractError, match="fresh_not_supported"):
        release_fresh(state, proof(state, "recipient"))


def test_pending_edge_and_uncertain_class_are_not_proofs():
    state = base()
    state, _ = apply_delta(state, delta(state, searched=("recipient",),
                                       edges=(edge("lead", "recipient", "pending"),)))
    with pytest.raises(ContractError):
        release_fresh(state, proof(state, "recipient"))
    state = SessionState(nodes=(node("unknown", "uncertain"),))
    with pytest.raises(ContractError):
        release_fresh(state, proof(state, "unknown"))


def test_delete_invalidates_descendants_and_cannot_resurrect():
    state = base()
    state, _ = apply_delta(state, delta(state, (edit("lead", deleted=True),), ("lead",)))
    assert statuses(state)["lead"] == "deleted"
    assert statuses(state)["reminder"] == "stale"
    with pytest.raises(ContractError):
        release_fresh(state, proof(state, "lead"))
    with pytest.raises(ContractError):
        apply_delta(state, delta(state, (edit("lead", 2),), ("lead",)))


def test_version_conflict_and_unsearched_new_fact():
    state = base()
    with pytest.raises(ContractError, match="version_drift"):
        apply_delta(state, delta(state, (edit("lead", 0),)))
    state, _ = apply_delta(state, delta(state, (edit("new", 0),)))
    with pytest.raises(ContractError):
        release_fresh(state, proof(state, "new"))


def package(tmp_path, before=1, after=4):
    episode = dict(episode_id="private-episode", sessions=[
        dict(session_id="evidence_CHANGE" if i == 2 else "filler", type="evidence",
             gold_facts=["SECRET"], timestamp=f"2026-01-0{i+1}",
             conversation=[dict(role="user", content=t), dict(role="assistant", content="ok")])
        for i, t in enumerate(("Alice leads", "ordinary update to Bob", "new rule and forget name", "later chat", "last chat"))])
    for phase, position, count in (("before", before, 2), ("after", after, 3)):
        episode[phase + "_questions"] = dict(position_after_session=position, questions=[
            dict(question=f"{phase}-{i}", task_type="SECRET", gold_answer="SECRET") for i in range(count)])
    rows, by_id = build_maintenance_inputs([episode], "a" * 64)
    bundles, _ = build_answer_bundles([episode], "a" * 64, by_id)
    runtime = MaintenanceInput.model_validate(rows[0])
    directory = tmp_path / "vault"
    refs = export_question_packages(bundles, directory)
    boundary = EpisodeBoundary(episode_id=runtime.episode_id, input_sha256=runtime.input_sha256,
        before=refs[(runtime.episode_id, "before")], after=refs[(runtime.episode_id, "after")])
    return runtime, boundary, directory


class MemoryBackend:
    def __init__(self):
        self.sources = []

    async def assert_empty(self):
        if self.sources:
            raise ContractError("not_empty")

    async def ingest_session(self, source):
        self.sources.append(source.model_dump(mode="json"))
        return {k: getattr(source, k) for k in ("ordinal", "source_id", "source_version", "content_sha256")} | dict(
            status="unresolved", revision=len(self.sources), state_sha256=canonical_sha256(await self.snapshot()))

    async def snapshot(self):
        return dict(sources=copy.deepcopy(self.sources))


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after", [(0, 2), (1, 4), (3, 4)])
async def test_boundaries_complete_sessions_all_questions_and_no_future(tmp_path, before, after):
    runtime, boundary, directory = package(tmp_path, before, after)
    backend, seen = MemoryBackend(), []

    async def answer(question, state):
        seen.append((question, len(state["state"]["sources"])))
        state["state"]["sources"].clear()  # detached, cannot corrupt memory
        if question == "before-0":
            raise ValueError("failed slot remains")
        return "answer must never enter maintenance"

    runner = SessionEpisodeRunner(backend, answer)
    result = await runner.run(runtime, boundary, question_directory=directory)
    assert seen == [(f"before-{i}", before + 1) for i in range(2)] + [(f"after-{i}", after + 1) for i in range(3)]
    assert len(backend.sources) == after + 1
    assert all(len(s["messages"]) == 2 for s in backend.sources)
    assert all(s["source_id"] == f"src_{i:024x}" for i, s in enumerate(backend.sources))
    assert "SECRET" not in str(backend.sources)
    assert "question" not in str(backend.sources)
    assert result["answers"]["before"][0]["error"] == "answer_failed"
    assert result["judge_executed"] is False
    with pytest.raises(ContractError):
        await runner.run(runtime, boundary, question_directory=directory)


@pytest.mark.asyncio
async def test_question_file_stays_closed_until_boundary(tmp_path):
    runtime, boundary, directory = package(tmp_path)
    (directory / boundary.before.filename).unlink()
    backend = MemoryBackend()
    with pytest.raises(FileNotFoundError):
        await SessionEpisodeRunner(backend, None).run(runtime, boundary, question_directory=directory)
    assert len(backend.sources) == 2


@pytest.mark.asyncio
async def test_answer_mutation_detected(tmp_path):
    runtime, boundary, directory = package(tmp_path)
    backend = MemoryBackend()
    async def malicious_answer(question, state):
        backend.sources.clear()
        return "x"
    with pytest.raises(ContractError, match="answer_modified"):
        await SessionEpisodeRunner(backend, malicious_answer).run(runtime, boundary, question_directory=directory)


@pytest.mark.asyncio
async def test_undrained_session_cannot_release_questions(tmp_path):
    runtime, boundary, directory = package(tmp_path)
    class BadBackend(MemoryBackend):
        async def ingest_session(self, source):
            return (await super().ingest_session(source)) | {"status": "running"}
    with pytest.raises(ContractError, match="not_drained"):
        await SessionEpisodeRunner(BadBackend(), None).run(runtime, boundary, question_directory=directory)
