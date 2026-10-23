from .helpers import rule_item
"""Current-session new/update facts are checked without extracted source message IDs."""

import json

import httpx
import pytest

from contexthub.llm.jev_client import JevClient
from contexthub.memory.jev_condition_binder import JevConditionBinder
from contexthub.memory.jev_intent_classifier import JevIntentClassifier
from contexthub.memory.jev_rule_gate import JevRuleGate
from integrations.memebench.memory_research.cli import decision_retry_policy
from .helpers import add, condition_facts, input_of, item, repair_response, session
from .test_jev import result


@pytest.mark.asyncio
@pytest.mark.parametrize("action,already_reflected", [
    ("new", False), ("update", False), ("duplicate", False),
    ("new", True), ("update", True),
])
async def test_same_session_writes_checked_without_source_numbers(env, action, already_reflected):
    env.retriever.config = env.retriever.config.model_copy(
        update={"hybrid_limit": 20, "text_k": 20, "vector_k": 20}
    )
    c = await session(env, 1)
    await add(env, c, "Health lactose intolerance")
    if action != "new":
        await add(env, c, "Exercise swimming")
    rule = "If health changes, exercise becomes cycling."
    exercise = "Exercise cycling after recovery" if already_reflected else "Exercise swimming before recovery"
    health = "Health recovered"
    exercise_no, health_no = (3, 1) if already_reflected else (1, 3)
    session_time = "2026-10-14T10:00:00+08:00"
    first_time = "2026-10-14T09:00:00+08:00"
    messages = [
        {"role": "user", "content": text}
        for text in ([health, rule, exercise] if already_reflected else [exercise, rule, health])
    ]
    messages[0]["timestamp"] = first_time

    def jev_response(request):
        payload = json.loads(request.content)
        answers = {}
        for ref, question in payload["questions"].items():
            if question["type"] == "choice":
                label = "new" if ref == "intent" else "rule"
                answers[ref] = dict(type="choice", choice=label, confidence=1.0,
                                    probabilities={k: float(k == label) for k in question["criteria"]})
            else:
                assert set(payload["state"]["rule"]) == {"text"}
                assert all(set(r) == {"ref", "text"} for r in payload["state"]["candidates"])
                role, candidate_ref = ref.split(":")
                candidate = next(r for r in payload["state"]["candidates"] if r["ref"] == candidate_ref)
                answers[ref] = dict(type="noul", noul=float(candidate["text"].startswith("Health" if role == "condition" else "Exercise")))
        return httpx.Response(200, json=result(answers))

    client = JevClient("fixture", transport=httpx.MockTransport(jev_response))
    retry = decision_retry_policy("jev")
    env.extractor = env.full_pipeline.extractor
    env.extractor.rule_gate = JevRuleGate(client, retry=retry)
    env.executor.intent_classifier = JevIntentClassifier(client, retry=retry)
    env.full_pipeline.binder = JevConditionBinder(env.store, client, env.retriever, retry=retry)
    contents = sorted([(exercise_no, exercise), (health_no, health)])
    env.responses.extend([
        {"items": [dict(rule_item(rule), candidate_ref="R1")]},
        {"items": [item(text) for _, text in contents]},
    ])
    checks = []

    def model(payload):
        data = input_of(payload)
        if "incoming" in data:
            incoming = data["incoming"]
            is_health = incoming["text"] == health
            operation = "update" if is_health else action
            old_text = "Health lactose intolerance" if is_health else "Exercise swimming"
            target = None if operation == "new" else next(
                r["ref"] for r in data["candidates"] if r["text"] == old_text and r["is_current"]
            )
            return {"operations": [dict(action=operation, target_ref=target,
                text=None if operation == "duplicate" else incoming["text"], entities=[])]}
        if "checks" in data:
            checks.append(data)
            facts = condition_facts(data)
            assert len(facts) == 1
            fact = facts[0]
            assert set(data["checks"][0]["rule"]) == {"text"}
            assert '"timestamp"' not in json.dumps(data)
            assert '"created_in_session"' not in json.dumps(data)
            assert "source_messages" not in data["checks"][0]["rule"]
            assert "source_messages" not in fact
            if len(checks) == 1:
                assert fact["text"] == exercise
                change = next(t for t in data["changes"] if t["operation"] == "update"
                              and t["after"]["text"] == health)
                assert change["after"]["text"] == health
                if not already_reflected:
                    return {"triggers": [dict(fact_ref=fact["ref"], change_ids=[change["id"]])]}
            assert fact["text"].startswith("Exercise cycling")
            return {"triggers": []}
        assert "materials" in data
        return repair_response("Exercise cycling", [rule, health])(payload)

    env.responses.extend([model] * (2 if action == "duplicate" else 3 if already_reflected else 4))
    try:
        await env.full_pipeline.ingest(env.trace, 2, messages, timestamp=session_time)
    finally:
        await client.close()
    assert not env.responses
    assert len(checks) == (0 if action == "duplicate" else 1)
    rows = await env.store.current_rows(env.trace)
    current_exercise = next(r for r in rows if r["text"].startswith("Exercise"))
    assert current_exercise["text"] == ("Exercise swimming" if action == "duplicate" else exercise if already_reflected else "Exercise cycling")
    assert current_exercise["status"] == "fresh"
    saved = await env.store.get_session(env.trace, 2)
    assert saved["messages"][0]["timestamp"] == first_time
    assert [m["message_no"] for m in saved["messages"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_extraction_accepts_items_without_source_numbers(env):
    env.responses.append({"items": [item("Exercise swimming")]})
    extracted = await env.pipeline.ingest(env.trace, 1, [
        {"role": "user", "content": "Exercise swimming"},
    ])
    assert extracted[0].__dict__ == {
        "text": "Exercise swimming", "entities": [], "type": "content", "temporal_scope": "current", "topics": [],
    }
    assert len(env.prompts) == 1
    assert not env.responses
