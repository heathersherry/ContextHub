"""Screen sentence-local if occurrences before full-session LLM confirmation."""

import re

import pysbd

from contexthub.llm.jev_client import JEV_MODEL

RULE_HINT = re.compile(r"\bif\b", re.I)


def rule_candidates(messages):
    candidates = []
    segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    for number, message in enumerate(messages, 1):
        if message["role"] not in ("user", "assistant"):
            continue
        source = message["content"]
        if not RULE_HINT.search(source):
            continue
        # A soft line break must not separate an if-clause from its consequence.
        # Equal-length substitutions preserve offsets; the payload uses original text.
        for span in segmenter.segment(source.replace("\n", " ").replace("\r", " ")):
            sentence = source[span.start:span.end].rstrip()
            for match in RULE_HINT.finditer(sentence):
                candidates.append({
                    "ref": f"R{len(candidates) + 1}",
                    "message_no": number,
                    "role": message["role"],
                    "text": sentence,
                    "sentence_start": span.start,
                    "if_start": span.start + match.start(),
                    "if_end": span.start + match.end(),
                })
    return candidates


class JevRuleGate:
    def __init__(self, client, *, retry, price=None):
        self.client, self.retry, self.price = client, retry, price

    async def select(self, messages, context):
        candidates = rule_candidates(messages)
        await context.store.record(context, "extraction.rule_candidates", {
            "candidates": candidates,
        })
        if not candidates:
            return []
        rubric = context.store.prompts.json("extractor_rule_jev")
        payload = {
            "model": JEV_MODEL,
            "state": {"rule_policy": rubric["instructions"]},
            "questions": {
                candidate["ref"]: {
                    "type": "choice",
                    "instructions": {
                        "question": "Apply state.rule_policy to this if occurrence in candidate.text; offsets are relative to that sentence.",
                        "candidate": {
                            "role": candidate["role"],
                            "text": candidate["text"],
                            "if_start": candidate["if_start"] - candidate["sentence_start"],
                            "if_end": candidate["if_end"] - candidate["sentence_start"],
                        },
                    },
                    "criteria": rubric["criteria"],
                }
                for candidate in candidates
            },
        }
        await context.store.record(context, "extraction.rule_gate.input", {
            "request": payload,
        })
        call = context.make("extraction_rule_gate", {"request": payload},
                            self.retry, price=self.price)
        answers = await self.client.evaluate(payload, call=call)
        selected = [c for c in candidates if answers[c["ref"]]["choice"] in ("rule", "unclear")]
        await context.store.record(context, "extraction.rule_gate.output", {
            "call_id": str(call.identity.call_id), "answers": answers,
            "selected_refs": [c["ref"] for c in selected],
        })
        return selected
