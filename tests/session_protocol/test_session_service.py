"""Service integration with SQL-shaped fake storage. No sockets or database."""
import copy
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.services.session_memory import SessionInput, Checkpoint, PgSessionStore, answer_view
from contexthub.services.session_models import SessionConfiguration
from contexthub.services.session_knowledge_service import SessionKnowledgeService


def config(tmp_path, profile='adaptive', search='selective'):
    retry = dict(max_attempts=1, timeout_seconds=2, backoff_seconds=[], retry_status_codes=[], retry_transport_errors=[])
    model = dict(id='offline', model='offline', base_url='https://invalid.example/v1', temperature=0,
        max_tokens=1000, prompt_template='{input_json}', rules_version='session-evidence-v2', retry=retry, price=None)
    return SessionConfiguration.model_validate(dict(protocol='session-evidence-v2',
        models={role: model for role in ('extraction','p1_weak','p1_strong','base','strong','verifier')},
        embedding=dict(model='offline-vector', base_url='https://invalid.example/v1', dimensions=2,
            send_dimensions=True, max_batch=8, response_archive_directory=str(tmp_path), retry=retry),
        embedding_price=None, profile=profile, search=search, planner=dict(lambda_p2=0,
            lambda_unit='targets/currency', currency=None, cost_mode='token_only_zero_lambda',
            u_max_algorithm='single-target-count-v1', estimates=[]), initial_candidates=2,
        expansion_factor=2, max_partial_rounds=2, session_timeout_seconds=10, explicit_rules=True))


class Repo:
    def __init__(self):
        self.rows = []
        self.account = None
        self.sql = []

    @asynccontextmanager
    async def session(self, account):
        assert self.account is None
        self.account = account
        old = copy.deepcopy(self.rows)
        try:
            yield self
        except BaseException:
            self.rows = old
            raise
        finally:
            self.account = None

    async def fetchrow(self, sql, account, stream):
        assert account == self.account
        rows = [r for r in self.rows if r['payload']['account_id'] == account and r['payload']['stream_id'] == stream]
        return copy.deepcopy(rows[-1]) if rows else None

    async def fetchval(self, sql, account):
        assert account == self.account
        return bool(self.rows) if 'knowledge_artifacts' in sql else False

    async def execute(self, sql, *args):
        self.sql.append(sql)
        if 'INSERT INTO knowledge_artifacts' in sql:
            account, ident, kind, sha, payload = args
            assert account == self.account and kind == 'session_checkpoint'
            self.rows.append(dict(sha256=sha, payload=copy.deepcopy(payload)))
        else:
            assert 'pg_advisory_xact_lock' in sql


def source(ordinal, *texts):
    messages = [dict(role='user', content=t) for t in texts]
    return SessionInput(source_id=f'src_{ordinal:024x}', ordinal=ordinal, source_version=1,
        timestamp=None, messages=messages, content_sha256=canonical_hash(messages))


