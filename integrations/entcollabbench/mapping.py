"""EntCollabBench <-> ContextHub concept mapping.

The mappings here are intentionally pure Python constants/functions so Task 8/9
can import them without importing EntCollabBench or starting MCP services.
Versioned ``ctx://...@vN`` values are runtime refs only; ContextHub
``contexts.uri`` rows must store the unversioned base URI.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


CTX_PREFIX = "ctx://entcollab"

ROLE_TO_DEPARTMENT: dict[str, str] = {
    "it_service_desk_l1": "it",
    "it_change_engineer": "it",
    "hr_service_specialist": "human_resources",
    "customer_support_specialist": "customer_service",
    "knowledge_base_specialist": "shared_services",
    "collaboration_ops_specialist": "shared_services",
    "developer_engineer": "engineering",
    "qa_test_engineer": "engineering",
    "finance_approval_specialist": "approval_center",
    "legal_approval_specialist": "approval_center",
    "procurement_approval_specialist": "approval_center",
}

ROLE_TO_SERVERS: dict[str, tuple[str, ...]] = {
    "it_service_desk_l1": ("calendar", "drive", "email", "itsm", "teams"),
    "it_change_engineer": ("calendar", "drive", "email", "itsm", "teams"),
    "hr_service_specialist": ("calendar", "drive", "email", "hr", "teams"),
    "customer_support_specialist": ("calendar", "csm", "drive", "email", "teams"),
    "knowledge_base_specialist": ("calendar", "csm", "drive", "email", "hr", "itsm", "teams"),
    "collaboration_ops_specialist": ("calendar", "drive", "email", "teams"),
    "developer_engineer": ("calendar", "drive", "email", "gitea", "teams"),
    "qa_test_engineer": ("calendar", "drive", "email", "gitea", "teams"),
    "finance_approval_specialist": ("workspace",),
    "legal_approval_specialist": ("workspace",),
    "procurement_approval_specialist": ("workspace",),
}

SERVER_TO_CONTEXT_TYPE: dict[str, str] = {
    "calendar": "tool_schema",
    "csm": "tool_schema",
    "drive": "tool_schema",
    "email": "tool_schema",
    "gitea": "tool_schema",
    "hr": "tool_schema",
    "itsm": "tool_schema",
    "teams": "tool_schema",
    "workspace": "tool_schema",
}

APPROVAL_ROLE_TO_POLICY_FAMILY: dict[str, str] = {
    "finance_approval_specialist": "finance",
    "legal_approval_specialist": "legal",
    "procurement_approval_specialist": "procurement",
}

_VERSION_TAG_RE = re.compile(
    r"^(?P<kind>role|tool_schema|tool|policy|object):(?P<name>[^@]+?)(?:@v(?P<version>[1-9][0-9]*))?$"
)
_RUNTIME_REF_RE = re.compile(r"^(?P<base>ctx://[^@]+?)(?:@v(?P<version>[1-9][0-9]*))?$")


def _clean_segment(value: str, *, field_name: str) -> str:
    segment = str(value or "").strip().strip("/")
    if not segment:
        raise ValueError(f"{field_name} must be non-empty")
    if "://" in segment or "@" in segment:
        raise ValueError(f"{field_name} must be a base path segment, not a URI or runtime ref")
    return segment


def _with_version(base_uri: str, version: int | None) -> str:
    if version is None:
        return base_uri
    if not isinstance(version, int) or version <= 0:
        raise ValueError("version must be a positive integer")
    return f"{base_uri}@v{version}"


def role_uri(role: str) -> str:
    """Return the base URI for an EntCollabBench role context."""
    return f"{CTX_PREFIX}/role/{_clean_segment(role, field_name='role')}"


def tool_schema_uri(tool_name: str, version: int | None = None) -> str:
    """Return a tool-schema URI, with optional versioned runtime ref."""
    base = f"{CTX_PREFIX}/tool_schema/{_clean_segment(tool_name, field_name='tool_name')}"
    return _with_version(base, version)


def policy_uri(policy_id: str, version: int | None = None) -> str:
    """Return an approval policy/rule URI, with optional versioned runtime ref."""
    base = f"{CTX_PREFIX}/policy/{_clean_segment(policy_id, field_name='policy_id')}"
    return _with_version(base, version)


def object_uri(object_id: str) -> str:
    """Return the base URI for a stateful enterprise business object."""
    return f"{CTX_PREFIX}/object/{_clean_segment(object_id, field_name='object_id')}"


def resolve_version_tag(tag: str) -> str:
    """Resolve shorthand tags like ``tool_schema:itsm@v3`` to ctx runtime refs."""
    raw = str(tag or "").strip()
    if not raw:
        raise ValueError("tag must be non-empty")

    runtime_match = _RUNTIME_REF_RE.match(raw)
    if runtime_match:
        return raw

    match = _VERSION_TAG_RE.match(raw)
    if not match:
        raise ValueError(
            "version tag must be '<kind>:<name>' or '<kind>:<name>@vN', "
            "where kind is role/tool_schema/tool/policy/object"
        )

    kind = match.group("kind")
    name = match.group("name")
    version_raw = match.group("version")
    version = int(version_raw) if version_raw is not None else None

    if kind == "role":
        if version is not None:
            raise ValueError("role contexts are not versioned runtime refs")
        return role_uri(name)
    if kind in {"tool", "tool_schema"}:
        return tool_schema_uri(name, version)
    if kind == "policy":
        return policy_uri(name, version)
    if kind == "object":
        if version is not None:
            raise ValueError("object contexts are not versioned runtime refs")
        return object_uri(name)
    raise ValueError(f"unsupported version tag kind: {kind}")


def role_to_owner_space(role: str) -> str:
    """Return the ContextHub owner_space for a role-owned context."""
    normalized = _clean_segment(role, field_name="role")
    department = ROLE_TO_DEPARTMENT.get(normalized)
    if department is None:
        raise KeyError(f"unknown EntCollabBench role: {normalized}")
    return department


def _extract_tool_name(ec_tool_def: Mapping[str, Any]) -> str:
    for key in ("tool_name", "name"):
        value = ec_tool_def.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    server = ec_tool_def.get("mcp_server_name") or ec_tool_def.get("server")
    tool_name = ec_tool_def.get("tool")
    if isinstance(server, str) and server.strip() and isinstance(tool_name, str) and tool_name.strip():
        return f"{server.strip()}.{tool_name.strip()}"
    raise ValueError("tool definition must contain 'tool_name' or 'name'")


def _extract_arg_schema(ec_tool_def: Mapping[str, Any]) -> dict[str, Any]:
    schema = ec_tool_def.get("inputSchema")
    if isinstance(schema, dict):
        return dict(schema)

    arguments = ec_tool_def.get("arguments")
    if isinstance(arguments, dict):
        return {
            "type": "object",
            "properties": {key: {} for key in sorted(arguments.keys())},
            "required": sorted(arguments.keys()),
        }

    return {"type": "object", "properties": {}, "required": []}


def to_tool_contract_fields(ec_tool_def: dict) -> dict:
    """Return fields suitable for constructing ``ToolCallContract``.

    Supported inputs are EntCollabBench MCP schema records from
    ``get_tool_schema`` and dataset ground-truth steps containing
    ``mcp_server_name``/``tool_name``/``agent``/``arguments``.
    """
    if not isinstance(ec_tool_def, dict):
        raise TypeError("ec_tool_def must be a dict")

    tool_name = _extract_tool_name(ec_tool_def)
    required_role = ec_tool_def.get("required_role") or ec_tool_def.get("agent")
    if required_role is not None:
        required_role = str(required_role).strip() or None

    server = ec_tool_def.get("mcp_server_name") or ec_tool_def.get("server")
    depends_on_uris = []
    if isinstance(server, str) and server.strip():
        depends_on_uris.append(tool_schema_uri(server.strip()))

    return {
        "tool_name": tool_name,
        "required_role": required_role,
        "arg_schema": _extract_arg_schema(ec_tool_def),
        "provenance_bound_args": [],
        "mutation_intent": str(ec_tool_def.get("mutation_intent") or ""),
        "depends_on_uris": depends_on_uris,
    }
