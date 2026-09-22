from __future__ import annotations

import pytest

from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.conversation_extraction_service import ExtractedFact
from contexthub.services.dependency_discovery_service import DependencyDiscoveryService
from integrations.memebench.chronological_ingest import ingest_case_chronological
from integrations.memebench.chronological_policy import (
    registered_build_plans,
    registered_schedules,
)
from integrations.memebench.ingest import ingest_case_raw
from integrations.memebench.loader import CascadeCase, Edge, Entity


class FakeChat(BaseChatClient):
    def __init__(self, reply: str = "NONE") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        return self.reply


class FakeExtractor:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def extract(self, conversation: str) -> list[ExtractedFact]:
        self.calls.append(conversation)
        for key, facts in self.mapping.items():
            if key in conversation:
                return [ExtractedFact(text) for text in facts]
        return []


class FakeDb:
    async def execute(self, sql: str, *args):
        return "OK"


class RecordingDiscovery(DependencyDiscoveryService):
    def __init__(self, chat) -> None:
        super().__init__(chat)
        self.seen_candidates: list[list[str]] = []
        self.seen_new_facts: list[str] = []

    async def discover_sources(self, new_fact, candidates):
        self.seen_new_facts.append(new_fact)
        self.seen_candidates.append([item.text for item in candidates])
        return await super().discover_sources(new_fact, candidates)


class AutoClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.001
        return self.t


async def _embed(texts):
    return [[float((len(text) % 5) + 1), 1.0] for text in texts]


def _case() -> CascadeCase:
    return CascadeCase(
        episode_id="ep-chrono",
        domain="test",
        hop=1,
        root="team_lead",
        root_change={"before": "Alice", "after": "Bob"},
        cascade_source="team_lead",
        target_entity="report_owner",
        gold_answer="Dana",
        question="who owns the report?",
        before_question=None,
        after_question=None,
        edges=[
            Edge(source="team_lead", target="report_owner", hop=1, pattern=""),
        ],
        entities={
            "team_lead": Entity(name="team_lead", before="Alice", after="Bob"),
            "report_owner": Entity(name="report_owner", before="Carol", after="Dana"),
        },
        sessions=[
            {
                "type": "evidence",
                "session_id": "s0",
                "conversation": [{"role": "user", "content": "session-zero Alice is team lead"}],
            },
            {
                "type": "evidence",
                "session_id": "s1",
                "conversation": [{"role": "user", "content": "session-one two facts"}],
            },
            {
                "type": "evidence",
                "session_id": "s2",
                "conversation": [{"role": "user", "content": "session-two future"}],
            },
            {
                "type": "evidence",
                "session_id": "evidence_change+delete_event",
                "conversation": [{"role": "user", "content": "lead changed to Bob"}],
            },
        ],
    )


def _extractor() -> FakeExtractor:
    return FakeExtractor(
        {
            "session-zero": ["The team lead is Alice."],
            "session-one": [
                "The report owner is Carol, assigned by the team lead.",
                "The deputy report owner is Evan.",
            ],
            "session-two": ["The standup time is 9am because the lead prefers mornings."],
        }
    )


async def _run(schedule_name: str, *, clock=None, hook=None, chat_reply: str = "NONE"):
    chat = FakeChat(chat_reply)
    discovery = RecordingDiscovery(chat)
    result = await ingest_case_chronological(
        FakeDb(),
        _case(),
        "acct",
        _embed,
        extractor=_extractor(),
        disamb_cheap=discovery,
        disamb_strong=discovery,
        edge_cheap=discovery,
        edge_strong=discovery,
        plan=registered_build_plans()["T_current_tau"],
        schedule=registered_schedules()[schedule_name],
        clock=clock or AutoClock(),
        consolidation_hook=hook,
    )
    return result, discovery


async def _run_with_audit(enabled: bool):
    chat = FakeChat("1")
    extractor = _extractor()
    discovery = RecordingDiscovery(chat)
    result = await ingest_case_chronological(
        FakeDb(),
        _case(),
        "acct",
        _embed,
        extractor=extractor,
        disamb_cheap=discovery,
        disamb_strong=discovery,
        edge_cheap=discovery,
        edge_strong=discovery,
        plan=registered_build_plans()["R_full_cheap"],
        schedule=registered_schedules()["async-each-session"],
        audit_trace=enabled,
        clock=AutoClock(),
    )
    return result, len(extractor.calls), len(chat.prompts)


@pytest.mark.asyncio
async def test_sessions_replay_in_original_order() -> None:
    result, _ = await _run("async-each-session")
    texts = [text for _, text in result.graph.inserted_nodes]
    assert texts[0] == "The team lead is Alice."
    assert "report owner is Carol" in texts[1]
    assert texts[-1].startswith("The standup time")
    assert [item["session_index"] for item in result.pending_audit] == [0, 1, 1, 2]


@pytest.mark.asyncio
async def test_same_session_facts_are_not_candidates_for_each_other() -> None:
    result, _ = await _run("backfill", chat_reply="1")
    session_one = [item for item in result.pending_audit if item["session_index"] == 1]
    assert len(session_one) == 2
    assert session_one[0]["snapshot_ids"] == session_one[1]["snapshot_ids"]
    sibling_ids = {item["node_id"] for item in session_one}
    for item in session_one:
        assert sibling_ids.isdisjoint(item["snapshot_ids"])
        assert item["snapshot_size"] == 1


