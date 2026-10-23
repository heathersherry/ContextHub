import pytest
from contexthub.memory.model_io import LocalReferences
from contexthub.memory.contracts import DirectDecision
from .helpers import add, row, session, input_of, direct_response, pending, direct


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["json", "empty", "unknown_ref", "extra_field", "self"]
)
async def test_direct_correction_preserves_input_ids_and_never_exposes_focus_as_target(
    env, fault
):
    c1 = await session(env, 1)
    await add(env, c1, "Owner Wang")
    c2 = await session(env, 2)
    focus = pending(c2, "Owner Li")
    seen = []

    def bad(payload):
        data = input_of(payload)
        seen.append(data)
        assert data["incoming"]["text"] == "Owner Li" and "ref" not in data["incoming"]
        assert all(r["text"] != "Owner Li" for r in data["candidates"])
        if fault == "json":
            return "{bad"
        if fault == "empty":
            return ""
        if fault in ("unknown_ref", "self"):
            return dict(action="update", target_ref="M999")
        return dict(action="new", target_ref=None, content_ref=None)

    def good(payload):
        data = input_of(payload)
        assert data == seen[0]
        return direct_response("update", "Owner Wang")(payload)

    env.responses.extend([bad, good])
    result, changes = await direct(env, focus, c2)
    assert (await row(env, result))["text"] == "Owner Li" and len(changes) == 1
    assert len(env.prompts) == 2 and not env.responses
    async with env.store.pool.acquire() as conn:
        outputs = await conn.fetch(
            "SELECT raw_output FROM execution_attempts WHERE run_id=$1 AND episode_id=$2 AND phase='finished' AND extraction_attempt IS NULL",
            *env.trace.args[:2],
        )
    assert len(outputs) >= 2


@pytest.mark.asyncio
async def test_invalid_direct_output_twice_skips_without_mutation(env):
    c = await session(env, 1)
    focus = pending(c, "Owner Wang")
    env.responses.extend([dict(action="duplicate", target_ref="M1")] * 2)
    assert await direct(env, focus, c) == (None, [])
    assert await env.store.current_rows(env.trace) == []


@pytest.mark.asyncio
async def test_local_refs_only_expose_supplied_records_and_keep_stored_provenance(env):
    c = await session(env, 1)
    a = await add(env, c, "Owner Wang")
    b = await add(env, c, "Recipient Wang", origin="derived", basis=[a])
    rows = [await row(env, b)]
    refs = LocalReferences(rows)
    assert refs.decode(refs.encode(b)) == b
    assert set(refs.describe(rows[0])) == {"ref", "text", "created_in_session", "timestamp"}
    assert rows[0]["basis_refs"] == [a]
    assert a not in refs.decode_map.values()
    with pytest.raises(ValueError):
        refs.decode("M1:1")


@pytest.mark.parametrize(
    "value",
    [
        dict(action="new", target_ref="M1"),
        dict(action="hold", target_ref="M1"),
        dict(action="update", target_ref=None),
        dict(action="replace", target_ref="M1"),
        dict(action="new", target_ref=None, temporal_scope="current"),
    ],
)
def test_minimal_direct_contract_rejects_obsolete_or_inconsistent_fields(value):
    with pytest.raises(ValueError):
        DirectDecision.model_validate(value)
