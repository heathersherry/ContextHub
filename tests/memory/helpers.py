import json
from dataclasses import replace
from contexthub.memory.storage import SCOPE


def input_of(payload):
    return json.loads(payload["messages"][0]["content"].splitlines()[-1])


def repair_target(payload):
    """Read the target from the request's opening; it is not repeated in JSON."""
    text = payload["messages"][0]["content"]
    line = text.split("Determine how to update this stored statement:\n", 1)[1].splitlines()[0]
    return json.loads(line)


def item(text, *, temporal_scope="current", topics=()):
    return dict(text=text, entities=[], temporal_scope=temporal_scope, topics=list(topics))


def rule_item(text):
    return dict(text=text, entities=[], topics=[])


def pending(context, text, *, type="content", topics=()):
    return dict(
        text=text,
        type=type,
        entities=[],
        topics=list(topics),
        temporal_scope="current" if type == "content" else None,
        created_in_session=context.session_seq,
    )


async def session(env, seq, messages=None):
    await env.store.save_session(
        env.trace,
        seq,
        None,
        messages
        or [
            {
                "role": "user",
                "content": "Fixture direct source. If owner changes, recipient follows owner.",
            }
        ],
    )
    await env.store.save_output(
        env.trace,
        seq,
        json.dumps(dict(items=[])),
    )
    return replace(env.context, session_seq=seq)


async def add(
    env,
    context,
    text,
    *,
    type="fact",
    accepted=True,
    status="fresh",
    basis=(),
    scope="current",
    origin="extracted",
    topics=(),
):
    if origin == "extracted":
        ref = await env.store.add_memory(
            env.trace,
            context.session_seq,
            text=text,
            type=type,
            entities=[],
            topics=list(topics),
            origin="extracted",
            sources=[context.session_seq],
        )
    else:
        ref = await env.store.add_memory(
            env.trace,
            context.session_seq,
            text=text,
            type=type,
            entities=[],
            topics=list(topics),
            origin="derived",
        )
    await env.indexer.index([ref], context)
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET status=$6,accepted=$7,temporal_scope=$8,basis_refs=$9 WHERE {SCOPE} AND memory_id=$4 AND version=$5",
            *env.trace.args,
            *ref,
            status,
            accepted,
            scope if accepted else None,
            list(basis),
        )
    return f"{ref[0]}:{ref[1]}"


async def row(env, ref):
    return dict(await env.store.read_ref(env.trace, ref))


def direct_response(action, text=None):
    def response(payload):
        data = input_of(payload)
        target = next((r["ref"] for r in data["candidates"] if r["text"] == text), None)
        assert target is not None or text is None
        return dict(action=action, target_ref=target)

    return response


def condition_facts(data):
    return list(
        {r["ref"]: r for check in data["checks"] for r in check["facts"]}.values()
    )


def targets_response(texts):
    def response(payload):
        data = input_of(payload)
        rows = data["candidates"]
        selected = [r["ref"] for r in rows if r["text"] in texts]
        assert len(selected) == len(texts)
        return {"target_refs": selected}

    return response


def trigger_response(texts, change_ids):
    def response(payload):
        data = input_of(payload)
        selected = [r["ref"] for r in condition_facts(data) if r["text"] in texts]
        assert len(selected) == len(texts)
        return {
            "triggers": [dict(fact_ref=ref, change_ids=change_ids) for ref in selected]
        }

    return response


def repair_response(text, premise_texts):
    def response(payload):
        data = input_of(payload)
        refs = [r["ref"] for r in data["materials"] if r["text"] in premise_texts]
        assert len(refs) == len(premise_texts)
        return dict(can_repair=True, text=text, entities=[], basis_refs=refs, retrieval_query=None)

    return response


async def direct(env, focus, context):
    candidates = await env.retriever.candidates(focus, context)
    return await env.executor.direct(focus, candidates, context)


async def bind(env, changed_refs, context):
    candidates = {
        key: await env.retriever.candidates(await row(env, key), context)
        for key in changed_refs
    }
    return await env.discovery.bind(candidates, context)


async def link(env, rule, *, sources=(), targets=()):
    """Seed rule-owned endpoint sets; fact rows never store rule references."""
    from contexthub.memory.contracts import split_ref
    async with env.store.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET condition_matter_ids=$6,affected_matter_ids=$7 WHERE {SCOPE} AND memory_id=$4 AND version=$5",
            *env.trace.args, *split_ref(rule),
            sorted({split_ref(r)[0] for r in sources}), sorted({split_ref(r)[0] for r in targets}),
        )


def bindings_response(sources, targets):
    def response(payload):
        data = input_of(payload)
        result = {}
        for role, texts in (("condition_refs", sources), ("affected_refs", targets)):
            selected = [r["ref"] for r in data["candidates"] if r["text"] in texts]
            assert len(selected) == len(texts)
            result[role] = selected
        return result
    return response


def extraction_response(*, rules, contents, forget_requests=()):
    """Build the three-array model response with explicit cross-array ordering."""
    return {
        "rules": rules,
        "contents": [dict(item, order=item.get("order", i)) for i, item in enumerate(contents, 1)],
        "forget_requests": [dict(item, order=item.get("order", i))
                            for i, item in enumerate(forget_requests, len(contents) + 1)],
    }


def forget_item(text, *, entities=(), topics=(), order=1):
    return dict(text=text, entities=list(entities), topics=list(topics), order=order)
