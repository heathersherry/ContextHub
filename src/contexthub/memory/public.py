"""Application contracts for managed conversational memory."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class MemoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1)
    timestamp: str | None = None


class MemoryIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    messages: list[MemoryMessage] = Field(min_length=1)
    timestamp: str | None = None


class MemoryIngestResult(BaseModel):
    namespace: str
    sequence: int
    status: Literal["complete"] = "complete"
    memory_refs: list[str]


class MemoryContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1)
    mode: Literal["auto", "answer", "policy", "history"] = "auto"
    max_chars: int | None = Field(default=None, ge=1)


class MemoryContextItem(BaseModel):
    ref: str
    text: str
    type: Literal["fact", "rule"]
    origin: Literal["extracted", "derived"]
    status: Literal["fresh", "stale"]
    temporal_scope: Literal["current", "historical"]
    source_sessions: list[int]
    basis_refs: list[str]


class MemoryContextResult(BaseModel):
    namespace: str
    sequence: int
    mode: Literal["answer", "policy", "history"]
    context: str
    items: list[MemoryContextItem]
    budget_exhausted: bool
