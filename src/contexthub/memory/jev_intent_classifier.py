"""One session-wide batch of candidate-free content intent questions."""

from contexthub.llm.jev_client import JEV_MODEL


class JevIntentClassifier:
    def __init__(self, client, *, retry, price=None):
        self.client, self.retry, self.price = client, retry, price

    async def classify(self, contents, context):
        if not contents:
            return []
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
            },
        }
        await context.store.record(context, "p2_intent.input", {"request": payload})
        call = context.make("p2_intent", {"request": payload}, self.retry, price=self.price)
        answers = await self.client.evaluate(payload, call=call)
        intents = [answers[key]["choice"] for key in payload["questions"]]
        await context.store.record(context, "p2_intent.output", {
            "call_id": str(call.identity.call_id), "answers": answers,
        })
        return intents
