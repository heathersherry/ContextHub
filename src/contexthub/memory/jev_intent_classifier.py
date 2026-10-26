"""One candidate-free Jev Choice question about an incoming content's purpose."""


from contexthub.llm.jev_client import JEV_MODEL


class JevIntentClassifier:
    def __init__(self, client, *, retry, price=None):
        self.client, self.retry, self.price = client, retry, price

    async def classify(self, incoming, context):
        question = context.store.prompts.json("p2_intent_jev")
        payload = {
            "model": JEV_MODEL,
            "state": {"incoming": {"text": incoming["text"]}},
            "questions": {"intent": question},
        }
        await context.store.record(context, "p2_intent.input", {"request": payload})
        call = context.make("p2_intent", {"request": payload}, self.retry, price=self.price)
        answers = await self.client.evaluate(payload, call=call)
        await context.store.record(context, "p2_intent.output", {
            "call_id": str(call.identity.call_id), "answer": answers["intent"],
        })
        return answers["intent"]["choice"]
