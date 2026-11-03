"""Screen rule and forgetting candidates before full-session confirmation."""

import re

import pysbd

from contexthub.llm.jev_client import JEV_MODEL
from .code_regions import code_regions

# Frozen expanded vocabulary from the MemDerive rule-text audit. These are
# candidate hints, not semantic labels; JEV/the full-session extractor decides.
RULE_HINT_TERMS = (
    "if", "unless", "requires", "eligible", "except", "starting from",
    "require", "required", "requirement", "requirements", "need", "needs",
    "must", "only", "when", "once", "never", "cannot", "before", "after",
    "without", "while", "until", "within", "above", "below", "rule", "rules",
    "policy", "excluded", "excludes", "exclude", "exception", "exceptions",
    "effective", "at least", "at most", "no more than", "from today onward",
    "approved by", "handled by", "claimable", "refundable", "allowance",
    "cap", "caps", "count toward", "counts toward",
)
RULE_HINT = re.compile(
    r"\b(?:" + "|".join(r"\s+".join(map(re.escape, term.split())) for term in RULE_HINT_TERMS) + r")\b",
    re.I,
)
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
        # A soft line break must not separate a rule's condition from its outcome.
        # Equal-length substitutions preserve offsets; the payload uses original text.
        for span in segmenter.segment(source.replace("\n", " ").replace("\r", " ")):
            sentence = source[span.start:span.end].rstrip()
            for match in RULE_HINT.finditer(sentence):
                hint_start = span.start + match.start()
                hint_end = span.start + match.end()
                if any(start < hint_end and hint_start < end for start, end in excluded):
                    continue
                candidates.append({
                    "ref": f"R{len(candidates) + 1}",
                    "message_no": number,
                    "role": message["role"],
                    "text": sentence,
                    "sentence_start": span.start,
                    "hint_start": hint_start,
                    "hint_end": hint_end,
                })
                # Judge the whole sentence once, even if several hints match.
                break
    return candidates


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
                            "text": candidate["text"],
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
