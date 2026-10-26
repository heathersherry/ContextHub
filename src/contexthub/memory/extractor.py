"""Current-session extraction only. Usability parsing is not semantic verification."""

import json
import logging

from contexthub.llm.retry_policy import ResponseSchemaError

from .contracts import rule_queries
from .schemas import ExtractedItem, Extraction, normalize_topics
from .jev_candidate_gate import candidate_text, extraction_candidates

logger = logging.getLogger(__name__)


def parse_items(raw, *, kind="content"):
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
        item_kind = item.get("type") if kind is None else kind
        if item_kind not in ("content", "rule", "forget_request"):
            raise ValueError("invalid program-owned extraction kind")
        fields = {"text", "entities", "topics"}
        if item_kind == "content":
            fields.add("temporal_scope")
        if kind is None:
            fields.add("type")
        if set(item) != fields:
            raise ValueError(
                "item requires text, entities and topics, plus temporal_scope for content; type is program-owned"
            )
        if (
            not isinstance(item["text"], str)
            or not item["text"].strip()
            or not isinstance(item["entities"], list)
            or any(not isinstance(v, str) for v in item["entities"])
        ):
            raise ValueError("item needs nonempty text and string entities")
        if item_kind == "rule":
            rule_queries(item["text"])
        elif item_kind == "content" and item["temporal_scope"] not in ("current", "historical"):
            raise ValueError("content temporal_scope must be current or historical")
        items.append(ExtractedItem(
            item["text"], item_kind, item["entities"], item.get("temporal_scope"),
            normalize_topics(item["topics"]),
        ))
    return items, []


def parse_extraction(raw, allowed_candidates):
    """Validate all groups and their shared execution order atomically."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("response must contain rules, contents and forget_requests") from None
    groups = {"rules", "contents", "forget_requests"}
    if not isinstance(data, dict) or set(data) != groups:
        raise ValueError("exactly rules, contents and forget_requests are required")
    if any(not isinstance(data[k], list) for k in groups):
        raise ValueError("rules, contents and forget_requests must be arrays")
    rules = []
    for item in data["rules"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("candidate_ref"), str)
            or item["candidate_ref"] not in allowed_candidates
        ):
            raise ValueError("candidate_ref must name a supplied candidate")
        rules.append({k: v for k, v in item.items() if k != "candidate_ref"})
    parsed_rules, _ = parse_items(json.dumps({"items": rules}), kind="rule")
    ordered, seen = [], set()
    for group, kind in (("contents", "content"), ("forget_requests", "forget_request")):
        for item in data[group]:
            if not isinstance(item, dict) or type(item.get("order")) is not int or item["order"] < 1:
                raise ValueError("each content and forget request requires a positive integer order")
            order = item["order"]
            if order in seen:
                raise ValueError("order must be unique across contents and forget_requests")
            seen.add(order)
            parsed, _ = parse_items(json.dumps({"items": [
                {k: v for k, v in item.items() if k != "order"}
            ]}), kind=kind)
            ordered.append((order, parsed[0]))
    return parsed_rules + [item for _, item in sorted(ordered)], []


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
            "accepted_candidates": [
                {"ref": c["ref"], "message_no": c["message_no"],
                 "role": c["role"], "text": candidate_text(c)}
                for c in selected["rules"]
            ],
            # Full source text is already in messages; these are hints, not a whitelist.
            "forget_candidate_messages": sorted({c["message_no"] for c in selected["forget_requests"]}),
        }
        items = await self.extract_items(inputs, context)
        # One internal batch, rules first; candidates are never persisted as facts.
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
            if inputs["accepted_candidates"]
            else "No rule candidates are supplied. Return rules as []; extract contents and forget_requests from all messages."
        )
        instructions = context.store.prompts.text("extractor").replace(
            "{rule_instructions}", rule_instructions
        )
        prompt = instructions + "\n" + json.dumps(inputs, ensure_ascii=False)
        log_stage = "extraction.joint"
        await context.store.record(context, log_stage + ".input", {"prompt": prompt})
        allowed_candidates = {c["ref"] for c in inputs["accepted_candidates"]}

        def parse(raw):
            return parse_extraction(raw, allowed_candidates)

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
