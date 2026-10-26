from .helpers import extraction_response
from .helpers import rule_item
from .helpers import bindings_response
from .helpers import link
"""Behavior tests for the fact/rule architecture, using real PostgreSQL."""

from .helpers import repair_target

import json
import pytest
import pytest_asyncio
import asyncpg
from contexthub.memory.contracts import DirectDecision, ref_of, split_ref
from contexthub.memory.storage import SCOPE
from .helpers import (
    add,
    row,
    session,
    input_of,
    condition_facts,
    direct_response,
    targets_response,
    trigger_response,
    repair_response,
    item,
    pending,
    direct,
    bind,
)


@pytest_asyncio.fixture(autouse=True)
async def scenario_candidates(env):
    # These tests check execution semantics. Small top-k recall is covered separately.
    env.retriever.config = env.retriever.config.model_copy(
        update={"text_k": 20, "vector_k": 20, "hybrid_limit": 20, "entity_k": 10}
    )


async def apply(env, context, text, action, target=None, **kwargs):
    kind = kwargs.get("type", "content")
    return await env.executor.apply_direct(
        pending(context, text, type=kind),
        DirectDecision(action=action, target_ref=target),
        context,
    )


@pytest.mark.asyncio
async def test_rule_duplicate_merges_session_sources_without_candidate_evidence(env):
    text = "If owner changes, then recipient follows owner"
    c1 = await session(env, 1)
    env.responses.append(dict(action="new", target_ref=None))
    original, _ = await direct(env, pending(c1, text, type="rule"), c1)
    embedded = len(env.embedded)
    c2 = await session(env, 2)
    env.responses.append(direct_response("duplicate", text))
    repeated, changes = await direct(env, pending(c2, text, type="rule"), c2)
    assert repeated == original and changes == []
    saved = await row(env, original)
    assert saved["namespace"] == env.trace.namespace
    assert saved["created_in_session"] == 1 and "rule_evidence" not in saved
    assert (await env.store.sources(env.trace, *split_ref(original)))[
        "session_seq"
    ] == [1, 2]
    assert len(await env.store.versions(env.trace, saved["memory_id"])) == 1
    assert len(env.embedded) == embedded + 1  # Query only; no new document embedding.
    assert not env.responses


@pytest.mark.asyncio
async def test_new_update_duplicate_history_and_source_merge(env):
    c1 = await session(env, 1)
    old, changes = await apply(env, c1, "Owner Wang", "new")
    assert changes[0]["operation"] == "new"
    c2 = await session(env, 2)
    same, changes = await apply(env, c2, "Owner Wang", "duplicate", old)
    assert same == old and changes == []
    assert (await env.store.sources(env.trace, *split_ref(old)))["session_seq"] == [
        1,
        2,
    ]
    fresh, changes = await apply(env, c2, "Owner Li", "update", old)
    assert (
        split_ref(fresh)[0] == split_ref(old)[0]
        and (await row(env, old))["status"] == "stale"
    )
    history, changes = await apply(env, c2, "Owner Zhao last year", "history", fresh)
    assert not changes and (await row(env, history))["temporal_scope"] == "historical"
    current = await env.retriever.current(env.trace, 2, purpose="answer")
    assert [r["text"] for r in current] == ["Owner Li"]
    historical = await env.retriever.current(env.trace, 2, purpose="history")
    assert {r["text"] for r in historical} == {
        "Owner Wang",
        "Owner Li",
        "Owner Zhao last year",
    }


@pytest.mark.asyncio
async def test_correction_of_previous_current_version_does_not_replace_today(env):
    c1 = await session(env, 1)
    old, _ = await apply(env, c1, "Owner Wang", "new")
    c2 = await session(env, 2)
    now, _ = await apply(env, c2, "Owner Li", "update", old)
    corrected, _ = await apply(
        env, c2, "Actually the former owner was Zhao", "correct", old
    )
    assert (await row(env, corrected))["temporal_scope"] == "historical"
    assert (await row(env, old))["invalidated"]
    assert [
        ref_of(r) for r in await env.retriever.current(env.trace, 2, purpose="answer")
    ] == [now]


