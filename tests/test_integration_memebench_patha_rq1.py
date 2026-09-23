import asyncio
from uuid import uuid4
import pytest
from test_integration_knowledge_storage import s2_repo as isolated_repo

s2_repo = isolated_repo
from test_memebench_patha_rq1_runner import runner_config, runtime, runtime_transport
from integrations.memebench.patha.rq1_runner import RQ1Runner
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.services.artifact_service import ArtifactService


async def run_one(repo, account):
    requests = []
    ledger = ExecutionLedger(repo)
    runner = RQ1Runner(
        runner_config(),
        repository=repo,
        ledger=ledger,
        api_key="fixture",
        transport=runtime_transport(requests),
    )
    out = await runner.run(
        runtime(), account_id=account, run_id=uuid4(), replicate_id=0, cutoff_ordinal=1
    )
    return out, requests


@pytest.mark.asyncio
async def test_shared_extraction_fairness_cost_and_real_graph(s2_repo):
    account = "s4-" + str(uuid4())
    out, requests = await run_one(s2_repo, account)
    assert len([r for r in requests if r[0] == "extract"]) == 2
    assert len(out.shared_extractions) == 2
    for arm in out.arms:
        assert arm.status == "complete", arm.reasons
        assert arm.cost.input_tokens_known >= 34
        assert arm.cost.billed_total is None and arm.cost.billed_status == "unknown"
        async with s2_repo.session(account) as db:
            graph = await ArtifactService().get(db, arm.graph)
            assert len(graph.edges) == 1 and len(graph.nodes) == 2
            for ref, shared in zip(arm.reports, out.shared_extractions):
                report = await ArtifactService().get(db, ref)
                assert report.shared_extraction == shared
    shared_calls = 2
    assert (
        sum(a.cost.model_attempts for a in out.arms)
        == out.actual_batch_cost.model_attempts + shared_calls
    )
    async with s2_repo.session(account) as db:
        persisted = await db.fetch(
            "SELECT payload FROM execution_attempts WHERE account_id=$1", account
        )
    assert len(persisted) == len(out.records)
    assert all(r.identity.account_id == account for r in out.records)


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_share_counters(s2_repo):
    accounts = ["s4-" + str(uuid4()) for _ in range(2)]
    outputs = await asyncio.gather(*(run_one(s2_repo, a) for a in accounts))
    for account, (out, _) in zip(accounts, outputs):
        assert all(r.identity.account_id == account for r in out.records)
        assert all(a.status == "complete" for a in out.arms)
        assert out.actual_batch_cost.model_attempts == 6


@pytest.mark.asyncio
async def test_sidecar_changes_cannot_change_requests_decisions_or_semantic_graph(
    s2_repo,
):
    from integrations.memebench.patha.firewall import assert_sidecar_independent

    original = {"task_type": "Cas", "hop": 1, "gold": "secret-a", "paths": ["x"]}
    changed = {"task_type": "Abs", "hop": 2, "gold": "secret-b", "paths": ["y", "z"]}
    assert_sidecar_independent(runtime().model_dump(mode="json"), original, changed)
    runs = []
    for sidecar in (original, changed):
        account = "s4-" + str(uuid4())
        out, requests = await run_one(s2_repo, account)
        semantic = []
        async with s2_repo.session(account) as db:
            for arm in out.arms:
                graph = await ArtifactService().get(db, arm.graph)
                names = {}
                for ref in arm.reports:
                    report = await ArtifactService().get(db, ref)
                    names.update({m.node: m.text for m in report.mappings})
                semantic.append(
                    sorted(
                        (names[e.upstream], names[e.downstream], e.semantic_basis)
                        for e in graph.edges
                    )
                )
        runs.append((requests, semantic))

    def stable(value):
        if isinstance(value, dict):
            return {
                k: stable(v)
                for k, v in value.items()
                if k
                not in {
                    "id",
                    "source_id",
                    "context_id",
                    "account_id",
                    "sha256",
                    "ingested_at",
                    "cutoff_at",
                    "evidence_sha256",
                }
            }
        if isinstance(value, (list, tuple)):
            return [stable(v) for v in value]
        return value

    assert stable(runs[0][0]) == stable(runs[1][0])
    assert runs[0][1] == runs[1][1]


@pytest.mark.asyncio
async def test_failed_extraction_attempts_stay_in_both_arm_costs(s2_repo):
    import httpx

    calls = []

    def fail(request):
        calls.append(request)
        return httpx.Response(503, json={})

    ledger = ExecutionLedger(s2_repo)
    runner = RQ1Runner(
        runner_config(),
        repository=s2_repo,
        ledger=ledger,
        api_key="fixture",
        transport=httpx.MockTransport(fail),
    )
    out = await runner.run(
        runtime(),
        account_id="s4-" + str(uuid4()),
        run_id=uuid4(),
        replicate_id=0,
        cutoff_ordinal=0,
    )
    assert len(calls) == 3 and out.actual_batch_cost.model_attempts == 3
    assert all(a.status == "pending" and a.cost.model_attempts == 3 for a in out.arms)
    assert all(a.cost.unknown_usage_attempts == 3 for a in out.arms)


@pytest.mark.asyncio
async def test_timeout_remains_failed_and_incomplete_cost_not_zero(s2_repo):
    import httpx

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(503, json={})

    cfg = runner_config().model_copy(update={"timeout_seconds": 0.03})
    runner = RQ1Runner(
        cfg,
        repository=s2_repo,
        ledger=ExecutionLedger(s2_repo),
        api_key="fixture",
        transport=httpx.MockTransport(slow),
    )
    out = await runner.run(
        runtime(),
        account_id="s4-" + str(uuid4()),
        run_id=uuid4(),
        replicate_id=0,
        cutoff_ordinal=0,
    )
    assert all(a.status == "error" and "TimeoutError" in a.reasons for a in out.arms)
    assert out.actual_batch_cost.billed_total is None


@pytest.mark.asyncio
async def test_explicit_fixture_price_recomputes_shared_attribution(s2_repo):
    from decimal import Decimal
    from knowledge_helpers import NOW
    from contexthub.models.execution import PriceBasis

    p = PriceBasis(
        source="fixture only",
        observed_at=NOW,
        currency="FIX",
        region="fixture",
        billing_tier="fixture",
        model="fixture-model",
        unit_tokens=1,
        input_rate=Decimal("1"),
        output_rate=Decimal("2"),
    )
    base = runner_config()
    cfg = base.model_copy(
        update={
            "extraction_price": p,
            "adjudication": base.adjudication.model_copy(
                update={"weak_price": p, "strong_price": p}
            ),
        }
    )
    req = []
    runner = RQ1Runner(
        cfg,
        repository=s2_repo,
        ledger=ExecutionLedger(s2_repo),
        api_key="fixture",
        transport=runtime_transport(req),
    )
    out = await runner.run(
        runtime(),
        account_id="s4-" + str(uuid4()),
        run_id=uuid4(),
        replicate_id=0,
        cutoff_ordinal=1,
    )
    assert out.actual_batch_cost.known_model_amount == 6 * 23
    assert all(a.cost.known_model_amount == 4 * 23 for a in out.arms)
    assert (
        out.actual_batch_cost.billed_total is None
    )  # local work has no infrastructure tariff
