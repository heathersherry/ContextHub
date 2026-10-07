"""Small, explicit model output contracts. References always name a content version."""

from pathlib import Path
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


def prompt(name):
    return (PROMPTS / (name + ".txt")).read_text()


def ref_of(row):
    return f"{row['memory_id']}:{row['version']}"


def split_ref(value):
    uid, version = value.rsplit(":", 1)
    number = int(version)
    if number < 1:
        raise ValueError("positive version required")
    return UUID(uid), number


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
                "created_in_session",
                "timestamp",
                "status_reason",
            )
        },
    }


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Binding(Contract):
    content_ref: str | None
    target_ref: str | None
    relation: Literal[
        "new", "replace", "correct", "duplicate", "historical", "forget", "unresolved"
    ]
    temporal_scope: Literal["current", "historical"] | None
    evidence_refs: list[str]
    issues: list[str]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def conditions(self):
        if self.relation == "unresolved" and not self.issues:
            raise ValueError("unresolved needs a concrete issue")
        if self.relation != "unresolved" and self.issues:
            raise ValueError("a concrete issue cannot be silently accepted")
        if (
            self.relation in ("replace", "correct", "duplicate", "forget")
            and not self.target_ref
        ):
            raise ValueError("target required")
        if self.relation not in ("forget", "unresolved") and (
            not self.content_ref or not self.temporal_scope
        ):
            raise ValueError("content and temporal scope required")
        if self.relation == "historical" and self.temporal_scope != "historical":
            raise ValueError("historical supplement requires historical scope")
        if self.relation == "forget" and self.content_ref is not None:
            raise ValueError("forget does not supply replacement content")
        return self


class EdgeJudgment(Contract):
    pair_id: str
    yes: bool = Field(strict=True)
    reason: str = Field(min_length=1)


class Discovery(Contract):
    bindings: list[Binding] = Field(min_length=1)
    dependencies: list[EdgeJudgment]
    existing_edges: list[EdgeJudgment]


class Repair(Contract):
    target_ref: str
    can_repair: bool = Field(strict=True)
    text: str | None
    entities: list[str]
    basis_refs: list[str]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def supported(self):
        if self.can_repair and (
            not self.text or not self.text.strip() or not self.basis_refs
        ):
            raise ValueError("repair requires text and versioned evidence")
        if not self.can_repair and (self.text is not None or self.basis_refs):
            raise ValueError("unsuccessful repair must not supply a conclusion")
        return self
