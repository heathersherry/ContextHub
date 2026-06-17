from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolCallContract:
    tool_name: str
    required_role: str | None = None
    arg_schema: dict = field(default_factory=dict)
    provenance_bound_args: list[str] = field(default_factory=list)
    mutation_intent: str = ""
    depends_on_uris: list[str] = field(default_factory=list)
