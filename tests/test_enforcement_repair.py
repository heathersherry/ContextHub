from __future__ import annotations

from contexthub.enforcement.decision import Violation, ViolationKind
from contexthub.enforcement.repair import RepairStrategy, plan_repair


def _violation(kind: ViolationKind, repair_hint: dict | None = None) -> Violation:
    return Violation(kind=kind, message=kind.value, repair_hint=repair_hint)


def test_empty_violations_are_deterministic_noop():
    plan = plan_repair([])

    assert plan.strategy == RepairStrategy.DETERMINISTIC
    assert plan.note == "no violation"


def test_permission_violation_fails_closed():
    plan = plan_repair([_violation(ViolationKind.UNAUTHORIZED_FLOW)])

    assert plan.strategy == RepairStrategy.FAIL_CLOSED


def test_wrong_object_mutation_fails_closed():
    plan = plan_repair([_violation(ViolationKind.WRONG_OBJECT_MUTATION)])

    assert plan.strategy == RepairStrategy.FAIL_CLOSED


def test_stale_dependency_escalates():
    plan = plan_repair([_violation(ViolationKind.STALE_DEPENDENCY)])

    assert plan.strategy == RepairStrategy.ESCALATE


def test_untrusted_provenance_escalates():
    plan = plan_repair([_violation(ViolationKind.UNTRUSTED_PROVENANCE)])

    assert plan.strategy == RepairStrategy.ESCALATE


def test_single_allowed_enum_gets_deterministic_patch():
    plan = plan_repair(
        [
            _violation(
                ViolationKind.SCHEMA_OR_ENUM,
                {"arg": "state", "allowed": ["resolved"], "got": "closed"},
            )
        ]
    )

    assert plan.strategy == RepairStrategy.DETERMINISTIC
    assert plan.patch == {"state": "resolved"}


def test_multiple_allowed_enum_uses_one_shot_model():
    plan = plan_repair(
        [
            _violation(
                ViolationKind.SCHEMA_OR_ENUM,
                {"arg": "state", "allowed": ["open", "resolved"], "got": "closed"},
            )
        ]
    )

    assert plan.strategy == RepairStrategy.ONE_SHOT_MODEL
    assert plan.patch is None
