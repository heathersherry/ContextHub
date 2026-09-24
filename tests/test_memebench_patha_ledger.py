from types import SimpleNamespace
import httpx
import pytest
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.knowledge import ContractError
from integrations.memebench.patha.ledger import (
    export_cost,
    rq1_costs,
    canonical_records,
)
from knowledge_helpers import MemorySink, identity, policy
from test_knowledge_retry import success
from test_knowledge_ledger import price


@pytest.mark.asyncio
async def test_core_retry_unknown_cost_not_zero_and_duplicate_exports_not_double_charged():
    sink = MemorySink()
    count = 0

    def send(req):
        nonlocal count
        count += 1
        return httpx.Response(503, json={}) if count == 1 else success(req)

    client = OpenAIChatClient(
        "synthetic", model="model-snapshot", transport=httpx.MockTransport(send)
    )
    try:
        await client.complete(
            "offline", call=RecordedCall(identity(), policy(), sink, price())
        )
    finally:
        await client.close()
    cost = export_cost([*sink.rows, *sink.rows], wall_seconds=0.1, attribution="test")
    assert cost["summary"]["model_attempts"] == 2 and len(cost["attempts"]) == 4
    assert cost["C_billed"] is None and cost["model_tariff_amount"] is None
    assert cost["summary"]["unknown_model_cost_attempts"] == 1
    assert canonical_records(sink.rows) == canonical_records([*sink.rows, *sink.rows])
    with pytest.raises(ContractError):
        canonical_records(
            [*sink.rows, sink.rows[0].model_copy(update={"input_sha256": "f" * 64})]
        )
    incomplete = export_cost(sink.rows[:1], wall_seconds=None, attribution="crash")
    assert (
        incomplete["summary"]["incomplete_attempts"] == 1
        and incomplete["C_billed"] is None
    )
    assert incomplete["T_wall"] is None


@pytest.mark.asyncio
async def test_shared_logical_cost_each_arm_physical_once():
    rows = []
    ops = []
    for _ in range(3):
        sink = MemorySink()
        ident = identity()
        ops.append(ident.execution_id)
        client = OpenAIChatClient(
            "synthetic", model="model-snapshot", transport=httpx.MockTransport(success)
        )
        try:
            await client.complete(
                "offline", call=RecordedCall(ident, policy(), sink, price())
            )
        finally:
            await client.close()
        rows.extend(sink.rows)
    costs = rq1_costs(
        SimpleNamespace(
            records=rows,
            shared_operations=(ops[0],),
            arm_operations=((ops[1],), (ops[2],)),
            shared_wall_seconds=1,
            arm_wall_seconds=(2, 3),
        )
    )
    assert costs["physical_total"]["summary"]["model_attempts"] == 3
    assert costs["selective"]["summary"]["model_attempts"] == 2
    assert costs["full-history"]["summary"]["model_attempts"] == 2
    assert costs["physical_total"]["T_wall"] == 6 and costs["selective"]["T_wall"] == 3
