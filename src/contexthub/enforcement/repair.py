from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from contexthub.enforcement.decision import Violation, ViolationKind


class RepairStrategy(StrEnum):
    DETERMINISTIC = "deterministic"
    ONE_SHOT_MODEL = "one_shot_model"
    ESCALATE = "escalate"
    FAIL_CLOSED = "fail_closed"


@dataclass
class RepairPlan:
    strategy: RepairStrategy
    violations: list[Violation] = field(default_factory=list)
    patch: dict | None = None
    note: str = ""


def plan_repair(violations: list[Violation]) -> RepairPlan:
    if not violations:
        return RepairPlan(
            strategy=RepairStrategy.DETERMINISTIC,
            note="no violation",
        )

    kinds = {v.kind for v in violations}
    fail_closed = {
        ViolationKind.UNAUTHORIZED_FLOW,
        ViolationKind.WRONG_OBJECT_MUTATION,
    }
    if kinds & fail_closed:
        return RepairPlan(
            strategy=RepairStrategy.FAIL_CLOSED,
            violations=list(violations),
            note="permission or wrong-object mutation",
        )

    if (
        ViolationKind.UNTRUSTED_PROVENANCE in kinds
        or ViolationKind.STALE_DEPENDENCY in kinds
    ):
        return RepairPlan(
            strategy=RepairStrategy.ESCALATE,
            violations=list(violations),
            note="needs refresh or human review",
        )

    if kinds <= {ViolationKind.SCHEMA_OR_ENUM, ViolationKind.INCOMPLETE_HANDOFF}:
        patch = _build_deterministic_patch(violations)
        if patch:
            return RepairPlan(
                strategy=RepairStrategy.DETERMINISTIC,
                violations=list(violations),
                patch=patch,
                note="field/enum normalization",
            )
        return RepairPlan(
            strategy=RepairStrategy.ONE_SHOT_MODEL,
            violations=list(violations),
            note="feed violations back for one retry",
        )

    return RepairPlan(
        strategy=RepairStrategy.ESCALATE,
        violations=list(violations),
    )


def _build_deterministic_patch(violations: list[Violation]) -> dict:
    patch: dict = {}
    for violation in violations:
        hint = violation.repair_hint or {}
        allowed = hint.get("allowed")
        arg = hint.get("arg")
        if arg and isinstance(allowed, list) and len(allowed) == 1:
            patch[arg] = allowed[0]
    return patch