@pytest.mark.asyncio
async def test_exact_basis_correction_excludes_multilevel_history(env):
    c = await session(env, 1)
    a = await add(env, c, "Owner Wang")
    r = await add(env, c, "If owner changes, then recipient follows owner", type="rule")
    b = await add(env, c, "Recipient Wang", origin="derived", basis=[a, r])
    d = await add(env, c, "Contact Wang", origin="derived", basis=[b, r])
    unrelated = await add(env, c, "Independent note", origin="derived", basis=[r])
    c2 = await session(env, 2)
    await apply(env, c2, "Owner actually Li", "correct", a)
    assert all([(await row(env, k))["invalidated"] for k in [a, b, d]])
    assert not (await row(env, unrelated))["invalidated"]
    assert {
        ref_of(x) for x in await env.retriever.current(env.trace, 2, purpose="history")
    }.isdisjoint({a, b, d})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forget", "retract", "invalidate", "revoke"])
async def test_temporary_instruction_and_provenance(env, kind):
    c = await session(env, 1)
    target = await add(
        env,
        c,
        (
            "If owner changes, then recipient follows owner"
            if kind == "revoke"
            else "Owner Wang"
        ),
        type="rule" if kind == "revoke" else "fact",
    )
    derived = await add(env, c, "Recipient Wang", origin="derived", basis=[target])
    c2 = await session(env, 2)
    env.responses.append(direct_response(kind, (await row(env, target))["text"]))
    if kind == "forget":
        env.responses.append(targets_response([(await row(env, target))["text"]]))
    changes = (await direct(env, pending(c2, "Fixture request"), c2))[1]
    assert len(changes) == 2
    t, d = await row(env, target), await row(env, derived)
    assert t["status"] == ("deleted" if kind == "forget" else "stale")
    assert d["status"] == "stale" and d["invalidated"] == (kind != "invalidate")
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                f"SELECT count(*) FROM memories WHERE {SCOPE}", *env.trace.args
            )
            == 2
        )
        assert await conn.fetchval("SELECT to_regclass('public.dependencies')") is None
        assert await conn.fetchval("SELECT to_regclass('public.change_events')") is None


@pytest.mark.asyncio
async def test_forget_all_versions_and_never_repair_forgotten_matter(env):
    c = await session(env, 1)
    a, _ = await apply(env, c, "Partner James", "new")
    c2 = await session(env, 2)
    b, _ = await apply(env, c2, "Partner Alex", "update", a)
    env.responses.append(direct_response("forget", "Partner Alex"))
    env.responses.append(targets_response(["Partner Alex"]))
    (await direct(env, pending(c2, "Forget my partner"), c2))[1]
    assert all(
        r["status"] == "deleted"
        for r in await env.store.versions(env.trace, split_ref(a)[0])
    )
    assert await env.executor.repair(await row(env, b), [], c2, set()) is None
    assert not await env.retriever.current(env.trace, 2, purpose="history")


@pytest.mark.asyncio
async def test_no_instruction_match_is_logged_without_mutation(env):
    c = await session(env, 1)
    a = await add(env, c, "Owner Wang")
    env.responses.append(dict(action="hold", target_ref=None))
    assert (await direct(env, pending(c, "Forget unknown partner"), c))[1] == []
    assert (await row(env, a))["status"] == "fresh"


@pytest.mark.asyncio
async def test_binder_binds_both_ends_and_survives_fact_versioning(env):
    c = await session(env, 1)
    health = await add(env, c, "Health lactose intolerance")
    exercise = await add(env, c, "Exercise swimming")
    rule = await add(env, c, "If health changes, then exercise becomes cycling", type="rule")
    env.responses.append(bindings_response(["Health lactose intolerance"], ["Exercise swimming"]))
    await bind(env, {rule}, c)
    assert (await row(env, rule))["affected_matter_ids"] == [split_ref(exercise)[0]]
    assert (await row(env, rule))["condition_matter_ids"] == [split_ref(health)[0]]
    c2 = await session(env, 2)
    updated, _ = await apply(env, c2, "Exercise cycling", "update", exercise)
    assert split_ref(updated)[0] in (await row(env, rule))["affected_matter_ids"]
    history, _ = await apply(env, c2, "Exercise running in 2020", "history", updated)
    assert (await row(env, history))["affected_matter_ids"] == []


