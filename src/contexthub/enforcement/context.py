from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from contexthub.models.request import RequestContext


class Boundary(StrEnum):
    INVOCATION = "invocation"
    HANDOFF = "handoff"
    TOOL_CALL = "tool_call"
    STATE_MUTATION = "state_mutation"
    SHARED_MEMORY_WRITE = "shared_memory_write"
    CLOSURE = "closure"


@dataclass
class EnforcementContext:
    boundary: Boundary
    actor: RequestContext
    recipient: RequestContext | None = None
    payload: dict | None = None
    declared_context_uris: list[str] | None = None
    workflow_id: str | None = None

    def __post_init__(self) -> None:
        if self.payload is None:
            self.payload = {}