class Models:
    def __init__(self):
        self.calls, self.embedded, self.call_ids = [], [], []
        self.ordinal = 0
        self.fail = False
        self.routing_complete = True
        self.reject_repair = False

    async def embed(self, texts):
        self.embedded.extend(texts)
        return tuple((1., float(len(t))) for t in texts)

    async def ask(self, role, operation, payload, schema, *, stage):
        self.calls.append((role, operation, copy.deepcopy(payload)))
        if self.fail:
            raise RuntimeError('synthetic failure')
        if operation == 'extract':
            s = payload['session']
            items = []
            for index, message in enumerate(s['messages']):
                text = ''.join(u['text'] for u in message['evidence_units'])
                if text == 'ordinary chat':
                    continue
                deletion = text.startswith('Forget ')
                attribute = text.split(' is ')[0] if not deletion else text.removeprefix('Forget ').rstrip('.')
                old = next((n for n in payload['memory'] if n['text'].startswith(attribute + ' is ') and n['status'] != 'deleted'), None)
                items.append(dict(action='delete' if deletion else 'replace' if old else 'add',
                    target_id=old['id'] if old else None, text=text,
                    kind='rule' if text.startswith('If ') else 'assertion',
                    evidence_ids=[u['id'] for u in message['evidence_units']], read_memory_ids=[]))
            data = dict(items=items, revisit_ids=[], routing_complete=self.routing_complete)
        elif operation == 'verify_ingest':
            target = payload['target']
            independent = target['text'].startswith('If ') or not (
                'recipient' in target['text'] and any(n['kind'] == 'rule' for n in payload['memory']))
            data = dict(applicable=True, prerequisites_satisfied=True, conflicts_resolved=True, kind_confirmed=True,
                independent=independent, memory_ids=[target['id']], dependency_ids=[], complete=True, missing=None)
        elif operation == 'verify':
            evidence = payload['evidence']
            target = payload['target']
            text = target.get('text','')
            independent = text.startswith('If ') or not ('recipient' in text and any('If ' in e['quote'] for e in evidence))
            data = dict(applicable=True,supported=True,conflicts_resolved=True,kind_confirmed=True,
                        independent=independent, evidence_ids=[e['id'] for e in evidence])
        elif operation == 'dependency':
            target = payload['target']['text']
            positives = []
            if 'recipient' in target and not target.startswith('If '):
                for n in payload['candidates']:
                    if 'lead is' in n['text'] or n['kind'] == 'rule':
                        positives.append(dict(upstream_id=n['id'], memory_ids=[n['id'], payload['target']['id']], evidence_ids=[],derivation='explicit rule',review=False))
            data = dict(positives=positives, identity_questions=[], missing=None, complete=True, independent=not positives)
        elif operation == 'generate':
            n = payload['target']
            data = dict(outcome='unresolved' if self.reject_repair else 'unchanged',text=None if self.reject_repair else n['text'],
                kind=n['kind'],evidence_ids=n['evidence'],read_ids=[])
        else:
            raise AssertionError(operation)
        return schema.model_validate(data)


def service(tmp_path, models=None, **kw):
    store = PgSessionStore(Repo(), 'isolated-offline', uuid4())
    return SessionKnowledgeService(config(tmp_path, **kw), store, models or Models())


