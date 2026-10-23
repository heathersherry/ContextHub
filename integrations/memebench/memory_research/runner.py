"""Sequential ingestion/answering and isolated official scoring. Importing does no work."""

import asyncio
import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from contexthub.memory.calls import CallContext
from contexthub.memory import contracts
from contexthub.memory.contracts import material, prompt
from contexthub.memory.retriever import answer_purpose
from contexthub.memory.schemas import Trace
from .official import load_official

TASKS = ("Cas", "Abs", "Del", "ER", "Agg", "Tr")


@contextmanager
def measure(timings, stage):
    start = time.monotonic()
    try:
        yield
    finally:
        timings[stage] = timings.get(stage, 0.0) + time.monotonic() - start


def selected_questions(episode, phase):
    return [
        dict(q, question_id=f"{phase}:{i}")
        for i, q in enumerate(episode[phase + "_questions"]["questions"])
        if q["task_type"].split(" (")[0] in TASKS
    ]


def answer_context(materials, purpose):
    if purpose in ("answer", "policy"):
        context = {
            "purpose": purpose,
            "memories": [
                {"text": m["text"], "type": "fact"}
                for m in materials if m["status"] == "fresh" and m["type"] == "fact"
            ],
        }
        if purpose == "policy":
            context["conditional_rules"] = [
                {"text": m["text"]}
                for m in materials if m["status"] == "fresh" and m["type"] == "rule"
            ]
        return context
    return {
        "purpose": purpose,
        "time_note": "Dates in statement text are fact-time evidence. created_in_session and timestamp only describe ingestion order, not effective dates.",
        "memories": [
            {k: v for k, v in m.items() if k not in ("ref", "basis_refs")}
            for m in materials
        ],
    }


class MemoryAnswerer:
    def __init__(self, store, retriever, client, retry, price=None):
        self.store, self.retriever, self.client, self.retry, self.price = (
            store,
            retriever,
            client,
            retry,
            price,
        )

    async def answer(self, question, context, *, timings):
        purpose = answer_purpose(question)
        with measure(timings, "retrieval"):
            hits = await self.retriever.search(
                question, context, through_session=context.session_seq, purpose=purpose,
                memory_type="fact" if purpose == "answer" else None,
            )
        with measure(timings, "answer"):
            materials = [material(h.memory) for h in hits]
            # Processing order is explicitly distinct from the time a fact held.
            template = prompt("answer_guidance")
            rendered = template.format(
                context=json.dumps(answer_context(materials, purpose), default=str, ensure_ascii=False),
                question=question,
            )
            manifest = json.loads(
                (
                    contracts.PROMPTS / "evaluation" / "official_manifest.json"
                ).read_text()
            )
            if (
                hashlib.sha256(template.encode()).hexdigest()
                != manifest["answer_sha256"]
            ):
                raise ValueError("official answer prompt changed")
            await self.store.record(
                context,
                "answer.input",
                {"question": question, "purpose": purpose, "prompt": rendered, "materials": materials},
            )
            text = await self.client.complete(
                rendered,
                max_tokens=500,
                call=context.make(
                    "answer",
                    {"prompt": rendered, "temperature": 0, "max_tokens": 500},
                    self.retry,
                    price=self.price,
                ),
            )
            await self.store.record(context, "answer.output", {"answer": text})
            return text


async def run_episode(episode, *, run_id, pipeline, answerer, output):
    started = time.monotonic()
    trace = Trace(
        run_id=run_id, episode_id=episode["episode_id"], strategy_id="local_base"
    )
    result = {
        "episode_id": episode["episode_id"],
        "status": "running",
        "sessions_completed": 0,
        "before_answers": [],
        "after_answers": [],
        "maintenance_error": None,
        "timings": {
            "maintenance": 0.0,
            "retrieval": 0.0,
            "answer": 0.0,
            "inference": 0.0,
        },
    }
    output = Path(output)

    def save():
        result["timings"]["episode"] = time.monotonic() - started
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
        )

    save()
    for seq, session in enumerate(episode["sessions"], 1):
        try:
            with measure(result["timings"], "maintenance"):
                # Deliberately discard task labels, graph metadata and session type/id.
                await pipeline.ingest(
                    trace,
                    seq,
                    [
                        {"role": m["role"], "content": m["content"]}
                        for m in session["conversation"]
                    ],
                    timestamp=session.get("timestamp"),
                    timeout_seconds=1800,
                )
        except Exception as exc:
            result.update(
                status="failed",
                maintenance_error={
                    "session_seq": seq,
                    "type": type(exc).__name__,
                    "detail": str(exc),
                },
            )
            save()
            return result
        result["sessions_completed"] = seq
        for phase in ("before", "after"):
            if seq != episode[phase + "_questions"]["position_after_session"] + 1:
                continue
            for q in selected_questions(episode, phase):
                # Only the question text reaches the answerer; labels/golds stay here.
                ctx = CallContext(
                    pipeline.store,
                    trace,
                    seq,
                    pipeline.code_version,
                    q["question_id"],
                    phase,
                )
                question_timings = {"retrieval": 0.0, "answer": 0.0}
                try:
                    with measure(question_timings, "inference"):
                        answer = await answerer.answer(
                            q["question"], ctx, timings=question_timings
                        )
                    row = dict(q, agent_answer=answer, error=None)
                except Exception as exc:
                    row = dict(
                        q,
                        agent_answer="",
                        error={"type": type(exc).__name__, "detail": str(exc)},
                    )
                    await pipeline.store.record(ctx, "answer.failed", row["error"])
                row["timings"] = question_timings
                for stage, seconds in question_timings.items():
                    result["timings"][stage] += seconds
                result[phase + "_answers"].append(row)
                save()
    result["status"] = "complete"
    save()
    return result


