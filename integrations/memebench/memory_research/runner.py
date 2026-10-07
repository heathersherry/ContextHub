"""Sequential ingestion/answering and isolated official scoring. Importing does no work."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from contexthub.memory.calls import CallContext
from contexthub.memory.contracts import material, prompt, PROMPTS
from contexthub.memory.retriever import answer_purpose
from contexthub.memory.schemas import Trace
from .official import load_official

TASKS = ("Cas", "Abs", "Del")


def selected_questions(episode, phase):
    return [
        dict(q, question_id=f"{phase}:{i}")
        for i, q in enumerate(episode[phase + "_questions"]["questions"])
        if q["task_type"].split(" (")[0] in TASKS
    ]


class MemoryAnswerer:
    def __init__(self, store, retriever, client, retry, price=None):
        self.store, self.retriever, self.client, self.retry, self.price = (
            store,
            retriever,
            client,
            retry,
            price,
        )

    async def answer(self, question, context):
        purpose = answer_purpose(question)
        hits = await self.retriever.search(
            question, context, through_session=context.session_seq, purpose=purpose
        )
        materials = [material(h.memory) for h in hits]
        # Processing order is explicitly distinct from the time a fact held.
        rendered = prompt("answer").format(
            context=json.dumps(
                {
                    "purpose": purpose,
                    "time_note": "Dates in statement text are fact-time evidence. created_in_session and timestamp only describe ingestion order, not effective dates.",
                    "memories": materials,
                },
                default=str,
                ensure_ascii=False,
            ),
            question=question,
        )
        manifest = json.loads((PROMPTS / "official_manifest.json").read_text())
        if (
            hashlib.sha256(prompt("answer").encode()).hexdigest()
            != manifest["answer_sha256"]
        ):
            raise ValueError("official answer prompt changed")
        await self.store.record(
            context,
            "answer.input",
            {"question": question, "prompt": rendered, "materials": materials},
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
    }
    output = Path(output)

    def save():
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
        )

    save()
    for seq, session in enumerate(episode["sessions"], 1):
        try:
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
                try:
                    answer = await answerer.answer(q["question"], ctx)
                    row = dict(q, agent_answer=answer, error=None)
                except Exception as exc:
                    row = dict(
                        q,
                        agent_answer="",
                        error={"type": type(exc).__name__, "detail": str(exc)},
                    )
                    await pipeline.store.record(ctx, "answer.failed", row["error"])
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

    async def score(self, result, episode, run_id):
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
                if r.get("error"):
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

        # Reuse official per-pair assembly and trivial-pass classification as-is.
        combined = await asyncio.to_thread(
            self.judge_episode, dict(result, **judged), Replay(), max_workers=1
        )
        combined["unjudged"] = sum("score_error" in v for v in replay.values())
        combined["maintenance_status"] = result["status"]
        return combined