@pytest.mark.asyncio
async def test_rule_before_fact_binds_when_fact_arrives(env):
    c = await session(env, 1)
    rule = await add(env, c, "If health changes, then exercise becomes cycling", type="rule")
    await bind(env, {rule}, c)
    assert not env.prompts
    c2 = await session(env, 2)
    exercise = await add(env, c2, "Exercise swimming")
    env.responses.append(bindings_response([], ["Exercise swimming"]))
    await bind(env, {exercise}, c2)
    assert (await row(env, rule))["affected_matter_ids"] == [split_ref(exercise)[0]]


@pytest.mark.asyncio
async def test_revoked_rule_does_not_trigger_or_inherit(env):
    c = await session(env, 1)
    rule = await add(env, c, "If owner changes, then recipient follows owner", type="rule")
    recipient = await add(env, c, "Recipient Wang")
    owner = await add(env, c, "Owner Wang")
    await link(env, rule, sources=[owner], targets=[recipient])
    c2 = await session(env, 2)
    env.responses.append(direct_response("revoke", (await row(env, rule))["text"]))
    (await direct(env, pending(c2, "Stop that rule"), c2))[1]
    _, changes = await apply(env, c2, "Owner Li", "update", owner)
    assert await env.executor.check_conditions(changes, set(), c2) == []
    updated, _ = await apply(env, c2, "Recipient fixed Zhao", "update", recipient)
    assert (await row(env, updated))["affected_matter_ids"] == []


@pytest.mark.asyncio
async def test_trigger_without_new_value_keeps_stale_and_does_not_fabricate(env):
    c = await session(env, 1)
    rule = await add(env, c, "If health changes, then exercise changes", type="rule")
    exercise = await add(env, c, "Exercise swimming")
    health = await add(env, c, "Health lactose intolerance")
    await link(env, rule, sources=[health], targets=[exercise])
    c2 = await session(env, 2)
    new, changes = await apply(env, c2, "Health high blood pressure", "update", health)
    env.responses.extend(
        [
            trigger_response(["Exercise swimming"], ["C1"]),
            dict(can_repair=False, text=None, entities=[], basis_refs=[], retrieval_query=None),
        ]
    )
    affected = await env.executor.check_conditions(changes, {new}, c2)
    assert affected and (await row(env, exercise))["status"] == "stale"
    attempted = set()
    assert (
        await env.executor.repair(await row(env, exercise), changes, c2, attempted)
        is None
    )
    assert (
        await env.executor.repair(await row(env, exercise), changes, c2, attempted)
        is None
    )
    assert not env.responses


@pytest.mark.asyncio
async def test_two_level_invalidation_and_repair_with_new_values(env):
    c = await session(env, 1)
    health = await add(env, c, "Health lactose intolerance")
    r1 = await add(env, c, "If health changes, then exercise becomes cycling", type="rule")
    r2 = await add(env, c, "If exercise changes venue becomes Velthari", type="rule")
    exercise = await add(env, c, "Exercise swimming")
    venue = await add(env, c, "Venue Crysthene")
    await link(env, r1, sources=[health], targets=[exercise])
    await link(env, r2, sources=[exercise], targets=[venue])
    unrelated = await add(env, c, "Unrelated stale hobby", status="stale")
    await add(env, c, "Unrelated fresh hobby")
    c2 = await session(env, 2)
    new, changes = await apply(env, c2, "Health high blood pressure", "update", health)

    def model(payload):
        data = input_of(payload)
        assert "Unrelated" not in str(data)
        if "checks" in data:
            texts = {t["before"]["text"] for t in data["changes"] if t["before"]}
            selected = []
            for t in condition_facts(data):
                if (
                    t["text"] == "Exercise swimming"
                    and "Health lactose intolerance" in texts
                ):
                    selected.append(t["ref"])
                if t["text"] == "Venue Crysthene" and "Exercise swimming" in texts:
                    selected.append(t["ref"])
            return dict(
                triggers=[
                    dict(fact_ref=ref, change_ids=[data["changes"][0]["id"]])
                    for ref in selected
                ]
            )
        assert "materials" in data
        if repair_target(payload) == "Exercise swimming":
            return repair_response(
                "Exercise cycling",
                [
                    "Health high blood pressure",
                    "If health changes, then exercise becomes cycling",
                ],
            )(payload)
        if any(m["text"] == "Exercise cycling" for m in data["materials"]):
            return repair_response(
                "Venue Velthari",
                ["Exercise cycling", "If exercise changes venue becomes Velthari"],
            )(payload)
        return dict(can_repair=False, text=None, entities=[], basis_refs=[], retrieval_query=None)

    env.responses.extend([model] * 12)
    await env.full_pipeline.maintain_conditions(changes, {new}, c2)
    current = await env.retriever.current(env.trace, 2, purpose="answer")
    assert {"Exercise cycling", "Venue Velthari"} <= {r["text"] for r in current}
    assert (await row(env, exercise))["status"] == "stale" and (await row(env, venue))[
        "status"
    ] == "stale"
    assert (await row(env, unrelated))["status"] == "stale"
    derived = [r for r in current if r["origin"] == "derived"]
    assert len(derived) == 2 and all(len(r["basis_refs"]) == 2 for r in derived)