class OfficialScorer:
    def __init__(self, source, client, store, code_version, policy, price=None):
        self.cls, self.judge_episode = load_official(source)
        self.client, self.store, self.code_version, self.policy, self.price = (
            client,
            store,
            code_version,
            policy,
            price,
        )

    async def score(self, result, episode, run_id, *, prior_verdicts=None):
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        trace = Trace(
            run_id=run_id, episode_id=episode["episode_id"], strategy_id="local_base"
        )
        replay = {}
        judged = {}
        for phase in ("before", "after"):
            available = {r["question_id"]: r for r in result[phase + "_answers"]}
            rows = []
            for q in selected_questions(episode, phase):
                r = available.get(
                    q["question_id"],
                    dict(q, agent_answer="", error={"type": "not_answered"}),
                )
                ctx = CallContext(
                    self.store,
                    trace,
                    episode[phase + "_questions"]["position_after_session"] + 1,
                    self.code_version,
                    q["question_id"],
                    phase,
                )
                entity = next(iter(q["entity_values"]))
                cached = (prior_verdicts or {}).get(q["question_id"])
                if cached is not None and "score_error" not in cached and not r.get("error"):
                    verdict = cached
                elif r.get("error"):
                    verdict = {
                        "u_pass": False,
                        "u_reason": "answer failed or not reached",
                        "score_error": "not_answered",
                    }
                else:

                    async def request(kwargs, context=ctx):
                        await self.store.record(
                            context, "judge.input", {"request": kwargs}
                        )
                        # Official _call owns all three attempts, including format
                        # errors. The transport policy has exactly one attempt.
                        raw = await self.client.complete(
                            kwargs["messages"][0]["content"],
                            max_tokens=None,
                            call=context.make(
                                "judge",
                                {"request": kwargs},
                                self.policy,
                                price=self.price,
                            ),
                        )
                        await self.store.record(context, "judge.raw", {"raw": raw})
                        return SimpleNamespace(
                            choices=[
                                SimpleNamespace(message=SimpleNamespace(content=raw))
                            ],
                            usage=None,
                        )

                    def create(**kwargs):
                        return asyncio.run_coroutine_threadsafe(
                            request(kwargs), loop
                        ).result()

                    bridge = SimpleNamespace(
                        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
                    )
                    official = self.cls(bridge, model="gpt-4o", max_retries=3)
                    try:
                        if q["task_type"].split(" (")[0] == "Agg":
                            verdict = await asyncio.to_thread(
                                official.u_check_multi,
                                question=q["question"],
                                entity_values=q["entity_values"],
                                agent_answer=r["agent_answer"],
                                phase=phase,
                            )
                        else:
                            verdict = await asyncio.to_thread(
                                official.u_check,
                                question=q["question"],
                                entity=entity,
                                gold_value=q["entity_values"][entity],
                                agent_answer=r["agent_answer"],
                                task_type=q["task_type"],
                                phase=phase,
                            )
                    except Exception as exc:
                        verdict = {
                            "u_pass": False,
                            "u_reason": "judge failed",
                            "score_error": type(exc).__name__,
                        }
                await self.store.record(ctx, "judge.result", verdict)
                replay[(phase, q["question"], entity)] = verdict
                rows.append(r)
            judged[phase + "_answers"] = rows

        class Replay:
            def u_check(self, *, phase, question, entity, **kwargs):
                return replay[(phase, question, entity)]

            def u_check_multi(self, *, phase, question, entity_values, **kwargs):
                return replay[(phase, question, next(iter(entity_values)))]

        # Reuse official per-pair assembly and trivial-pass classification as-is.
        combined = await asyncio.to_thread(
            self.judge_episode, dict(result, **judged), Replay(), max_workers=1
        )
        # Official assembly skips before ER. Preserve that output and expose the
        # actual deterministic check separately; skipped is not an ER failure.
        combined["before_er_diagnostics"] = [
            dict(
                question_id=q["question_id"],
                **replay[("before", q["question"], next(iter(q["entity_values"])))],
            )
            for q in selected_questions(episode, "before")
            if q["task_type"].split(" (")[0] == "ER"
        ]
        combined["unjudged"] = sum("score_error" in v for v in replay.values())
        combined["maintenance_status"] = result["status"]
        combined["repair_failures"] = result["repair_failures"]
        combined["direct_failures"] = result["direct_failures"]
        combined["timings"] = dict(
            result["timings"], scoring=time.monotonic() - started
        )
        return combined
