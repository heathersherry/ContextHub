"""Batch rule and forgetting hints before full-session confirmation."""

import re

import pysbd

from contexthub.llm.jev_client import JEV_MODEL
from .code_regions import code_regions

RULE_HINT = re.compile(r"\bif\b", re.I)
FORGET_HINT = re.compile(r"\b(?:remove|forget|delete)\b", re.I)


def rule_candidates(messages):
    candidates = []
    segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    for number, message in enumerate(messages, 1):
        if message["role"] not in ("user", "assistant"):
            continue
        source = message["content"]
        if not RULE_HINT.search(source):
            continue
        excluded = code_regions(source)
        # A soft line break must not separate an if-clause from its consequence.
        # Equal-length substitutions preserve offsets; the payload uses original text.
        for span in segmenter.segment(source.replace("\n", " ").replace("\r", " ")):
            sentence = source[span.start:span.end].rstrip()
            for match in RULE_HINT.finditer(sentence):
                if_start = span.start + match.start()
                if any(start <= if_start < end for start, end in excluded):
                    continue
                candidates.append({
                    "ref": f"R{len(candidates) + 1}",
                    "message_no": number,
                    "role": message["role"],
                    "text": sentence,
                    "sentence_start": span.start,
                    "if_start": if_start,
                    "if_end": span.start + match.end(),
                })
    return candidates


def candidate_text(candidate):
    """Mark only ambiguous if occurrences; keep source offsets in audit records."""
    text = candidate["text"]
    if len(RULE_HINT.findall(text)) == 1:
        return text
    start = candidate["if_start"] - candidate["sentence_start"]
    end = candidate["if_end"] - candidate["sentence_start"]
    return text[:start] + "<target>" + text[start:end] + "</target>" + text[end:]


def forget_candidates(messages):
    """One sentence per hint; offsets stay in the audit trail only."""
    candidates = []
    segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    for number, message in enumerate(messages, 1):
        if message["role"] not in ("user", "assistant"):
            continue
        source = message["content"]
        if not FORGET_HINT.search(source):
            continue
        for span in segmenter.segment(source.replace("\n", " ").replace("\r", " ")):
            sentence = source[span.start:span.end].rstrip()
            if FORGET_HINT.search(sentence):
                candidates.append({
                    "ref": f"F{len(candidates) + 1}", "message_no": number,
                    "role": message["role"], "text": sentence, "sentence_start": span.start,
                })
    return candidates


def extraction_candidates(messages):
    return {"rules": rule_candidates(messages), "forget_requests": forget_candidates(messages)}


class JevCandidateGate:
    def __init__(self, client, *, retry, price=None):
        self.client, self.retry, self.price = client, retry, price

    async def select(self, messages, context):
        candidates = extraction_candidates(messages)
        await context.store.record(context, "extraction.candidates", candidates)
        if not any(candidates.values()):
            return candidates
        state, questions = {}, {}
        for kind, prompt, positive in (
            ("rules", "extractor_rule_jev", "rule"),
            ("forget_requests", "extractor_forget_jev", "forget"),
        ):
            if not candidates[kind]:
                continue
            rubric = context.store.prompts.json(prompt)
            policy = positive + "_policy"
            state[policy] = {"instructions": rubric["instructions"], "criteria": rubric["criteria"]}
            for candidate in candidates[kind]:
                questions[candidate["ref"]] = {
                    "type": "choice",
                    "instructions": {
                        "question": f"Apply state.{policy} to candidate.",
                        "candidate": {
                            "role": candidate["role"],
                            "text": candidate_text(candidate) if kind == "rules" else candidate["text"],
                        },
                    },
                    "criteria": {key: f"state.{policy}.criteria.{key}" for key in rubric["criteria"]},
                }
        payload = {"model": JEV_MODEL, "state": state, "questions": questions}
        await context.store.record(context, "extraction.candidate_gate.input", {"request": payload})
        call = context.make("extraction_candidate_gate", {"request": payload}, self.retry, price=self.price)
        answers = await self.client.evaluate(payload, call=call)
        selected = {
            kind: [c for c in items if answers[c["ref"]]["choice"] in ("rule", "forget", "unclear")]
            for kind, items in candidates.items()
        }
        await context.store.record(context, "extraction.candidate_gate.output", {
            "call_id": str(call.identity.call_id), "answers": answers,
            "selected_refs": {kind: [c["ref"] for c in items] for kind, items in selected.items()},
        })
        return selected
