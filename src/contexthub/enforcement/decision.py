from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REPAIR = "repair"
    ESCALATE = "escalate"


class ViolationKind(StrEnum):
    UNAUTHORIZED_FLOW = "unauthorized_flow"
    INCOMPLETE_HANDOFF = "incomplete_handoff"
    STALE_DEPENDENCY = "stale_dependency"
    SCHEMA_OR_ENUM = "schema_or_enum"
    UNTRUSTED_PROVENANCE = "untrusted_provenance"
    WRONG_OBJECT_MUTATION = "wrong_object_mutation"
    UNCLOSED_WORKFLOW = "unclosed_workflow"
    WEAK_DECISION = "weak_decision"


@dataclass
class Violation:
    kind: ViolationKind
    message: str
    repair_hint: dict | None = None
    evidence: dict = field(default_factory=dict)


@dataclass
class GuardrailDecision:
    verdict: Verdict
    violations: list[Violation] = field(default_factory=list)
    reason: str = ""
    sanitized_payload: dict | None = None
    guardrail: str = ""