@pytest.mark.asyncio
async def test_current_session_duplicate_confirmation_protected_from_condition(env):
    c = await session(env, 1)
    r = await add(env, c, "If owner changes, then recipient follows owner", type="rule")
    old = await add(env, c, "Recipient Wang")
    owner = await add(env, c, "Owner Wang")
    await link(env, r, sources=[owner], targets=[old])
    c2 = await session(env, 2)
    _, changes = await apply(env, c2, "Owner Li", "update", owner)
    now, _ = await apply(env, c2, "Recipient Wang", "duplicate", old)
    assert await env.executor.check_conditions(changes, {now}, c2) == []
    assert not env.prompts and (await row(env, now))["status"] == "fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["target", "unknown", "no_rule", "no_fact", "stale", "json", "empty"]
)
async def test_repair_rejects_invalid_or_incomplete_premises(env, bad):
    c = await session(env, 1)
    rule = await add(env, c, "If owner changes, then recipient follows owner", type="rule")
    await add(env, c, "Owner Li")
    await add(env, c, "Old evidence", status="stale")
    target = await add(env, c, "Recipient Wang", status="stale")

    await link(env, rule, targets=[target])
    def wrong(payload):
        data = input_of(payload)
        assert all(m["text"] != "Old evidence" for m in data["materials"])
        if bad in ("json", "empty"):
            return "{bad" if bad == "json" else ""
        refs = (
            [target]
            if bad == "target"
            else (
                ["M999"]
                if bad in ("unknown", "stale")
                else [
                    m["ref"]
                    for m in data["materials"]
                    if m["type"] == ("rule" if bad == "no_fact" else "fact")
                ]
            )
        )
        return dict(can_repair=True, text="Recipient Li", entities=[], basis_refs=refs, retrieval_query=None)

    env.responses.extend([wrong, wrong])
    assert await env.executor.repair(await row(env, target), [], c, set()) is None
    assert (await row(env, target))["status"] == "stale"
    assert (
        len(await env.store.versions(env.trace, (await row(env, target))["memory_id"]))
        == 1
    )
    assert len(env.prompts) == 2 and not env.responses
    # The persisted failure also prevents repeating the exact same attempt.
    assert await env.executor.repair(await row(env, target), [], c, set()) is None
    assert len(env.prompts) == 2
    async with env.store.pool.acquire() as conn:
        logs = await conn.fetch(
            f"SELECT stage,payload FROM memory_steps WHERE {SCOPE} AND stage IN ('p2_repair.validation_error','p2.repair_skipped') ORDER BY step_id",
            *env.trace.args,
        )
    assert [r["stage"] for r in logs] == ["p2_repair.validation_error"] * 2 + [
        "p2.repair_skipped"
    ]
    assert logs[-1]["payload"]["target"] == target
    assert logs[-1]["payload"]["attempts"] == 2
    assert logs[-1]["payload"]["action"] == "keep_stale"
    if bad == "empty":
        assert logs[0]["payload"]["raw"] == ""


@pytest.mark.asyncio
async def test_full_pipeline_keeps_only_fact_rule_and_processes_forget(env):
    env.responses.extend(
        [
            extraction_response(rules=[], contents=[item("Partner James")]),
            dict(action="new", target_ref=None),
        ]
    )
    refs = await env.full_pipeline.ingest(
        env.trace, 1, [dict(role="user", content="My partner is James")]
    )
    env.responses.extend(
        [
            extraction_response(rules=[], contents=[item("Forget my partner")]),
            direct_response("forget", "Partner James"),
            targets_response(["Partner James"]),
        ]
    )
    await env.full_pipeline.ingest(
        env.trace, 2, [dict(role="user", content="Forget my partner")]
    )
    assert (await env.store.versions(env.trace, refs[0][0]))[0]["status"] == "deleted"
    assert len(env.prompts) == 5 and not env.responses
    async with env.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                f"SELECT count(*) FROM memories WHERE {SCOPE}", *env.trace.args
            )
            == 1
        )