@pytest.mark.asyncio
async def test_microbatch_cannot_see_future_sessions() -> None:
    result, discovery = await _run("async-microbatch-k5", chat_reply="1")
    text_by_id = {str(nid): text for nid, text in result.graph.inserted_nodes}
    future = "The standup time is 9am because the lead prefers mornings."
    for item in result.pending_audit:
        snapshot_texts = [text_by_id[nid] for nid in item["snapshot_ids"]]
        assert future not in snapshot_texts
        if item["session_index"] == 1:
            assert any("Alice" in text for text in snapshot_texts)
        if item["session_index"] == 2:
            assert any("report owner is Carol" in text for text in snapshot_texts)
    for candidates in discovery.seen_candidates:
        if any("report owner is Carol" in text for text in candidates):
            assert future not in candidates


@pytest.mark.asyncio
async def test_all_four_schedules_drain_pending() -> None:
    for name in registered_schedules():
        result, _ = await _run(name)
        assert result.timings["pending_remaining"] == 0
        assert result.timings["n_consolidation_jobs"] >= 1
        assert len(result.node_audit) == 4


@pytest.mark.asyncio
async def test_async_fast_path_excludes_consolidation() -> None:
    clock = AutoClock()

    def hook(_ready) -> None:
        clock.t += 1.0

    async_result, _ = await _run("async-each-session", clock=clock, hook=hook)
    assert async_result.timings["consolidation_seconds"] >= 1.0
    assert (
        async_result.timings["fast_path_seconds"]
        < async_result.timings["consolidation_seconds"]
    )

    sync_clock = AutoClock()

    def sync_hook(_ready) -> None:
        sync_clock.t += 1.0

    discovery = RecordingDiscovery(FakeChat("NONE"))
    sync_result = await ingest_case_chronological(
        FakeDb(),
        _case(),
        "acct",
        _embed,
        extractor=_extractor(),
        disamb_cheap=discovery,
        disamb_strong=discovery,
        edge_cheap=discovery,
        edge_strong=discovery,
        plan=registered_build_plans()["T_current_tau"],
        schedule=registered_schedules()["sync-inline"],
        clock=sync_clock,
        consolidation_hook=sync_hook,
    )
    assert sync_result.timings["fast_path_seconds"] >= 1.0
    assert (
        sync_result.timings["fast_path_seconds"]
        >= sync_result.timings["consolidation_seconds"]
    )


@pytest.mark.asyncio
async def test_old_ingest_path_still_allows_same_session_edges() -> None:
    extractor = _extractor()
    discovery = RecordingDiscovery(FakeChat("1"))
    graph = await ingest_case_raw(
        FakeDb(), _case(), "acct", _embed, extractor, discovery
    )
    first = "The report owner is Carol, assigned by the team lead."
    second = "The deputy report owner is Evan."
    assert any(first in candidates and second not in candidates for candidates in discovery.seen_candidates)
    assert any(first in candidates and second in "".join(candidates) for candidates in discovery.seen_candidates)
    assert graph.inserted_nodes
    assert extractor.calls


@pytest.mark.asyncio
async def test_audit_trace_is_opt_in_and_does_not_add_model_calls() -> None:
    off, off_extract, off_chat = await _run_with_audit(False)
    on, on_extract, on_chat = await _run_with_audit(True)

    assert off.audit_trace is None
    assert on.audit_trace is not None
    assert off_extract == on_extract
    assert off_chat == on_chat
    assert len(on.audit_trace["nodes"]) == len(on.graph.inserted_nodes)
    assert len(on.audit_trace["consolidations"]) == len(on.graph.inserted_nodes)

    for node in on.audit_trace["nodes"]:
        assert set(node) == {
            "node_id",
            "text",
            "session_index",
            "embedding_present",
            "original_session_id",
            "original_turns",
            "source_span",
        }
        assert node["original_session_id"]
        assert node["original_turns"]
        assert node["source_span"]["method"] in {"exact", "token_overlap"}
    for trace in on.audit_trace["consolidations"]:
        assert set(trace["routed_candidate_ids"]) <= set(
            trace["hmax_candidate_ids"]
        )
        assert set(trace["hmax_candidate_ids"]) <= set(
            trace["candidate_snapshot_ids"]
        )
        assert trace["final_selected_source_ids"] == trace["persisted_source_ids"]


@pytest.mark.asyncio
async def test_audit_gold_identity_never_enters_prompt_or_router_input() -> None:
    case = _case()
    source = case.entities.pop("team_lead")
    target = case.entities.pop("report_owner")
    source.name = "GOLD_SOURCE_SENTINEL"
    target.name = "GOLD_TARGET_SENTINEL"
    case.entities[source.name] = source
    case.entities[target.name] = target
    case.edges[0].source = source.name
    case.edges[0].target = target.name
    case.root = source.name
    case.target_entity = target.name

    chat = FakeChat("NONE")
    discovery = RecordingDiscovery(chat)
    await ingest_case_chronological(
        FakeDb(),
        case,
        "acct",
        _embed,
        extractor=_extractor(),
        disamb_cheap=discovery,
        disamb_strong=discovery,
        edge_cheap=discovery,
        edge_strong=discovery,
        plan=registered_build_plans()["R_full_cheap"],
        schedule=registered_schedules()["async-each-session"],
        audit_trace=True,
        clock=AutoClock(),
    )

    model_inputs = "\n".join(
        chat.prompts
        + discovery.seen_new_facts
        + [text for group in discovery.seen_candidates for text in group]
    )
    assert "GOLD_SOURCE_SENTINEL" not in model_inputs
    assert "GOLD_TARGET_SENTINEL" not in model_inputs
    assert (
        "ep-chrono|1|GOLD_SOURCE_SENTINEL|GOLD_TARGET_SENTINEL"
        not in model_inputs
    )
