"""Current-session extraction only. Usability parsing is not semantic verification."""

import json
import logging

from contexthub.llm.retry_policy import ResponseSchemaError

from .contracts import rule_queries
from .schemas import ExtractedItem, Extraction, normalize_topics
from .jev_candidate_gate import extraction_candidates

logger = logging.getLogger(__name__)


def parse_items(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("response is not a JSON object with readable items") from None
    if not isinstance(data, dict) or set(data) != {"items"}:
        raise ValueError("only items is allowed")
    if not isinstance(data["items"], list):
        raise ValueError("items array required")
    items = []
    for item in data["items"]:
        if not isinstance(item, dict):
            raise ValueError("each item must be an object")
        item_kind = item.get("type")
        if item_kind not in ("content", "rule", "forget_request"):
            raise ValueError("invalid extraction type")
        fields = {"type", "text", "entities", "topics", "source_message_nos"}
        if item_kind == "content":
            fields.add("temporal_scope")
        if set(item) != fields:
            raise ValueError(
                "item requires type, text, entities, topics and source_message_nos, plus temporal_scope for content"
            )
        if (
            not isinstance(item["text"], str)
            or not item["text"].strip()
            or not isinstance(item["entities"], list)
            or any(not isinstance(v, str) for v in item["entities"])
        ):
            raise ValueError("item needs nonempty text and string entities")
        source_nos = item["source_message_nos"]
        if (not isinstance(source_nos, list) or not source_nos
                or any(type(n) is not int or n < 1 for n in source_nos)
                or len(set(source_nos)) != len(source_nos)):
            raise ValueError("source_message_nos must contain unique positive message numbers")
        if item_kind == "rule":
            rule_queries(item["text"])
        elif item_kind == "content" and item["temporal_scope"] not in ("current", "historical"):
            raise ValueError("content temporal_scope must be current or historical")
        items.append(ExtractedItem(
            item["text"], item_kind, item["entities"], item.get("temporal_scope"),
            normalize_topics(item["topics"]), sorted(source_nos),
        ))
    return items, []


def parse_extraction(raw, *, rules_allowed, forget_allowed):
    """Validate one typed list without changing the model's source order."""
    items, warnings = parse_items(raw)
    if not rules_allowed and any(item.type == "rule" for item in items):
        raise ValueError("rule items forbidden when no rule candidates are supplied")
    if not forget_allowed and any(item.type == "forget_request" for item in items):
        raise ValueError("forget_request items forbidden when no forgetting candidates are supplied")
    return items, warnings


class ExtractionFailed(RuntimeError):
    pass


class Extractor:
    def __init__(self, client, *, retry, max_tokens=None, price=None, candidate_gate=None):
        self.client, self.retry, self.max_tokens, self.price = (
            client,
            retry,
            max_tokens,
            price,
        )
        self.candidate_gate = candidate_gate

    async def extract(self, messages, context):
        if self.candidate_gate is not None:
            selected = await self.candidate_gate.select(messages, context)
        else:
            selected = extraction_candidates(messages)
            await context.store.record(context, "extraction.candidates", selected)
        inputs = {
            "messages": messages,
            "accepted_candidates": {
                kind: [
                    {"ref": c["ref"], "message_no": c["message_no"],
                     "role": c["role"],
                     "text": c["text"]}
                    for c in selected[kind]
                ]
                for kind in ("rules", "forget_requests")
            },
        }
        items = await self.extract_items(inputs, context)
        # Preserve the model list order; candidates are never persisted as facts.
        raw = json.dumps({"items": [
            {k: v for k, v in i.__dict__.items() if k != "temporal_scope" or i.type == "content"}
            for i in items
        ]}, ensure_ascii=False)
        await context.store.save_output(context.trace, context.session_seq, raw)
        await context.store.record(context, "extraction.parsed", {
            "items": [i.__dict__ for i in items], "warnings": [],
        })
        return Extraction(items)

    async def extract_items(self, inputs, context):
        rule_instructions = (
            context.store.prompts.text("extractor_rules")
            if inputs["accepted_candidates"]["rules"]
            else "No rule candidates are supplied. Do not emit any rule items."
        )
        instructions = context.store.prompts.text("extractor").replace(
            "{rule_instructions}", rule_instructions
        )
        prompt = instructions + "\n" + json.dumps(inputs, ensure_ascii=False)
        log_stage = "extraction.joint"
        await context.store.record(context, log_stage + ".input", {"prompt": prompt})

        def parse(raw):
            items, warnings = parse_extraction(
                raw,
                rules_allowed=bool(inputs["accepted_candidates"]["rules"]),
                forget_allowed=bool(inputs["accepted_candidates"]["forget_requests"]),
            )
            message_nos = {m["message_no"] for m in inputs["messages"]}
            if any(set(item.source_message_nos) - message_nos for item in items):
                raise ValueError("source_message_nos must name messages in this session")
            return items, warnings

        error = None
        for attempt in range(1, 5):
            request = (
                prompt
                if error is None
                else prompt + "\nCorrect the previous output: " + error
            )
            call = context.make(
                "extraction_joint",
                {"prompt": request, "max_tokens": self.max_tokens},
                self.retry,
                extraction_attempt=attempt,
                parser=parse,
                price=self.price,
            )
            try:
                raw = await self.client.complete(
                    request, max_tokens=self.max_tokens, call=call
                )
            except ResponseSchemaError as exc:
                if str(exc) == "response_truncated_or_refused":
                    raise
                # A received empty text is a format failure, not a transport retry.
                raw = call.recorder.raw_output
                if not isinstance(raw, str):
                    raise
            else:
                raw = call.recorder.raw_output
            await context.store.save_output(context.trace, context.session_seq, raw)
            try:
                items, _ = parse(raw)
            except ValueError as exc:
                error = str(exc)
                await context.store.record(
                    context,
                    log_stage + ".validation_error",
                    {"attempt": attempt, "error": error, "raw_output": raw},
                )
                logger.warning(
                    "Extraction attempt %s/4 unusable: %s", attempt, exc
                )
                continue
            return items
        raise ExtractionFailed("four unusable joint extraction responses; stop this episode strategy")
