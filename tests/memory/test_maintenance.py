import json
from dataclasses import replace
from uuid import uuid4
import pytest
from contexthub.memory.contracts import Binding, Discovery, ref_of
from contexthub.memory.schemas import ExtractedItem
from contexthub.memory.retriever import answer_purpose


async def session(env, seq):
    await env.store.save_session(
        env.trace,
        seq,
        f"T{seq}",
        [{"role": "user", "content": "Explicit test evidence"}],
    )
    await env.store.save_output(env.trace, seq, '{"items":[]}')
    return replace(env.context, session_seq=seq)


async def add(env, ctx, text, type="fact", *, accepted=False, scope="current"):
    (ref,) = await env.store.save_extracted(
        env.trace, ctx.session_seq, [ExtractedItem(text, type, ["Atlas"], [1])]
    )
    await env.indexer.index([ref], ctx)
    key = f"{ref[0]}:{ref[1]}"
    if accepted:
        async with env.store.pool.acquire() as conn:
            await env.executor.mutate(
                key,
                {"status": "fresh", "accepted": True, "temporal_scope": scope},
                ctx,
                conn,
            )
    return key


def binding(source, target=None, relation="new", scope="current"):
    return Binding(
        content_ref=source,
        target_ref=target,
        relation=relation,
        temporal_scope=scope,
        evidence_refs=[r for r in (source, target) if r],
        issues=[],
        reason="Explicit fixture relationship",
    )


def discovery(focus, b, *, edges=(), judgments=()):
    return {
        "focus": focus,
        "result": Discovery(
            bindings=[b], dependencies=[], existing_edges=list(judgments)
        ),
        "pairs": {},
        "existing": {},
        "rows": {},
    }


async def row(env, key):
    return dict(await env.store.read_ref(env.trace, key))


@pytest.mark.asyncio
async def test_replacement_history_duplicate_and_same_change_once(env):
    c1 = await session(env, 1)
    old = await add(env, c1, "Atlas owner Wang", accepted=True)
    c2 = await session(env, 2)
    new = await add(env, c2, "Atlas owner Li")
    change = await add(env, c2, "Atlas owner changed to Li", "change")
    b = binding(new, old, "replace")
    affected, accepted = await env.executor.apply_discoveries(
        [discovery(new, b), discovery(change, b)], c2
    )
    assert len(accepted) == 1 and not affected
    current = next(iter(accepted))
    assert (await row(env, old))["status"] == "stale"
    assert current.endswith(":2") and (await row(env, new))["merged_into"] == {
        "ref": current
    }
    assert (
        await env.store.get_change(env.trace, (await row(env, change))["memory_id"])
    )["applied_session_seq"] == 2
    c3 = await session(env, 3)
    past = await add(env, c3, "Last year Atlas owner Zhao")
    _, accepted = await env.executor.apply_discoveries(
        [discovery(past, binding(past, current, "historical", "historical"))], c3
    )
    assert next(iter(accepted)).endswith(":3")
    assert (
        ref_of((await env.retriever.current(env.trace, 3, purpose="answer"))[0])
        == current
    )
    history = await env.retriever.current(env.trace, 3, purpose="history")
    assert len(history) == 3
    c4 = await session(env, 4)
    repeat = await add(env, c4, "Atlas owner Li")
    await env.executor.apply_discoveries(
        [discovery(repeat, binding(repeat, current, "duplicate"))], c4
    )
    assert (
        len(await env.store.versions(env.trace, (await row(env, old))["memory_id"]))
        == 3
    )
    source = await env.store.sources(
        env.trace, (await row(env, current))["memory_id"], 2
    )
    assert source["message_refs"] == {"2": [1], "4": [1]}


