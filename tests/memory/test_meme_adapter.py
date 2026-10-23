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
                "entity_values": (
                    {"meeting": "Monday 11am", "channel": "#fixture", "format": "async"}
                    if t == "Agg" else
                    {t: ["first", "second", "third"]} if t == "Tr" else
                    {t: "secret gold"}
                ),
            }
            for t in TASKS
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
        async def answer(self, question, context, *, timings):
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
    assert [x[1] for x in calls if x[0] == "answer"] == [1] * 6 + [2] * 6
    assert len(out["before_answers"]) == len(out["after_answers"]) == 6
    assert next(q for q in out["after_answers"] if q["task_type"] == "Del")["error"]["type"] == "RuntimeError"
    assert all(
        set(m) == {"role", "content"} for c in calls if c[0] == "ingest" for m in c[2]
    )


@pytest.mark.asyncio
async def test_official_six_tasks_pair_scoring_and_original_denominator(env):
    from contexthub.llm.chat_client import OpenAIChatClient
    import httpx

    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        prompt = body["messages"][0]["content"]
        content = (
            '{"results":[{"present":true},{"present":true},{"present":false}],"reason":"missing format"}'
            if 'after question Agg' in prompt or 'before question Agg' in prompt
            else '{"correct":true,"reason":"fixture"}'
        )
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o",
                "choices": [
                    {"message": {"content": content}}
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
        "repair_failures": 2,
        "direct_failures": 3,
        "timings": {"maintenance": 2.0, "episode": 3.0},
        "before_answers": [],
        "after_answers": [],
    }
    for phase in ("before", "after"):
        for i, q in enumerate(e[phase + "_questions"]["questions"]):
            result[phase + "_answers"].append(
                dict(q, question_id=f"{phase}:{i}", agent_answer="secret gold", error=None)
            )
    # Unanswered Del still counts, cannot get a lucky pass.
    result["after_answers"] = [r for r in result["after_answers"] if r["task_type"] != "Del"]
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
        first_request_count = len(requests)
        # Completed judgments, including a negative one, are reused. Failed
        # judgments are retried; a cache entry cannot make a missing answer pass.
        resumed = await scorer.score(
            result, e, env.trace.run_id,
            prior_verdicts={
                "before:0": {"u_pass": False, "u_reason": "saved negative verdict"},
                "after:0": {"u_pass": True, "u_reason": "saved positive verdict"},
                "after:1": {"u_pass": False, "score_error": "TimeoutError"},
                "after:2": {"u_pass": True, "u_reason": "must not override missing answer"},
            },
        )
        resumed_requests = requests[first_request_count:]
        requests = requests[:first_request_count]
    finally:
        await client.close()
    assert scored["trivial_analysis"]["Cas"]["real_pass"] == 1
    assert scored["trivial_analysis"]["Abs"]["real_pass"] == 1
    assert (
        scored["trivial_analysis"]["Del"]["total"] == 1
        and scored["trivial_analysis"]["Del"]["real_pass"] == 0
    )
    assert scored["timings"]["maintenance"] == 2.0
    assert scored["timings"]["scoring"] > 0
    assert scored["unjudged"] == 1 and len(requests) == 9
    assert scored["before_er_diagnostics"][0]["u_pass"] is True
    er = next(q for q in scored["after_answers"] if q["task_type"] == "ER")
    assert er["u_pass"] is True
    agg = next(q for q in scored["after_answers"] if q["task_type"] == "Agg")
    assert agg["u_pass"] is False
    assert agg["u_pass_per_entity"] == {"meeting": True, "channel": True, "format": False}
    assert agg["u_pass_count"] == 2 and agg["u_pass_total"] == 3
    agg_prompt = next(r["messages"][0]["content"] for r in requests if "after question Agg" in r["messages"][0]["content"])
    assert all(value in agg_prompt for value in ("Monday 11am", "#fixture", "async"))
    tracking_prompt = next(r["messages"][0]["content"] for r in requests if "after question Tr" in r["messages"][0]["content"])
    assert "first, second, third" in tracking_prompt
    assert not any("question ER" in r["messages"][0]["content"] for r in requests)
    assert scored["repair_failures"] == 2
    assert scored["direct_failures"] == 3
    assert all("max_tokens" not in r and r["temperature"] == 0 for r in requests)
    assert len(resumed_requests) == 7
    resumed_prompts = [r["messages"][0]["content"] for r in resumed_requests]
    assert not any("question Cas" in p or "after question Del" in p for p in resumed_prompts)
    assert any("after question Abs" in p for p in resumed_prompts)
    assert resumed["trivial_analysis"]["Cas"]["real_pass"] == 0
    assert resumed["before_answers"][0]["u_reason"] == "saved negative verdict"
    assert resumed["after_answers"][0]["u_reason"] == "saved positive verdict"
    assert resumed["after_answers"][1]["u_pass"] is True
    assert resumed["after_answers"][2]["u_pass"] is False
    assert resumed["unjudged"] == 1


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
        and report["request_seconds"] == 2
        and "wall_seconds" not in report
        and report["actual_cost"] is None
        and report["paper_price_cost"] is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "maintenance", "retrieval", "answer"])
async def test_elapsed_timings_include_processing_and_failed_work(
    monkeypatch, tmp_path, failure
):
    from integrations.memebench.memory_research import runner

    clock = [0.0]
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def record(*args):
        clock[0] += 1  # Database/logging work outside model requests.

    async def ingest(*args, **kwargs):
        clock[0] += 7  # Includes transport retries, waits and maintenance work.
        if failure == "maintenance":
            raise RuntimeError("maintenance failed")

    async def search(*args, **kwargs):
        clock[0] += 3
        if failure == "retrieval":
            raise RuntimeError("retrieval failed")
        return []

    async def complete(*args, **kwargs):
        clock[0] += 5
        if failure == "answer":
            raise RuntimeError("answer failed")
        return "fixture answer"

    store = SimpleNamespace(record=record)
    # The test exercises timing boundaries without sending requests or writing PG.
    monkeypatch.setattr(runner.CallContext, "make", lambda *args, **kwargs: None)
    answerer = runner.MemoryAnswerer(
        store, SimpleNamespace(search=search), SimpleNamespace(complete=complete), None
    )
    pipeline = SimpleNamespace(store=store, code_version="fixture", ingest=ingest)
    out = await runner.run_episode(
        episode(),
        run_id="timing-test",
        pipeline=pipeline,
        answerer=answerer,
        output=tmp_path / "timings.json",
    )
    timings = out["timings"]
    assert timings["maintenance"] == (7 if failure == "maintenance" else 21)
    assert timings["retrieval"] == (0 if failure == "maintenance" else 36)
    assert timings["answer"] == (
        0
        if failure in ("maintenance", "retrieval")
        else 72
        if failure == "answer"
        else 84
    )
    assert timings["inference"] == timings["retrieval"] + timings["answer"]
    assert timings["episode"] == clock[0]
    assert json.loads((tmp_path / "timings.json").read_text())["timings"] == timings
    if failure == "maintenance":
        assert out["status"] == "failed"
    elif failure:
        assert all(
            r["error"] for phase in ("before", "after") for r in out[phase + "_answers"]
        )
