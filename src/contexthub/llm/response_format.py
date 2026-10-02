"""One observed provider wrapper; never search arbitrary text for JSON."""
import json

from contexthub.llm.retry_policy import ResponseParseError
from contexthub.models.execution import ResponseConversion
from contexthub.models.knowledge import text_hash

RESPONSE_FORMAT_VERSION = "leading-think-json-v1"


def unwrap_think_json(text):
    head = text.lstrip()
    if not head.startswith("<think>"):
        return text, None
    thinking, separator, remainder = head[len("<think>"):].partition("</think>")
    if not separator or "<think>" in thinking:
        raise ResponseParseError("incomplete_or_nested_think_wrapper")
    converted = remainder.strip()
    # loads consumes the WHOLE suffix; prose, fences and a second object fail.
    try:
        value = json.loads(converted)
    except ValueError as exc:
        raise ResponseParseError("think_suffix_not_complete_json") from exc
    if not isinstance(value, dict):
        raise ResponseParseError("think_suffix_not_json_object")
    return converted, ResponseConversion(rule=RESPONSE_FORMAT_VERSION,
        original_sha256=text_hash(text), converted_sha256=text_hash(converted),
        removed_prefix_characters=len(text) - len(remainder))
