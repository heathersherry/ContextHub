"""Current-session extraction only. Usability parsing is not semantic verification."""

import json
import logging
import re

from contexthub.llm.retry_policy import ResponseSchemaError

from .schemas import ExtractedItem

logger = logging.getLogger(__name__)
RULE_HINT = re.compile(
    r"\bif\b[^.!?\n]*\b(?:will|would|change|become|switch)\b[^.!?\n]*", re.I
)
from .contracts import prompt as read_prompt

PROMPT = read_prompt("extractor")


def parse_items(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("response is not a JSON object with readable items") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("items array required")
    items, warnings = [], []
    for index, item in enumerate(data["items"]):
        if not (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and item["text"].strip()
            and isinstance(item.get("type"), str)
            and isinstance(item.get("entities"), list)
            and all(isinstance(v, str) for v in item["entities"])
            and isinstance(item.get("message_refs"), list)
            and all(type(v) is int for v in item["message_refs"])
        ):
            warnings.append(
                f"item {index}: cannot map to memory columns; retained in raw response"
            )
            continue
        # Unknown type labels and nonexistent/negative refs are preserved, not retried.
        items.append(
            ExtractedItem(
                item["text"], item["type"], item["entities"], item["message_refs"]
            )
        )
        if item["type"] not in ("fact", "rule", "change"):
            warnings.append(f"item {index}: unknown type retained unresolved")
    if data["items"] and not items:
        raise ValueError("no readable items in nonempty batch")
    return items, warnings


class ExtractionFailed(RuntimeError):
    pass


class Extractor:
    def __init__(self, client, *, retry, max_tokens=None, price=None):
        self.client, self.retry, self.max_tokens, self.price = (
            client,
            retry,
            max_tokens,
            price,
        )

    async def extract(self, messages, context):
        hints = [
            {"message_no": m["message_no"], "matches": RULE_HINT.findall(m["content"])}
            for m in messages
            if RULE_HINT.search(m["content"])
        ]
        prompt = (
            PROMPT
            + "\n"
            + json.dumps(
                {"messages": messages, "possible_rules": hints}, ensure_ascii=False
            )
        )
        await context.store.record(context, "extraction.input", {"prompt": prompt})
        for attempt in range(1, 5):
            call = context.make(
                "extraction",
                {"prompt": PROMPT, "max_tokens": self.max_tokens},
                self.retry,
                extraction_attempt=attempt,
                parser=parse_items,
                price=self.price,
            )
            try:
                raw = await self.client.complete(
                    prompt, max_tokens=self.max_tokens, call=call
                )
            except ResponseSchemaError as exc:
                if str(exc) == "response_truncated_or_refused":
                    raise
                # A received empty text is a format failure, not a transport retry.
                raw = self.client.last_raw_response
                if not isinstance(raw, str):
                    raise
            else:
                raw = self.client.last_raw_response
            await context.store.save_output(context.trace, context.session_seq, raw)
            try:
                items, warnings = parse_items(raw)
            except ValueError as exc:
                logger.warning("Extraction attempt %s/4 unusable: %s", attempt, exc)
                continue
            for warning in warnings:
                logger.warning("%s", warning)
            await context.store.record(
                context,
                "extraction.parsed",
                {"items": [i.__dict__ for i in items], "warnings": warnings},
            )
            return items
        raise ExtractionFailed(
            "four unusable extraction responses; stop this episode strategy"
        )
