import json
from types import SimpleNamespace
import pytest
from integrations.memebench.memory_research.runner import (
    run_episode,
    OfficialScorer,
    TASKS,
)
from integrations.memebench.memory_research.cli import totals


def episode():
    def questions(phase):
        return [
            {
                "task_type": t,
                "question": f"{phase} question {t}",
                "entity_values": {t: "secret gold"},
            }
            for t in (*TASKS, "Tr")
        ]

    return {
        "episode_id": "synthetic",
        "sessions": [
            {
                "session_id": "hidden",
                "type": "evidence",
                "conversation": [{"role": "user", "content": f"message {i}"}],
                "timestamp": f"T{i}",
            }
            for i in range(3)
        ],
        "before_questions": {
            "position_after_session": 0,
            "questions": questions("before"),
        },
        "after_questions": {
            "position_after_session": 1,
            "questions": questions("after"),
        },
    }


@pytest.mark.asyncio
async def test_runner_boundaries_all_sessions_and_evaluation_isolation(env, tmp_path):
    calls = []

    async def ingest(trace, seq, messages, **kwargs):
        calls.append(("ingest", seq, messages, kwargs))
        assert "secret gold" not in json.dumps(messages)

    class Answerer:
        async def answer(self, question, context):
            calls.append(("answer", context.session_seq, question))
            if question == "after question Del":
                raise RuntimeError("fixture answer failure")
            return "fixture answer"

    pipeline = SimpleNamespace(store=env.store, code_version="fixture", ingest=ingest)
    out = await run_episode(
        episode(),
        run_id=env.trace.run_id,
        pipeline=pipeline,
        answerer=Answerer(),
        output=tmp_path / "out.json",
    )
    assert out["sessions_completed"] == 3
    assert [x[1] for x in calls if x[0] == "answer"] == [1] * 3 + [2] * 3
    assert len(out["before_answers"]) == len(out["after_answers"]) == 3
    assert out["after_answers"][-1]["error"]["type"] == "RuntimeError"
    assert all(
        set(m) == {"role", "content"} for c in calls if c[0] == "ingest" for m in c[2]
    )


@pytest.mark.asyncio
async def test_official_three_tasks_pair_scoring_and_original_denominator(env):
    from contexthub.llm.chat_client import OpenAIChatClient
    import httpx

    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o",
                "choices": [
                    {"message": {"content": '{"correct":true,"reason":"fixture"}'}}
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    client = OpenAIChatClient(
        "fixture",
        base_url="https://fixture.invalid/v1",
        model="gpt-4o",
        temperature=0,
        transport=httpx.MockTransport(handler),
    )
    e = episode()
    result = {
        "episode_id": e["episode_id"],
        "status": "failed",
        "before_answers": [],
        "after_answers": [],
    }
    for phase in ("before", "after"):
        for i, q in enumerate(e[phase + "_questions"]["questions"][:3]):
            result[phase + "_answers"].append(
                dict(q, question_id=f"{phase}:{i}", agent_answer="value", error=None)
            )
    result[
        "after_answers"
    ].pop()  # unanswered Del still counts, cannot get a lucky pass
    scorer = OfficialScorer(
        "/Users/sherrylin/Documents/PythonProjects/public/MEME-public/code/eval/judge.py",
        client,
        env.store,
        "fixture",
        env.executor.retry.model_copy(
            update={"max_attempts": 1, "backoff_seconds": ()}
        ),
    )
    try:
        scored = await scorer.score(result, e, env.trace.run_id)
    finally:
        await client.close()
    assert scored["trivial_analysis"]["Cas"]["real_pass"] == 1
    assert scored["trivial_analysis"]["Abs"]["real_pass"] == 1
    assert (
        scored["trivial_analysis"]["Del"]["total"] == 1
        and scored["trivial_analysis"]["Del"]["real_pass"] == 0
    )
    assert scored["unjudged"] == 1 and len(requests) == 5
    assert all("max_tokens" not in r and r["temperature"] == 0 for r in requests)


def test_unknown_cost_stays_unknown_and_requests_not_events():
    r = {
        "identity": {"stage": "repair"},
        "requested_model": "fixture",
        "usage": {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "completeness": "complete",
        },
        "cost": {"amount": None, "currency": None},
        "wall_seconds": 1,
    }
    report = totals([r, r], {})["maintenance"]
    assert (
        report["requests"] == 2
        and report["actual_cost"] is None
        and report["paper_price_cost"] is None
    )