@pytest.mark.asyncio
async def test_propagation_repair_and_forgotten_basis(env):
    c1 = await session(env, 1)
    owner = await add(env, c1, "Atlas owner Wang", accepted=True)
    rule = await add(
        env, c1, "Report always follows Atlas owner", "rule", accepted=True
    )
    report = await add(env, c1, "Atlas report recipient Wang", accepted=True)
    for upstream in (owner, rule):
        a, b = await row(env, upstream), await row(env, report)
        await env.store.put_dependency(
            env.trace,
            (a["memory_id"], 1),
            (b["memory_id"], 1),
            "confirmed",
            "recipient follows owner",
        )
    c2 = await session(env, 2)
    new = await add(env, c2, "Atlas owner Li")
    affected, accepted = await env.executor.apply_discoveries(
        [discovery(new, binding(new, owner, "replace"))], c2
    )
    assert affected == {report} and (await row(env, report))["status"] == "stale"
    owner2 = next(iter(accepted))
    env.responses.append(
        {
            "target_ref": report,
            "can_repair": True,
            "text": "Atlas report recipient Li",
            "entities": ["Atlas", "Li"],
            "basis_refs": [owner2, rule],
            "reason": "Current owner and explicit rule",
        }
    )
    repaired, _ = await env.executor.repair(report, c2, set())
    assert (await row(env, repaired))["origin"] == "derived"
    assert (await row(env, repaired))["basis_refs"] == sorted([owner2, rule])
    assert repaired in {
        ref_of(r) for r in await env.retriever.current(env.trace, 2, purpose="history")
    }
    c3 = await session(env, 3)
    forget = await add(env, c3, "Forget Atlas owner", "change")
    await env.executor.apply_discoveries(
        [discovery(forget, binding(None, owner2, "forget", None))], c3
    )
    assert (await row(env, repaired))["invalidated"]
    for purpose in ("answer", "history", "p2"):
        assert repaired not in {
            ref_of(r)
            for r in await env.retriever.current(env.trace, 3, purpose=purpose)
        }
    assert all(
        r["status"] == "deleted"
        for r in await env.store.versions(
            env.trace, (await row(env, owner))["memory_id"]
        )
    )


@pytest.mark.asyncio
async def test_release_prevents_propagation_and_independent_partial_change(env):
    from contexthub.memory.contracts import EdgeJudgment

    c1 = await session(env, 1)
    owner = await add(env, c1, "Atlas owner Wang", accepted=True)
    report = await add(env, c1, "Report fixed to Zhao", accepted=True)
    a, b = await row(env, owner), await row(env, report)
    await env.store.put_dependency(
        env.trace,
        (a["memory_id"], 1),
        (b["memory_id"], 1),
        "confirmed",
        "old relationship",
    )
    c2 = await session(env, 2)
    new = await add(env, c2, "Atlas owner Li")
    change = await add(env, c2, "Atlas and Beacon owners changed", "change")
    d = discovery(
        change,
        binding(new, owner, "replace"),
        judgments=[
            EdgeJudgment(pair_id=owner + ">" + report, yes=False, reason="report fixed")
        ],
    )
    d["result"].bindings.append(
        Binding(
            content_ref=None,
            target_ref=None,
            relation="unresolved",
            temporal_scope=None,
            evidence_refs=[change],
            issues=["Beacon new owner not stated"],
            reason="missing value",
        )
    )
    affected, _ = await env.executor.apply_discoveries([d], c2)
    assert not affected and (await row(env, report))["status"] == "fresh"
    assert (
        await env.store.get_change(env.trace, (await row(env, change))["memory_id"])
    )["applied_session_seq"] is None
    count = len(await env.store.versions(env.trace, a["memory_id"]))
    await env.executor.apply_discoveries([d], c2)
    assert len(await env.store.versions(env.trace, a["memory_id"])) == count


@pytest.mark.asyncio
async def test_invalid_reference_rolls_back_batch(env):
    c1 = await session(env, 1)
    old = await add(env, c1, "Atlas owner Wang", accepted=True)
    c2 = await session(env, 2)
    new = await add(env, c2, "Atlas owner Li")
    second = await add(env, c2, "Unknown replacement")
    with pytest.raises(ValueError, match="missing memory"):
        await env.executor.apply_discoveries(
            [
                discovery(new, binding(new, old, "replace")),
                discovery(second, binding(second, f"{uuid4()}:1", "replace")),
            ],
            c2,
        )
    assert (await row(env, old))["status"] == "fresh"
    assert (await row(env, new))["merged_into"] is None