@pytest.mark.asyncio
async def test_real_service_regular_update_literal_repair_index_and_checkpoint(tmp_path):
    s = service(tmp_path)
    await s.assert_empty()
    await s.ingest_session(source(0, 'My lead is Alice.', 'My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    first = await s.store.load()
    assert first.status == 'complete'
    assert len(first.state.edges) == 2
    before_calls = len(s.models.calls)
    await s.ingest_session(source(1, 'My lead is Carol.'))
    after = await s.store.load()
    assert after.status == 'complete'
    assert 'My recipient is Bob.' in [n.text for n in after.state.nodes]
    assert all(n.status == 'fresh' for n in after.state.nodes)
    assert any(a['operation'] == 'literal_repair' for a in after.audits)
    assert not any(c[1] == 'generate' for c in s.models.calls[before_calls:])
    # The rule vector is reused; changed source and changed result get new vectors.
    assert s.models.embedded.count('If my lead changes, my recipient will be Bob.') == 1
    assert len(after.history) == 2
    assert len(s.store.repository.rows) == 4
    await s.ingest_session(source(2, 'ordinary chat'))
    assert len(s.models.embedded) == 5
    assert (await s.store.load()).status == 'complete'


@pytest.mark.asyncio
async def test_delete_does_not_leak_old_raw_or_history_to_answer_view(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My lead is Secret.'))
    await s.ingest_session(source(1, 'Forget My lead.'))
    cp = await s.store.load()
    assert cp.state.nodes[0].status == 'deleted'
    assert not cp.vectors
    assert 'Secret' in cp.model_dump_json()  # Audit storage is retained.
    assert 'Secret' not in json.dumps(answer_view(cp))


@pytest.mark.asyncio
async def test_failed_checkpoint_cannot_answer_resume_or_skip_session(tmp_path):
    s = service(tmp_path)
    with pytest.raises(ContractError, match='source_order'):
        await s.ingest_session(source(1, 'ordinary chat'))
    s.models.fail = True
    with pytest.raises(RuntimeError):
        await s.ingest_session(source(0, 'My lead is Alice.'))
    assert (await s.store.load()).status == 'failed'
    with pytest.raises(ContractError, match='not_drained'):
        await s.snapshot()
    with pytest.raises(ContractError, match='previous_incomplete'):
        await s.ingest_session(source(1, 'ordinary chat'))


@pytest.mark.asyncio
async def test_incomplete_routing_cannot_report_all_stale_as_success(tmp_path):
    m = Models()
    m.routing_complete = False
    s = service(tmp_path, m)
    await s.ingest_session(source(0, 'My lead is Alice.'))
    cp = await s.store.load()
    assert cp.status == 'unresolved'
    assert cp.state.pending_targets
    assert not answer_view(cp)['current']


@pytest.mark.asyncio
async def test_store_cas_and_configuration_drift(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'ordinary chat'))
    cp = await s.store.load()
    with pytest.raises(ContractError, match='cas_conflict'):
        await s.store.save(cp, None)
    assert len(s.store.repository.rows) == 2
    with pytest.raises(ContractError, match='account_not_empty'):
        await s.assert_empty()
    s.config = s.config.model_copy(update={'initial_candidates': 4})
    with pytest.raises(ContractError, match='config_drift'):
        await s.ingest_session(source(1, 'ordinary chat'))


@pytest.mark.asyncio
@pytest.mark.parametrize('profile,roles', [('local/base-only',['base']), ('full/strong-only',['strong']),('adaptive',['base','base','strong'])])
async def test_repair_escalation_bounded_and_m3_evidence_identical(tmp_path, profile, roles):
    m = Models()
    m.reject_repair = True
    s = service(tmp_path,m,profile=profile)
    await s.ingest_session(source(0,'My lead is Alice.','My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    cp = await s.store.load()
    assert cp.status == 'unresolved'
    calls = [c for c in m.calls if c[1] == 'generate']
    assert [c[0] for c in calls] == roles
    if profile == 'adaptive':
        assert calls[1][2] == calls[2][2]


def test_prefix_evidence_cannot_inject_privileged_fields(tmp_path):
    raw = source(0,'ordinary chat').model_dump(mode='json')
    for key in ['questions','gold_facts','task_type','session_id','hop']:
        with pytest.raises(ValueError):
            SessionInput.model_validate(raw | {key:'secret'})
    with pytest.raises(ValueError,match='nonsemantic_id'):
        SessionInput.model_validate(raw | {'source_id':'src_'+'f'*24})


@pytest.mark.asyncio
async def test_recorded_factory_runner_whole_episode_official_answer_and_no_feedback(tmp_path):
    from hashlib import sha256
    from integrations.memebench.patha.answer_prompt import OfficialAnswerConfig
    from integrations.memebench.patha.session_factory import create_session_components, run_session_arm
    from test_session_protocol import package
    template = 'CONTEXT: {context}\nQUESTION: {question}'
    path = tmp_path / 'official_fixture.py'
    path.write_text('UNIFIED_ANSWER_PROMPT = ' + repr(template))
    cfg = config(tmp_path)
    ac = OfficialAnswerConfig.model_validate(cfg.models['base'].model_dump(mode='json') | dict(
        adapter='meme-unified-answer-v1', context_serialization='sealed-contexts-canonical-json-v1',
        official_source=dict(path=str(path), sha256=sha256(path.read_bytes()).hexdigest()),
        max_tokens=500, prompt_template=template))
    class Sink:
        def __init__(self): self.rows = []
        async def append(self, row): self.rows.append(row)
    sink, requests = Sink(), []
    def handler(request):
        data = json.loads(request.content)
        prompt = data['messages'][0]['content']
        requests.append(prompt)
        if prompt.startswith('CONTEXT:'):
            assert 'sources' not in prompt and 'SECRET' not in prompt
            content = 'ANSWER-NO-FEEDBACK'
        else:
            wire = json.loads(prompt)
            assert 'question' not in json.dumps(wire['input']).lower()
            assert 'ANSWER-NO-FEEDBACK' not in prompt
            assert len(wire['input']['session']['messages']) == 2
            content = json.dumps(dict(items=[],revisit_ids=[],routing_complete=True))
        return httpx.Response(200,json=dict(model='offline',usage=dict(prompt_tokens=10,completion_tokens=5,total_tokens=15),
            choices=[dict(message=dict(content=content))]))
    repo, stream = Repo(), uuid4()
    with pytest.raises(ContractError,match='authorization_required'):
        create_session_components(repository=repo,config=cfg,answer_config=ac,account_id='maintenance',
            answer_account_id='answers',stream_id=stream)
    components = create_session_components(repository=repo,config=cfg,answer_config=ac,account_id='maintenance',
        answer_account_id='answers',stream_id=stream,transport=httpx.MockTransport(handler),ledger=sink)
    runtime,boundary,directory = package(tmp_path)
    result = await run_session_arm(components,runtime,boundary,question_directory=directory)
    assert len(result['sessions']) == 5
    assert [len(result['answers'][p]) for p in ('before','after')] == [2,3]
    assert all(row['error'] is None for p in ('before','after') for row in result['answers'][p])
    assert len(requests) == 10
    assert len(sink.rows) == 20
    assert sum(r.usage.total_tokens or 0 for r in sink.rows if r.phase == 'finished') == 150
    assert {r.identity.account_id for r in sink.rows if r.identity.stage == 'answer'} == {'answers'}


@pytest.mark.asyncio
async def test_new_rule_opens_old_target_and_combined_change_keeps_old_graph(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0,'My lead is Alice.','My recipient is Alice.'))
    assert not (await s.store.load()).state.edges
    await s.ingest_session(source(1,'My lead is Carol.','My hobby is Chess.',
        'If my lead changes, my recipient will be Bob.'))
    cp = await s.store.load()
    assert len(cp.state.edges) == 2
    # Newly learned rule cannot retroactively take the model-free repair path.
    assert not any(a['operation'] == 'literal_repair' for a in cp.audits)
    assert any(c[1] == 'generate' for c in s.models.calls)
    assert any(n.text == 'My hobby is Chess.' and n.status == 'fresh' for n in cp.state.nodes)
    assert any(a['operation'] == 'delta' and a['affected'] for a in cp.audits)


@pytest.mark.asyncio
async def test_historical_derived_value_preserves_sealed_validity(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0,'My lead is Alice.','My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    await s.ingest_session(source(1,'My lead is Carol.'))
    view = answer_view(await s.store.load())
    assert 'My recipient is Alice.' in [n['text'] for n in view['historical']]
    assert 'My recipient is Bob.' in [n['text'] for n in view['current']]


@pytest.mark.asyncio
async def test_recorded_embedding_and_schema_failure_still_have_attempts(tmp_path):
    from contexthub.llm.chat_client import OpenAIChatClient
    from contexthub.llm.recorded_embedding import RecordedEmbeddingClient
    from contexthub.services.session_models import RecordedSessionModels, Extraction
    cfg = config(tmp_path)
    class Sink:
        rows = []
        async def append(self, record): self.rows.append(record)
    sink = Sink()
    def handler(request):
        data = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200,json=dict(model='offline-vector',usage=dict(prompt_tokens=7,total_tokens=7),
                data=[dict(index=i,embedding=[1.,2.]) for i in range(len(data['input']))]))
        return httpx.Response(200,json=dict(model='offline',usage=dict(prompt_tokens=4,completion_tokens=1,total_tokens=5),
            choices=[dict(message=dict(content='{}'))]))
    transport = httpx.MockTransport(handler)
    chat = OpenAIChatClient('offline',model='offline',transport=transport)
    embedding = RecordedEmbeddingClient(cfg.embedding,api_key='offline',transport=transport)
    models = RecordedSessionModels(cfg,{'extraction':chat},embedding,sink,'offline',uuid4())
    try:
        assert await models.embed(['memory']) == ((1.,2.),)
        with pytest.raises(Exception):
            await models.ask('extraction','extract',{},Extraction,stage='P1')
    finally:
        await chat.close()
        await embedding.close()
    finished = [r for r in sink.rows if r.phase == 'finished']
    assert len(finished) == 2
    assert finished[0].usage.completeness == 'partial'
    assert finished[0].cost.amount is None
    assert finished[1].outcome == 'error' and finished[1].usage.total_tokens == 5


@pytest.mark.asyncio
async def test_rq1_pair_uses_one_extraction_and_identical_frozen_inventory(tmp_path):
    s = service(tmp_path)
    s.config = s.config.model_copy(update={'paired_rq1':True})
    await s.ingest_session(source(0,'My lead is Alice.','My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    assert len([c for c in s.models.calls if c[1]=='extract']) == 1
    cp = await s.store.load()
    pair = next(a for a in cp.audits if a['operation']=='paired_rq1')
    assert pair['operational_arm']=='selective'
    assert set(pair['states'])=={'selective','full-history'}
    for arm in pair['states'].values():
        assert {(n['id'],n['version'],n['text']) for n in arm['nodes']} == {(n.id,n.version,n.text) for n in cp.state.nodes}
    assert s.config.search=='selective' and not s.models.reference


@pytest.mark.asyncio
async def test_independent_judge_keeps_missing_phases_and_never_calls_maintenance(tmp_path):
    from types import SimpleNamespace
    from test_session_protocol import package
    from integrations.memebench.patha.session_evaluation import judge_session_result, session_costs
    runtime,boundary,directory = package(tmp_path)
    label = SimpleNamespace(episode_id=runtime.episode_id,record_id='r',record_sha256='a'*64,
                            before_question_id='b',after_question_id='a')
    result = dict(protocol='session-evidence-v2',episode_id=runtime.episode_id,
                  states={},answers={},failure='synthetic_failure')
    evaluated = await judge_session_result(result,boundary=boundary,question_directory=directory,
        labels=[label],judge=None,run_id=uuid4(),replicate_id=0,profile='adaptive')
    assert len(evaluated['judgments'])==2
    assert all(j['correct'] is False for j in evaluated['judgments'])
    assert not evaluated['attempts'] and not evaluated['maintenance_feedback']
    costs = session_costs([])
    assert costs['episode']['T_wall'] is None
    assert 'P1-reference' in costs['parts']


@pytest.mark.asyncio
async def test_three_trajectories_isolated_and_failed_profile_retained(tmp_path):
    from types import SimpleNamespace
    from test_session_protocol import package
    from integrations.memebench.patha.session_runner import SessionEpisodeRunner
    from integrations.memebench.patha.session_factory import run_session_comparison
    profiles = ('local/base-only','full/strong-only','adaptive')
    components = {}
    class Answer:
        def __init__(self,account): self.account_id,self.call_ids,self.config = account,[],{"synthetic_answer": "fixed"}
        async def __call__(self,q,state): return 'isolated answer'
    async def close(): pass
    for i,profile in enumerate(profiles):
        s = service(tmp_path,profile=profile)
        s.store.account_id = f'maintenance-{i}'
        a = Answer(f'answer-{i}')
        components[profile] = SimpleNamespace(service=s,answerer=a,runner=SessionEpisodeRunner(s,a),close=close)
    components[profiles[0]].service.models.fail=True
    runtime,boundary,directory = package(tmp_path)
    result = await run_session_comparison(components,runtime,boundary,question_directory=directory)
    assert set(result['profiles'])==set(profiles)
    assert result['profiles'][profiles[0]]['arm_status']=='failed'
    assert all(result['profiles'][p]['arm_status']=='complete' for p in profiles[1:])
    assert all(len(result['profiles'][p]['sessions'])==5 for p in profiles[1:])