@pytest.mark.asyncio
async def test_schema_rejects_change_and_fact_endpoint_arrays(env):
    c = await session(env, 1)
    with pytest.raises(asyncpg.CheckViolationError):
        await add(env, c, "event", type="change")
    fact = await add(env, c, "Owner Wang")
    for field in ("condition_matter_ids", "affected_matter_ids"):
        with pytest.raises(asyncpg.CheckViolationError):
            async with env.store.pool.acquire() as conn:
                await conn.execute(f"UPDATE memories SET {field}=$1 WHERE tenant_id=$2 AND memory_id=$3",
                                   [split_ref(fact)[0]], env.trace.tenant_id, split_ref(fact)[0])


@pytest.mark.asyncio
async def test_full_pipeline_rule_extraction_binding_update_and_repair(env):
    def initial(payload):
        data = input_of(payload)
        if "messages" in data:
            return extraction_response(rules=[dict(rule_item("If owner changes, then recipient follows owner"), candidate_ref="R1")], contents=[item("Owner Wang"), item("Recipient Wang")])
        if "incoming" in data:
            return dict(action="new", target_ref=None)
        if "rule" in data:
            assert set(data) == {"rule", "candidates"}
            return bindings_response(["Owner Wang"], ["Recipient Wang"])(payload)
        if "checks" in data:
            return {"triggers": []}
        raise AssertionError(data)

    env.responses.extend([initial] * 6)
    await env.full_pipeline.ingest(
        env.trace,
        1,
        [
            dict(role="user", content="Owner Wang."),
            dict(role="user", content="Recipient Wang."),
            dict(role="user", content="If owner changes, then recipient follows owner."),
        ],
    )
    assert not env.responses
    current = await env.store.current_rows(env.trace)
    rule = next(r for r in current if r["type"] == "rule")
    recipient = next(r for r in current if r["text"] == "Recipient Wang")
    assert rule["affected_matter_ids"] == [recipient["memory_id"]]
    assert "rule_evidence" not in rule
    assert rule["namespace"] == env.trace.namespace
    assert rule["created_in_session"] == 1
    assert (await env.store.sources(env.trace, rule["memory_id"], rule["version"]))[
        "session_seq"
    ] == [1]

    env.responses.extend(
        [
            extraction_response(rules=[], contents=[item("Owner Li")]),
            direct_response("update", "Owner Wang"),
            trigger_response(["Recipient Wang"], ["C1"]),
            repair_response(
                "Recipient Li", ["Owner Li", "If owner changes, then recipient follows owner"]
            ),
        ]
    )
    await env.full_pipeline.ingest(
        env.trace, 2, [dict(role="user", content="Owner changed to Li.")]
    )
    answer_rows = await env.retriever.current(env.trace, 2, purpose="answer")
    assert {"Owner Li", "Recipient Li"} <= {r["text"] for r in answer_rows}
    assert not env.responses


@pytest.mark.asyncio
async def test_rule_update_rebinds_existing_targets_even_when_search_misses(
    env, monkeypatch
):
    c = await session(env, 1)
    old = await add(env, c, "If owner changes, then recipient follows owner", type="rule")
    fact = await add(env, c, "Recipient Wang")
    await link(env, old, targets=[fact])
    c2 = await session(env, 2)
    rule, _ = await apply(
        env,
        c2,
        "If owner changes, then recipient is now the deputy",
        "update",
        old,
        type="rule",
    )

    async def no_hits(*args, **kwargs):
        return {}

    monkeypatch.setattr(env.retriever, "candidates", no_hits)
    env.responses.append(bindings_response([], ["Recipient Wang"]))
    await bind(env, {rule}, c2)
    assert (await row(env, rule))["affected_matter_ids"] == [split_ref(fact)[0]]
    assert set(await env.store.active_rules(env.trace)) == {rule}


