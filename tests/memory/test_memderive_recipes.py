"""Run official Q1/Q2/Q3 on public development data with explicit model fixtures.

This is a wiring test, never an effectiveness measurement. The real shared
engine ingests every public event; fixture extraction returns no assertions.
"""

import json
import os
from pathlib import Path

import pytest

pytest.importorskip('deriveval')
from deriveval.cli import run_plan
from deriveval.planner import plan_spec, persist_plan
from deriveval.spec import load_spec
from deriveval.store import Store
from integrations.memderive import backend as adapter
from integrations.memderive.prepare import prepare
from .test_application import engine_for
from .test_runtime_factory import config_dict


@pytest.mark.asyncio
@pytest.mark.parametrize('recipe', ['q1', 'q2', 'q3'])
async def test_official_recipe(env, tmp_path, monkeypatch, recipe):
    if not os.environ.get('DERIVEVAL_ROOT') or not os.environ.get('DERIVEMEM_ROOT'):
        pytest.skip('set DERIVEVAL_ROOT and DERIVEMEM_ROOT to the pinned source checkouts')
    data_root = Path(os.environ['DERIVEMEM_ROOT'])
    public_events = [json.loads(line) for line in
        (data_root / 'benchmark/data/tripops_w1/chronicle.jsonl').read_text().splitlines() if line]
    env.responses.extend([dict(rules=[], contents=[], forget_requests=[]) for _ in public_events])
    instances = []
    async def create(*args, **kwargs):
        engine = engine_for(env)
        instances.append(engine)
        return engine
    monkeypatch.setattr(adapter, 'create_memory_engine', create)
    for name in ('DATABASE_URL', 'CHAT_API_KEY', 'EMBEDDING_API_KEY'):
        monkeypatch.setenv('MEMDERIVE_' + name, 'fixture')
    output = prepare(deriveval_root=os.environ['DERIVEVAL_ROOT'], derivemem_root=data_root,
        memory_config=config_dict(tmp_path), output=tmp_path / 'experiment', worlds=['tripops_w1'],
        answer_route={'model': 'fixture', 'provider': 'local', 'window': 'local', 'local': True})
    path = output / f'{recipe}.json'
    raw = json.loads(path.read_text())
    if recipe == 'q1':
        raw['benchmark']['subset']['question_ids'] = ['q001']
        path.write_text(json.dumps(raw))
    spec = load_spec(path)
    store = Store(tmp_path / 'deriveval.sqlite')
    try:
        plan = persist_plan(store, plan_spec(spec))
        result = await run_plan(store, plan, offline_fixture=True)
        (tmp_path / 'run-result.json').write_text(json.dumps(result, indent=2, default=str))
        assert result['status'] == 'completed', result
        tasks = store.list_tasks(plan.experiment_id)
        assert all(task.status == 'completed' for task in tasks)
        assert instances and not env.responses  # every public event was ingested
        audits = [p for p in tmp_path.rglob('*.jsonl')
                  if p.parent.name == 'state' and not p.name.endswith('.attempts.jsonl')]
        # Maintenance provides its own state_root. Discover audit files by header.
        if recipe == 'q3':
            audits = []
            for p in tmp_path.rglob('*.jsonl'):
                first = p.read_text().splitlines()[:1]
                if first and json.loads(first[0]).get('kind') == 'namespace':
                    audits.append(p)
        audit = [json.loads(line) for p in audits for line in p.read_text().splitlines()]
        ingested = [row for row in audit if row['kind'] == 'ingest_complete']
        reads = [row for row in audit if row['kind'] == 'retrieve']
        assert len(ingested) == len(public_events)
        assert [r['event_id'] for r in ingested] == [e['event_id'] for e in public_events]
        assert reads
        if recipe == 'q3':
            assert len({r['sequence'] for r in reads}) == 5
            assert min(r['sequence'] for r in reads) < len(public_events)
        if recipe == 'q2':
            assert len(reads) > 1  # actual official state probes
        report = {'kind': 'offline_protocol_validation', 'recipe': recipe,
                  'world': 'tripops_w1', 'events_ingested': len(ingested),
                  'retrievals': len(reads), 'checkpoints': sorted({r['sequence'] for r in reads}),
                  'tasks_completed': len(tasks), 'status': result['status'],
                  'model_calls': 'deterministic fixtures; no paid API requests',
                  'effectiveness_result': False}
        (tmp_path / 'protocol-summary.json').write_text(json.dumps(report, indent=2) + '\n')
    finally:
        store.close()
