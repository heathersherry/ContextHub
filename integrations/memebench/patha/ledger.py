"""Attribute and export existing core attempts; never append or price them anew."""

from collections import defaultdict
from decimal import Decimal
from contexthub.models.execution import AttemptRecord
from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.services.execution_ledger import validate_append
from .rq1_runner import summarize


def canonical_records(records):
    unique = {}
    for raw in records:
        row = AttemptRecord.model_validate(
            raw.model_dump(mode="json") if isinstance(raw, AttemptRecord) else raw
        )
        key = (row.identity.account_id, row.identity.call_id, row.attempt_no, row.phase)
        if key in unique and unique[key] != row:
            raise ContractError("conflicting_attempt_export")
        unique[key] = row
    calls = defaultdict(list)
    for row in unique.values():
        calls[(row.identity.account_id, row.identity.call_id)].append(row)
    result = []
    for key in sorted(calls, key=str):
        history = []
        for row in sorted(
            calls[key], key=lambda r: (r.attempt_no, r.phase == "finished")
        ):
            validate_append(history, row)
            history.append(row)
        result.extend(history)
    return tuple(result)


def export_cost(records, *, wall_seconds, attribution, complete_export=True):
    rows = canonical_records(records)
    # Reuse S4's S2-compatible aggregator. AttemptRecord validates every charge
    # with the core calculate_cost, including failed/retried attempts.
    summary = summarize(
        rows, wall_seconds if wall_seconds is not None else 0
    ).model_dump(mode="json")
    if wall_seconds is None:
        summary["wall_seconds"] = None
    if not complete_export:
        summary["billed_total"] = None
        summary["billed_status"] = "unknown"
    model = [r for r in rows if r.phase == "finished" and r.requested_model is not None]
    unknown = (
        not complete_export
        or summary["unknown_model_cost_attempts"]
        or summary["incomplete_attempts"]
    )
    known = sum((r.cost.amount for r in model if r.cost.amount is not None), Decimal(0))
    tariff = (
        None if unknown or len({r.cost.currency for r in model}) > 1 else str(known)
    )
    return dict(
        attribution=attribution,
        complete_export=complete_export,
        summary=summary,
        # Monetary axis: external token charges recomputed from core usage and
        # frozen tariffs. Local work is a separate axis, NOT assumed free.
        C_billed=tariff,
        C_billed_status="recomputed_simple_tariff" if tariff is not None else "unknown",
        complete_monetary_amount=summary["billed_total"],
        T_wall=wall_seconds,
        W_local=summary["local_work"],
        model_tariff_amount=tariff,
        precision="S2 simple token tariff only; not a provider invoice; unpriced local work and missing usage remain unknown",
        attempts=[
            dict(
                account_id=r.identity.account_id,
                execution_id=str(r.identity.execution_id),
                call_id=str(r.identity.call_id),
                attempt_no=r.attempt_no,
                phase=r.phase,
                event_id=str(r.identity.event_id) if r.identity.event_id else None,
                work_item_id=str(r.identity.work_item_id)
                if r.identity.work_item_id
                else None,
                operation=r.identity.operation,
                stage=r.identity.stage,
                role=r.identity.model_role,
                sha256=canonical_hash(r),
            )
            for r in rows
        ],
        attempt_records_sha256=canonical_hash(
            [r.model_dump(mode="json") for r in rows]
        ),
    )


def rq1_costs(result):
    rows = canonical_records(result.records)
    if len(result.arm_operations) != 2:
        raise ContractError("rq1_cost_arms_incomplete")
    groups = [
        set(result.shared_operations),
        *(set(ops) for ops in result.arm_operations),
    ]
    if any(groups[i] & groups[j] for i in range(3) for j in range(i)):
        raise ContractError("cost_operations_overlap")
    if any(r.identity.execution_id not in set.union(*groups) for r in rows):
        raise ContractError("unattributed_core_attempt")
    shared = [r for r in rows if r.identity.execution_id in groups[0]]
    result_costs = {}
    for i, arm in enumerate(("selective", "full-history")):
        selected = [
            *shared,
            *(r for r in rows if r.identity.execution_id in groups[i + 1]),
        ]
        wall = (
            result.shared_wall_seconds + result.arm_wall_seconds[i]
            if result.shared_wall_seconds is not None
            and len(result.arm_wall_seconds) == 2
            else None
        )
        result_costs[arm] = export_cost(
            selected, wall_seconds=wall, attribution="P1/" + arm + "/logical"
        )
    result_costs["shared_physical"] = export_cost(
        shared,
        wall_seconds=result.shared_wall_seconds,
        attribution="P1/shared/physical_once",
    )
    wall = (
        result.shared_wall_seconds + sum(result.arm_wall_seconds)
        if result.shared_wall_seconds is not None and len(result.arm_wall_seconds) == 2
        else None
    )
    result_costs["physical_total"] = export_cost(
        rows, wall_seconds=wall, attribution="P1/physical_all_once"
    )
    return result_costs