@pytest.mark.asyncio
async def test_no_repair_guess_and_same_evidence_not_recalled(env):
    c1 = await session(env, 1)
    target = await add(env, c1, "Meeting Wednesday", accepted=True)
    await env.store.set_status(
        env.trace,
        (await row(env, target))["memory_id"],
        1,
        "stale",
        "requires reconfirmation",
    )
    env.responses.append(
        {
            "target_ref": target,
            "can_repair": False,
            "text": None,
            "entities": [],
            "basis_refs": [],
            "reason": "No new schedule",
        }
    )
    assert await env.executor.repair(target, c1, set()) == (None, set())
    assert await env.executor.repair(target, c1, set()) == (None, set())
    assert len(env.prompts) == 1
    assert (await row(env, target))["status"] == "stale"


def p1_new(payload):
    data = json.loads(payload["messages"][0]["content"].splitlines()[-1])
    focus = data["focus"]
    return {
        "bindings": [binding(focus).model_dump()],
        "dependencies": [
            {"pair_id": p, "yes": False, "reason": "Independent"} for p in data["pairs"]
        ],
        "existing_edges": [
            {"pair_id": p, "yes": True, "reason": "Still applies"}
            for p in data["existing_edges"]
        ],
    }


@pytest.mark.asyncio
async def test_full_pipeline_accepts_direct_fact_and_rule_without_new_inferences(env):
    env.responses.extend(
        [
            {
                "items": [
                    {
                        "text": "Atlas owner Wang",
                        "type": "fact",
                        "entities": ["Atlas"],
                        "message_refs": [1],
                    },
                    {
                        "text": "Report follows Atlas owner",
                        "type": "rule",
                        "entities": ["Atlas"],
                        "message_refs": [1],
                    },
                ]
            },
            p1_new,
            p1_new,
        ]
    )
    refs = await env.full_pipeline.ingest(
        env.trace,
        1,
        [{"role": "user", "content": "Atlas owner Wang; report follows owner"}],
    )
    assert len(refs) == 2
    allowed = await env.retriever.current(env.trace, 1, purpose="answer")
    assert {r["type"] for r in allowed} == {"fact", "rule"}
    assert len(env.prompts) == 3
    assert "max_tokens" not in env.prompts[1]


@pytest.mark.asyncio
async def test_incomplete_p1_output_stops_trajectory(env):
    env.responses.extend(
        [
            {
                "items": [
                    {
                        "text": "Atlas owner Wang",
                        "type": "fact",
                        "entities": [],
                        "message_refs": [1],
                    }
                ]
            },
            {"bindings": [], "dependencies": [], "existing_edges": []},
        ]
    )
    with pytest.raises(ValueError):
        await env.full_pipeline.ingest(
            env.trace, 1, [{"role": "user", "content": "Atlas owner Wang"}]
        )
    with pytest.raises(ValueError, match="previous maintenance failed"):
        await env.full_pipeline.ingest(
            env.trace, 2, [{"role": "user", "content": "next"}]
        )
    assert await env.retriever.current(env.trace, 1, purpose="answer") == []


def test_history_intent_is_question_only():
    assert answer_purpose("Who owned Atlas over time?") == "history"
    assert answer_purpose("Who owns Atlas?") == "answer"


