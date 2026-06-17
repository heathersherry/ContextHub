from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkflowAnchor:
    workflow_id: str
    required_actions: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)


@dataclass
class ClosureChecklist:
    anchor: WorkflowAnchor
    completed_actions: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    require_decision: bool = False
    decision_label: str | None = None
    rule_citations: list[str] | None = None
