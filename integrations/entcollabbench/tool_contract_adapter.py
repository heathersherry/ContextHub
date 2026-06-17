"""Tool argument and contract adapters for EntCollabBench MCP calls."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from integrations.entcollabbench import mapping
from integrations.entcollabbench.mcp_runtime_adapter import normalize_tool_schema_record


_TEAMS_CAMEL_ALIASES = {
    "team_id": "teamId",
    "channel_id": "channelId",
    "content_type": "contentType",
    "reply_to_id": "replyToId",
    "message_id": "messageId",
}


def normalize_tool_args(server: str, tool_name: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize wrapper/raw-service aliases without dropping original keys."""

    normalized = dict(args or {})
    if str(server or "").strip().lower() != "teams":
        return normalized

    for snake, camel in _TEAMS_CAMEL_ALIASES.items():
        _mirror_alias(normalized, snake, camel)

    # EntCollabBench traces may expose Teams message text as either a flat
    # ``content`` value or the Graph-like ``body: {content, contentType}``.
    if "content" in normalized and "body" not in normalized:
        body: dict[str, Any] = {"content": normalized["content"]}
        if "contentType" in normalized:
            body["contentType"] = normalized["contentType"]
        elif "content_type" in normalized:
            body["contentType"] = normalized["content_type"]
        normalized["body"] = body
    elif isinstance(normalized.get("body"), Mapping):
        body = normalized["body"]
        if "content" in body and "content" not in normalized:
            normalized["content"] = body["content"]
        if "contentType" in body and "contentType" not in normalized:
            normalized["contentType"] = body["contentType"]
        if "contentType" in body and "content_type" not in normalized:
            normalized["content_type"] = body["contentType"]
    elif "body" in normalized and "content" not in normalized:
        normalized["content"] = normalized["body"]

    return normalized


def tool_schema_to_contract_fields(
    server: str,
    schema_record: Mapping[str, Any],
    *,
    required_role: str | None = None,
    mutation_intent: str = "",
) -> dict[str, Any]:
    """Build ``ToolCallContract`` fields from a live MCP ``inputSchema`` record."""

    normalized = normalize_tool_schema_record(schema_record)
    role = str(required_role).strip() if required_role is not None else None
    if role == "":
        role = None
    return {
        "tool_name": normalized["tool_name"],
        "required_role": role,
        "arg_schema": normalized["inputSchema"],
        "provenance_bound_args": [],
        "mutation_intent": str(mutation_intent or ""),
        # Store base context URI here. Versioned ctx://...@vN refs belong only
        # in runtime payloads such as declared context versions.
        "depends_on_uris": [mapping.tool_schema_uri(str(server).strip())],
    }


def _mirror_alias(args: dict[str, Any], left: str, right: str) -> None:
    if left in args and right not in args:
        args[right] = args[left]
    elif right in args and left not in args:
        args[left] = args[right]