@pytest.mark.asyncio
async def test_bound_unresolved_version_blocks_old_fresh_fallback(env):
    c1 = await session(env, 1)
    old = await add(env, c1, "Atlas office floor 5", accepted=True)
    c2 = await session(env, 2)
    new = await add(env, c2, "Atlas office floor 3 or 4")
    b = Binding(
        content_ref=new,
        target_ref=old,
        relation="unresolved",
        temporal_scope="current",
        evidence_refs=[new, old],
        issues=["Conflicting floor values for the same office"],
        reason="Matter is bound but value is unresolved",
    )
    await env.executor.apply_discoveries([discovery(new, b)], c2)
    assert await env.retriever.current(env.trace, 2, purpose="answer") == []
    assert len(await env.retriever.current(env.trace, 2, purpose="history")) == 1


@pytest.mark.asyncio
async def test_correction_invalidates_actual_premises_not_all_historical_dependencies(
    env,
):
    c1 = await session(env, 1)
    wrong = await add(env, c1, "Atlas owner Wang", accepted=True)
    dependent = await add(env, c1, "Meeting Wednesday", accepted=True)
    independent = await add(env, c1, "Office floor 5", accepted=True)
    derived = await add(env, c1, "Office floor label five", accepted=True)
    async with env.store.pool.acquire() as conn:
        await env.executor.mutate(derived, {"basis_refs": [independent]}, c1, conn)
    a, b = await row(env, wrong), await row(env, independent)
    await env.store.put_dependency(
        env.trace,
        (a["memory_id"], 1),
        (b["memory_id"], 1),
        "confirmed",
        "recheck office after owner change",
    )
    c2 = await session(env, 2)
    correct = await add(env, c2, "Atlas owner Li")
    await env.executor.apply_discoveries(
        [discovery(correct, binding(correct, wrong, "correct"))], c2
    )
    history = {
        ref_of(r) for r in await env.retriever.current(env.trace, 2, purpose="history")
    }
    assert (
        wrong not in history
        and independent in history
        and derived in history
        and dependent in history
    )


@pytest.mark.asyncio
async def test_corrected_basis_excludes_transitive_derived_versions(env):
    c1 = await session(env, 1)
    original = await add(env, c1, "Original assertion", accepted=True)
    first = await add(env, c1, "First conclusion", accepted=True)
    second = await add(env, c1, "Second conclusion", accepted=True)
    async with env.store.pool.acquire() as conn:
        await env.executor.mutate(first, {"basis_refs": [original]}, c1, conn)
        await env.executor.mutate(second, {"basis_refs": [first]}, c1, conn)
    c2 = await session(env, 2)
    correct = await add(env, c2, "Corrected assertion")
    await env.executor.apply_discoveries(
        [discovery(correct, binding(correct, original, "correct"))], c2
    )
    for r in (original, first, second):
        assert (await row(env, r))["invalidated"]
    assert len(await env.retriever.current(env.trace, 2, purpose="history")) == 1


@pytest.mark.asyncio
async def test_new_rule_can_invalidate_existing_conclusion_via_newly_discovered_edge(
    env,
):
    from contexthub.memory.contracts import EdgeJudgment

    c1 = await session(env, 1)
    old = await add(env, c1, "Report recipient Wang", accepted=True)
    c2 = await session(env, 2)
    rule = await add(env, c2, "Report recipient must follow owner", "rule")
    d = discovery(rule, binding(rule))
    d["pairs"] = {rule + ">" + old: [rule, old]}
    d["result"].dependencies = [
        EdgeJudgment(
            pair_id=rule + ">" + old, yes=True, reason="new explicit constraint"
        )
    ]
    affected, _ = await env.executor.apply_discoveries([d], c2)
    assert old in affected and (await row(env, old))["status"] == "stale"