@pytest.mark.asyncio
async def test_forget_used_evidence_never_enters_repair_materials(env):
    c = await session(env, 1)
    secret = await add(env, c, "Secret owner Wang")
    rule = await add(env, c, "If owner changes, then recipient follows owner", type="rule")
    derived = await add(
        env,
        c,
        "Recipient Wang",
        origin="derived",
        basis=[secret, rule],
    )
    await link(env, rule, sources=[secret], targets=[derived])
    await add(env, c, "Independent current owner Li")
    c2 = await session(env, 2)
    env.responses.append(direct_response("forget", "Secret owner Wang"))
    env.responses.append(targets_response(["Secret owner Wang"]))
    changes = (await direct(env, pending(c2, "Forget secret owner"), c2))[1]

    def response(payload):
        data = input_of(payload)
        assert all(m["text"] != "Secret owner Wang" for m in data["materials"])
        assert "Secret owner Wang" not in json.dumps(data["changes"])
        assert (
            repair_target(payload)
            == "[obsolete value unavailable; use the attached rules to identify the matter]"
        )
        return dict(can_repair=False, text=None, entities=[], basis_refs=[], retrieval_query=None)

    env.responses.append(response)
    await env.executor.repair(await row(env, derived), changes, c2, set())
    assert not env.responses


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "invalidate"])
async def test_new_rule_is_bound_before_same_session_change_is_checked(env, action):
    c1 = await session(env, 1)
    health = await add(env, c1, "Health lactose intolerance")
    exercise = await add(env, c1, "Exercise swimming")
    await add(env, c1, "Unrelated current preference tea")
    rule_text = "If health changes, then exercise changes"
    incoming = (
        "Health high blood pressure"
        if action == "update"
        else "The old health condition is no longer current; the new condition is unknown."
    )

    def bind(payload):
        data = input_of(payload)
        assert data["rule"]["text"] == rule_text
        assert all(
            set(r) == {"ref", "text"} for r in data["candidates"]
        )
        return bindings_response([incoming if action == "update" else "Health lactose intolerance"], ["Exercise swimming"])(payload)

    def trigger(payload):
        data = input_of(payload)
        change = next(t for t in data["changes"] if t["operation"] == action)
        assert change["before"]["text"] == "Health lactose intolerance"
        if action == "invalidate":
            assert change["after"] is None
        else:
            assert change["after"]["text"] == incoming
        assert any(c["rule"]["text"] == rule_text for c in data["checks"])
        target = next(
            t for t in condition_facts(data) if t["text"] == "Exercise swimming"
        )
        return {"triggers": [dict(fact_ref=target["ref"], change_ids=[change["id"]])]}

    def repair(payload):
        data = input_of(payload)
        assert repair_target(payload) == "Exercise swimming"
        assert all(m["text"] != "Health lactose intolerance" for m in data["materials"])
        # A rule saying only "changes" supplies no replacement exercise value.
        return dict(can_repair=False, text=None, entities=[], basis_refs=[], retrieval_query=None)

    env.responses.extend(
        [
            extraction_response(rules=[dict(rule_item(rule_text), candidate_ref="R1")], contents=[item(incoming)]),
            dict(action="new", target_ref=None),
            direct_response(action, "Health lactose intolerance"),
            bind,
            trigger,
            repair,
        ]
    )
    refs = await env.full_pipeline.ingest(
        env.trace,
        2,
        [dict(role="user", content=rule_text), dict(role="user", content=incoming)],
    )
    assert not env.responses and len(refs) == (2 if action == "update" else 1)
    assert (await row(env, health))["status"] == "stale"
    saved_exercise = await row(env, exercise)
    assert saved_exercise["status"] == "stale"
    active = await env.store.active_rules(env.trace)
    assert all(saved_exercise["memory_id"] in r["affected_matter_ids"] for r in active.values())
    assert len(await env.store.versions(env.trace, split_ref(exercise)[0])) == 1
    async with env.store.pool.acquire() as conn:
        stages = await conn.fetch(
            f"SELECT stage FROM memory_steps WHERE {SCOPE} AND session_seq=2 ORDER BY step_id",
            *env.trace.args,
        )
    model_inputs = [r["stage"] for r in stages if r["stage"].endswith(".input")]
    assert model_inputs == [
        "extraction.joint.input",
        "p2_direct.input",
        "p2_direct.input",
        "p1.input",
        "p2_conditions.input",
        "p2_repair.input",
    ]
