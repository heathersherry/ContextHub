"""Current-session extraction only. Usability parsing is not semantic verification."""

import json
import logging

from contexthub.llm.retry_policy import ResponseSchemaError

from .contracts import rule_queries
from .schemas import ExtractedItem, Extraction, normalize_topics
from .jev_rule_gate import RULE_HINT

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
        if item_kind not in ("content", "rule"):
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
        elif item["temporal_scope"] not in ("current", "historical"):
            raise ValueError("content temporal_scope must be current or historical")
        items.append(ExtractedItem(
            item["text"], item_kind, item["entities"], item.get("temporal_scope"),
            normalize_topics(item["topics"]),
        ))
    return items, []


class ExtractionFailed(RuntimeError):
    pass


class Extractor:
    def __init__(self, client, *, retry, max_tokens=None, price=None, rule_gate=None):
        self.client, self.retry, self.max_tokens, self.price = (
            client,
            retry,
            max_tokens,
            price,
        )
        self.rule_gate = rule_gate

    async def extract(self, messages, context):
        # Read from this runtime's packaged or explicitly frozen prompt set.
        rule_prompt = context.store.prompts.text(
            "extractor_rules_normalize" if self.rule_gate else "extractor"
        )
        fact_prompt = context.store.prompts.text("extractor_facts")
        if self.rule_gate is not None:
            selected = await self.rule_gate.select(messages, context)
            if selected:
                rules = await self.extract_stage(
                    "rules", rule_prompt,
                    {"messages": messages, "accepted_candidates": selected}, context,
                    allowed_candidates={c["ref"] for c in selected},
                )
            else:
                rules = {"items": []}
                await context.store.record(context, "extraction.rules.skipped", {
                    "reason": "No candidates accepted by Jev rule gate",
                })
        elif any(
            m["role"] in ("user", "assistant") and RULE_HINT.search(m["content"])
            for m in messages
        ):
            rules = await self.extract_stage(
                "rules",
                rule_prompt,
                {"messages": messages},
                context,
            )
        else:
            rules = {"items": []}
            await context.store.record(
                context,
                "extraction.rules.skipped",
                {"reason": "No standalone if in user/assistant messages"},
            )
        facts = await self.extract_stage(
            "facts",
            fact_prompt,
            {"messages": messages, "rule_output": rules},
            context,
        )
        combined = {
            "items": [dict(i, type="rule") for i in rules["items"]]
            + [dict(i, type="content") for i in facts["items"]],
        }
        raw = json.dumps(combined, ensure_ascii=False)
        items, warnings = parse_items(raw, kind=None)
        await context.store.save_output(context.trace, context.session_seq, raw)
        await context.store.record(
            context,
            "extraction.parsed",
            {
                "items": [i.__dict__ for i in items],
                "warnings": warnings,
            },
        )
        return Extraction(items)

    async def extract_stage(
        self, stage, instructions, inputs, context, *, allowed_candidates=None
    ):
        prompt = instructions + "\n" + json.dumps(inputs, ensure_ascii=False)
        log_stage = "extraction." + stage
        await context.store.record(context, log_stage + ".input", {"prompt": prompt})

        def parse(raw):
            if allowed_candidates is not None:
                data = json.loads(raw)
                if (
                    not isinstance(data, dict)
                    or set(data) != {"items"}
                    or not isinstance(data["items"], list)
                ):
                    raise ValueError("normalization requires only an items array")
                cleaned = []
                for item in data["items"]:
                    if (
                        not isinstance(item, dict)
                        or not isinstance(item.get("candidate_ref"), str)
                        or item["candidate_ref"] not in allowed_candidates
                    ):
                        raise ValueError(
                            "candidate_ref must name a Jev-accepted source candidate"
                        )
                    cleaned.append({k: v for k, v in item.items() if k != "candidate_ref"})
                raw = json.dumps({"items": cleaned})
            items, warnings = parse_items(raw, kind="rule" if stage == "rules" else "content")
            return items, warnings

        error = None
        for attempt in range(1, 5):
            request = (
                prompt
                if error is None
                else prompt + "\nCorrect the previous output: " + error
            )
            call = context.make(
                "extraction_" + stage,
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
                items, warnings = parse(raw)
            except ValueError as exc:
                error = str(exc)
                await context.store.record(
                    context,
                    log_stage + ".validation_error",
                    {"attempt": attempt, "error": error, "raw_output": raw},
                )
                logger.warning(
                    "Extraction %s attempt %s/4 unusable: %s", stage, attempt, exc
                )
                continue
            for warning in warnings:
                logger.warning("%s", warning)
            await context.store.record(
                context,
                log_stage + ".parsed",
                {
                    "items": [i.__dict__ for i in items],
                    "warnings": warnings,
                },
            )
            return {"items": [
                dict(text=i.text, entities=i.entities, topics=i.topics, **(
                    {"temporal_scope": i.temporal_scope} if i.type == "content" else {}
                )) for i in items
            ]}
        raise ExtractionFailed(
            f"four unusable {stage} extraction responses; stop this episode strategy"
        )
