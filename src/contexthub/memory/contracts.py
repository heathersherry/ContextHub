"""Minimal model outputs; references name exact supplied versions."""

import re
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

def ref_of(row):
    return f"{row['memory_id']}:{row['version']}"


def split_ref(value):
    uid, version = value.rsplit(":", 1)
    number = int(version)
    if number < 1:
        raise ValueError("positive version required")
    return UUID(uid), number


def rule_queries(text):
    """Split normalized If/then prose without guessing a semantic boundary.

    An explicit ', then' allows commas inside the condition. In the simple
    'If A, B' form, the first comma is the condition/consequence boundary.
    """
    match = re.fullmatch(r"If\s+(.+)", text.strip(), flags=re.I | re.S)
    if match is None:
        raise ValueError("rule text must use If <condition>, then <consequence>")
    body = match[1]
    parts = re.split(r",\s*then\b", body, maxsplit=1, flags=re.I)
    if len(parts) == 1:
        parts = body.split(",", 1)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError("rule text must separate a nonempty condition and consequence")
    return dict(zip(("condition", "affected"), (part.strip() for part in parts)))


def material(row):
    return {
        "ref": ref_of(row),
        **{
            k: row[k]
            for k in (
                "text",
                "type",
                "origin",
                "status",
                "temporal_scope",
                "accepted",
                "invalidated",
                "basis_refs",
                "entities",
                "topics",
                "created_in_session",
                "timestamp",
            )
        },
    }


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectDecision(Contract):
    action: Literal[
        "new",
        "update",
        "correct",
        "duplicate",
        "history",
        "hold",
        "forget",
        "revoke",
        "invalidate",
        "retract",
    ]
    target_ref: str | None

    @model_validator(mode="after")
    def target(self):
        if self.action in ("new", "hold") and self.target_ref is not None:
            raise ValueError("new/hold require target_ref=null")
        if (
            self.action
            in (
                "update",
                "correct",
                "duplicate",
                "forget",
                "revoke",
                "invalidate",
                "retract",
            )
            and not self.target_ref
        ):
            raise ValueError("this action requires a supplied target")
        return self


class DirectOperation(DirectDecision):
    text: str | None
    entities: list[str]

    @model_validator(mode="after")
    def content(self):
        writes = self.action in ("new", "update", "correct", "history")
        if writes and (not self.text or not self.text.strip()):
            raise ValueError("a write requires self-contained text")
        if not writes and (self.text is not None or self.entities):
            raise ValueError("non-writing operations require null text and empty entities")
        return self


class DirectPlan(Contract):
    operations: list[DirectOperation]


class Targets(Contract):
    target_refs: list[str]


class RuleBindings(Contract):
    condition_refs: list[str]
    affected_refs: list[str]


class ConditionTrigger(Contract):
    fact_ref: str
    change_ids: list[str] = Field(min_length=1)


class TriggerDecision(Contract):
    triggers: list[ConditionTrigger]


class RepairDecision(Contract):
    can_repair: bool = Field(strict=True)
    text: str | None
    entities: list[str]
    basis_refs: list[str]
    retrieval_query: str | None

    @model_validator(mode="after")
    def supported(self):
        if self.can_repair and (
            not self.text or not self.text.strip() or not self.basis_refs
        ):
            raise ValueError("repair requires text and versioned premises")
        if not self.can_repair and (
            self.text is not None or self.entities or self.basis_refs
        ):
            raise ValueError("unsuccessful repair requires null text and empty arrays")
        if self.retrieval_query is not None and (
            self.can_repair or not self.retrieval_query.strip()
        ):
            raise ValueError("retrieval_query must name missing evidence for an unsuccessful repair")
        return self
