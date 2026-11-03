"""Route explicit verbatim requests, then batch remaining content intents."""

import re

from contexthub.llm.jev_client import JEV_MODEL


VERBATIM_MEMORY_REQUEST = re.compile(
    r"\A\s*(?:please\s+)?remember\s+(?:this|these|the\s+following)\b"
    r"[^:\n?!]*\b(?:word\s+for\s+word|verbatim)\s*:\s*\S",
    re.IGNORECASE,
)


class JevIntentClassifier:
    def __init__(self, client, *, retry, price=None):
        self.client, self.retry, self.price = client, retry, price

    async def classify(self, contents, context):
        if not contents:
            return []
        intents = {
            f"C{index}": "inform"
            for index, incoming in enumerate(contents, 1)
            if VERBATIM_MEMORY_REQUEST.match(incoming["text"])
        }
        if intents:
            await context.store.record(context, "p2_intent.program", {
                "rule": "explicit_verbatim_memory_request", "intents": dict(intents),
            })
        if len(intents) == len(contents):
            return ["inform"] * len(contents)
        rubric = context.store.prompts.json("p2_intent_jev")
        payload = {
            "model": JEV_MODEL,
            "state": {"intent_policy": {"instructions": rubric["instructions"], "criteria": rubric["criteria"]}},
            "questions": {
                f"C{index}": {
                    "type": "choice",
                    "instructions": {"question": "Apply state.intent_policy to incoming.text.",
                                     "incoming": {"text": incoming["text"]}},
                    "criteria": {key: f"state.intent_policy.criteria.{key}" for key in rubric["criteria"]},
                }
                for index, incoming in enumerate(contents, 1)
                if f"C{index}" not in intents
            },
        }
        await context.store.record(context, "p2_intent.input", {"request": payload})
        call = context.make("p2_intent", {"request": payload}, self.retry, price=self.price)
        answers = await self.client.evaluate(payload, call=call)
        intents.update({key: answers[key]["choice"] for key in payload["questions"]})
        await context.store.record(context, "p2_intent.output", {
            "call_id": str(call.identity.call_id), "answers": answers,
        })
        return [intents[f"C{index}"] for index in range(1, len(contents) + 1)]