def owner_scenario(payload):
    data = json.loads(payload["messages"][0]["content"].splitlines()[-1])
    rows = {r["ref"]: r for r in data["memories"]}
    focus = rows[data["focus"]]
    if not data["relationships"]:
        b = Binding(
            content_ref=None,
            target_ref=None,
            relation="unresolved",
            temporal_scope=None,
            evidence_refs=[],
            issues=["already accepted; dependency refresh only"],
            reason="refresh",
        )
    elif "Li" in focus["text"]:
        source = next(
            r
            for r in rows.values()
            if r["text"] == "Atlas owner Li" and r["ref"] in data["session_refs"]
        )
        target = next(r for r in rows.values() if r["text"] == "Atlas owner Wang")
        b = binding(source["ref"], target["ref"], "replace")
    else:
        b = binding(focus["ref"])

    def yes(a, b):
        return (
            rows[a]["text"].startswith("Atlas owner") or rows[a]["type"] == "rule"
        ) and "report recipient" in rows[b]["text"]

    return {
        "bindings": [b.model_dump()],
        "dependencies": [
            {"pair_id": p, "yes": yes(*pair), "reason": "fixture directed relationship"}
            for p, pair in data["pairs"].items()
        ],
        "existing_edges": [
            {"pair_id": p, "yes": True, "reason": "relation remains applicable"}
            for p in data["existing_edges"]
        ],
    }


def repaired_report(payload):
    data = json.loads(payload["messages"][0]["content"].splitlines()[-1])
    refs = [
        r["ref"]
        for r in data["materials"]
        if r["text"] == "Atlas owner Li" or r["type"] == "rule"
    ]
    assert len(refs) == 2
    return {
        "target_ref": data["target"]["ref"],
        "can_repair": True,
        "text": "Atlas report recipient Li",
        "entities": ["Atlas", "Li"],
        "basis_refs": refs,
        "reason": "owner plus rule",
    }


@pytest.mark.asyncio
async def test_complete_update_repair_refresh_pipeline(env):
    def extracted(texts):
        return {
            "items": [
                {"text": text, "type": kind, "entities": ["Atlas"], "message_refs": [1]}
                for text, kind in texts
            ]
        }

    env.responses.extend(
        [
            extracted(
                [
                    ("Atlas owner Wang", "fact"),
                    ("Report follows Atlas owner", "rule"),
                    ("Atlas report recipient Wang", "fact"),
                ]
            ),
            owner_scenario,
            owner_scenario,
            owner_scenario,
            extracted(
                [("Atlas owner Li", "fact"), ("Atlas owner changed to Li", "change")]
            ),
            owner_scenario,
            owner_scenario,
            repaired_report,
            owner_scenario,
        ]
    )
    for seq in (1, 2):
        await env.full_pipeline.ingest(
            env.trace, seq, [{"role": "user", "content": "fixture explicit source"}]
        )
    current = await env.retriever.current(env.trace, 2, purpose="answer")
    assert {r["text"] for r in current} == {
        "Atlas owner Li",
        "Report follows Atlas owner",
        "Atlas report recipient Li",
    }
    assert not env.responses
    async with env.store.pool.acquire() as conn:
        active = await conn.fetch(
            "SELECT * FROM dependencies WHERE run_id=$1 AND episode_id=$2 AND maintenance_active",
            env.trace.run_id,
            env.trace.episode_id,
        )
    assert len(active) == 2 and all(r["downstream_version"] == 2 for r in active)


@pytest.mark.asyncio
async def test_replaying_completed_part_of_change_does_not_reinvalidate(env):
    c1 = await session(env, 1)
    old = await add(env, c1, "Atlas owner Wang", accepted=True)
    report = await add(env, c1, "Report recipient Wang", accepted=True)
    c2 = await session(env, 2)
    new = await add(env, c2, "Atlas owner Li")
    from contexthub.memory.contracts import EdgeJudgment

    d = discovery(new, binding(new, old, "replace"))
    d["pairs"] = {new + ">" + report: [new, report]}
    d["result"].dependencies = [
        EdgeJudgment(
            pair_id=new + ">" + report, yes=True, reason="recipient follows owner"
        )
    ]
    await env.executor.apply_discoveries([d], c2)
    await env.store.set_status(
        env.trace,
        (await row(env, report))["memory_id"],
        1,
        "fresh",
        "independently reconfirmed",
    )
    affected, _ = await env.executor.apply_discoveries([d], c2)
    assert not affected and (await row(env, report))["status"] == "fresh"
