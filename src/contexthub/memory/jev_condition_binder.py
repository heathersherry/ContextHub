"""Optional Jev judgments; candidate selection and condition writes remain shared."""

import math

from contexthub.llm.jev_client import JEV_MODEL
from .condition_binder import ConditionBinder
from .contracts import prompt


class JevConditionBinder(ConditionBinder):
    def __init__(self, store, client, retriever, *, retry, threshold=0.5, price=None):
        super().__init__(store, client, retriever, retry=retry, price=price)
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Jev threshold must be between 0 and 1")
        self.threshold = threshold

    async def select_bindings(self, inputs, refs, key, context):
        instructions = prompt("p1_jev")
        payload = {
            "model": JEV_MODEL,
            "state": {"binding_policy": instructions, **inputs},
            "questions": {
                f"{role}:{candidate['ref']}": {
                    "type": "noul",
                    "instructions": "Apply state.binding_policy to state.rule and state.candidates. Role: "
                    + role
                    + ". Evaluate candidate "
                    + candidate["ref"]
                    + ".",
                }
                for role in ("condition", "affected")
                for candidate in inputs["candidates"]
            },
        }
        await self.store.record(
            context,
            "p1.input",
            {
                "backend": "jev",
                "request": payload,
                "rule": key,
                "reference_map": refs.decode_map,
                "threshold": self.threshold,
            },
        )
        call = context.make(
            "condition_binding",
            {"request": payload, "threshold": self.threshold},
            self.retry,
            price=self.price,
        )
        answers = await self.client.evaluate(payload, call=call)
        probabilities = {ref: answer["noul"] for ref, answer in answers.items()}
        await self.store.record(
            context,
            "p1.jev_decisions",
            {
                "rule": key,
                "call_id": str(call.identity.call_id),
                "probabilities": probabilities,
                "threshold": self.threshold,
                "reference_map": refs.decode_map,
            },
        )
        return {
            role + "_refs": [
                refs.decode(question.split(":", 1)[1])
                for question, p in probabilities.items()
                if question.startswith(role + ":") and p >= self.threshold
            ]
            for role in ("condition", "affected")
        }
