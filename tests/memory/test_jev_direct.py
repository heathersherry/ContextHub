from .helpers import rule_item
from .helpers import bindings_response
"""Candidate-free intent plus LLM plans on real PostgreSQL and fixture HTTP."""

from dataclasses import replace
import json

import httpx
import pytest
import pytest_asyncio

from contexthub.llm.jev_client import JevClient
from contexthub.llm.retry_policy import ResponseSchemaError
from contexthub.memory.jev_intent_classifier import JevIntentClassifier
from contexthub.memory.prompts_config import PromptSet
from contexthub.memory.contracts import split_ref, ref_of
from contexthub.memory.storage import SCOPE
from integrations.memebench.memory_research.cli import argument_parser, freeze, decision_retry_policy
from .test_jev import Sink, call, result
from .helpers import add, pending, row, session, targets_response, input_of, item, trigger_response, repair_response


class Context:
    def __init__(self, prompts=None):
        self.prompts = prompts or PromptSet()
        self.store = self
        self.steps = []
        self.sink = Sink()

    async def record(self, context, stage, payload):
        self.steps.append((stage, payload))

    def make(self, stage, config, retry, **kwargs):
        recorded = call(self.sink)
        return replace(recorded, identity=recorded.identity.model_copy(update={"stage": stage}))


def choice_client(sequence, requests):
    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        assert set(data['state']) == {'incoming'}
        assert set(data['state']['incoming']) == {'text'}
        assert set(data['questions']) == {'intent'}
        question = data['questions']['intent']
        assert set(question['criteria']) == {'new', 'forget', 'correction', 'hold'}
        selected = sequence.pop(0)
        return httpx.Response(200, json=result({'intent': {
            'type': 'choice', 'choice': selected, 'confidence': 1.0,
            'probabilities': {key: float(key == selected) for key in question['criteria']},
        }}))
    return JevClient('fixture', transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def jev(env):
    sequence, requests = [], []
    client = choice_client(sequence, requests)
    env.executor.intent_classifier = JevIntentClassifier(client, retry=decision_retry_policy('jev'))
    yield sequence, requests
    assert not sequence
    await client.close()


def plan_response(*operations):
    # Each tuple supplies action, exact old text (or None), and generated text.
    def response(payload):
        data = input_of(payload)
        rows = data['candidates']
        assert set(data['incoming']) == {'text'}
        assert all(not {'created_in_session', 'timestamp'} & set(r) for r in rows)
        result = []
        for action, old, text in operations:
            target = next((r['ref'] for r in rows if r['text'] == old), None)
            assert target is not None or old is None
            result.append(dict(action=action, target_ref=target, text=text, entities=[]))
        return {'operations': result}
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', ['new', 'forget', 'correction', 'hold'])
async def test_single_question_uses_frozen_prompt_without_candidates(tmp_path, monkeypatch, intent):
    question = json.loads((PromptSet().root / 'p2_intent_jev.json').read_text())
    question['instructions'] += '\nFrozen marker'
    (tmp_path / 'p2_intent_jev.json').write_text(json.dumps(question))
    sequence, requests, context = [intent], [], Context(PromptSet(tmp_path))
    client = choice_client(sequence, requests)
    try:
        answer = await JevIntentClassifier(client, retry=decision_retry_policy('jev')).classify(
            {'text': 'fixture', 'created_in_session': 2, 'type': 'content'}, context)
    finally:
        await client.close()
    assert answer == intent and len(requests) == 1
    assert requests[0]['questions']['intent'] == question
    assert context.sink.records[-1].raw_output


@pytest.mark.asyncio
async def test_rule_add_and_content_hold_need_no_llm(env, jev):
    sequence, requests = jev
    c = await session(env, 1)
    keys, changes = await env.executor.process(pending(c, 'If health changes, then exercise changes', type='rule'), {}, c)
    assert len(keys) == 1 and (await row(env, keys[0]))['type'] == 'rule'
    assert changes[0]['operation'] == 'new' and not requests
    sequence.append('hold')
    assert await env.executor.process(pending(c, 'unclear'), {}, c) == ([], [])
    assert len(requests) == 1 and not env.prompts


@pytest.mark.asyncio
async def test_new_updates_one_duplicate_retires_another_and_leaves_other_matter(env, jev):
    sequence, requests = jev
    c1 = await session(env, 1)
    old = await add(env, c1, 'Home Shanghai')
    redundant = await add(env, c1, 'Currently living in Shanghai')
    food = await add(env, c1, 'Likes Shanghai food')
    rule = await add(env, c1, 'If home changes commute changes', type='rule')
    c2 = await session(env, 2)
    sequence.append('new')
    def response(payload):
        assert all(r['type'] == 'fact' for r in input_of(payload)['candidates'])
        return plan_response(('update', 'Home Shanghai', 'Home Beijing'),
                             ('invalidate', 'Currently living in Shanghai', None))(payload)
    env.responses.append(response)
    keys, changes = await env.executor.process(pending(c2, 'I moved to Beijing'),
        {key: await row(env, key) for key in [old, redundant, food, rule]}, c2)
    assert len(keys) == 1
    saved = await row(env, keys[0])
    assert saved['text'] == 'Home Beijing' and saved['memory_id'] == split_ref(old)[0]
    assert not (await row(env, old))['invalidated']
    assert (await row(env, redundant))['status'] == 'stale'
    assert (await row(env, food))['status'] == 'fresh'
    assert {t['operation'] for t in changes} == {'update', 'invalidate'}
    assert len(requests) == len(env.prompts) == 1


@pytest.mark.asyncio
async def test_new_history_then_duplicate_does_not_replace_current(env, jev):
    sequence, _ = jev
    c1 = await session(env, 1)
    current = await add(env, c1, 'Home Beijing')
    c2 = await session(env, 2)
    sequence.extend(['new', 'new'])
    env.responses.extend([plan_response(('history', 'Home Beijing', 'Home Shanghai last year')),
                          plan_response(('duplicate', 'Home Beijing', None))])
    keys, changes = await env.executor.process(pending(c2, 'Last year I lived in Shanghai'),
        {current: await row(env, current)}, c2)
    historical = await row(env, keys[0])
    assert historical['temporal_scope'] == 'historical' and not changes
    assert (await row(env, current))['status'] == 'fresh'
    keys, changes = await env.executor.process(pending(c2, 'I still live in Beijing'),
        {current: await row(env, current)}, c2)
    assert keys == [current] and changes == []
    assert len(await env.store.versions(env.trace, split_ref(current)[0])) == 2
    async with env.store.pool.acquire() as conn:
        sources = await conn.fetchval(f'SELECT session_seq FROM memory_sources WHERE {SCOPE} AND memory_id=$4 AND version=$5',
                                      *env.trace.args, *split_ref(current))
    assert sources == [1, 2]


@pytest.mark.asyncio
async def test_correction_multiple_generated_texts_and_rule_type(env, jev):
    sequence, _ = jev
    c1 = await session(env, 1)
    fact = await add(env, c1, 'Home Shanghai')
    rule = await add(env, c1, 'If it rains, then take the bus', type='rule')
    c2 = await session(env, 2)
    sequence.append('correction')
    env.responses.append(plan_response(('correct', 'Home Shanghai', 'Home Suzhou'),
                                       ('update', 'If it rains, then take the bus', 'If it rains, then take the subway')))
    keys, _ = await env.executor.process(pending(c2, 'The address was wrong; change the saved transport rule too'),
        {key: await row(env, key) for key in [fact, rule]}, c2)
    saved = [await row(env, key) for key in keys]
    assert [(r['text'], r['type']) for r in saved] == [('Home Suzhou', 'fact'), ('If it rains, then take the subway', 'rule')]
    assert (await row(env, fact))['invalidated']
    assert not (await row(env, rule))['invalidated']
    assert all('change the saved' not in r['text'] for r in saved)


@pytest.mark.asyncio
async def test_historical_correction_does_not_change_current(env, jev):
    sequence, _ = jev
    c1 = await session(env, 1)
    current = await add(env, c1, 'Home Beijing')
    env.responses.append(plan_response(('history', 'Home Beijing', 'Home Shanghai last year')))
    sequence.append('new')
    keys, _ = await env.executor.process(pending(c1, 'Home Shanghai last year'), {current: await row(env, current)}, c1)
    history = keys[0]
    c2 = await session(env, 2)
    sequence.append('correction')
    env.responses.append(plan_response(('correct', 'Home Shanghai last year', 'Home Suzhou last year')))
    keys, _ = await env.executor.process(pending(c2, 'Last year was Suzhou, not Shanghai'),
        {key: await row(env, key) for key in [history, current]}, c2)
    assert (await row(env, keys[0]))['temporal_scope'] == 'historical'
    assert (await row(env, history))['invalidated']
    assert (await row(env, current))['status'] == 'fresh'
    assert ref_of((await env.store.current_rows(env.trace))[0]) == current


@pytest.mark.asyncio
async def test_correction_can_retract_fact_and_revoke_rule_without_new_values(env, jev):
    sequence, _ = jev
    c = await session(env, 1)
    fact = await add(env, c, 'Package 2kg')
    rule = await add(env, c, 'If heavy choose courier A', type='rule')
    sequence.append('correction')
    env.responses.append(plan_response(('retract', 'Package 2kg', None),
                                       ('revoke', 'If heavy choose courier A', None)))
    keys, changes = await env.executor.process(pending(c, 'Weight was wrong; stop using the old courier rule'),
        {key: await row(env, key) for key in [fact, rule]}, c)
    assert not keys and len(changes) == 2
    assert (await row(env, fact))['invalidated'] and (await row(env, rule))['invalidated']


@pytest.mark.asyncio
async def test_forget_scans_beyond_retrieval_including_history_rules_and_embedded_disclosure(env, jev):
    sequence, _ = jev
    c1 = await session(env, 1)
    texts = [('Partner James', 'fact', 'current'), ('Cook with James', 'fact', 'current'),
             ('Partner was James last year', 'fact', 'historical'),
             ('If my partner James visits cook pasta', 'rule', 'current'),
             ('Colleague James handles invoices', 'fact', 'current')]
    records = [await add(env, c1, text, type=kind, scope=scope) for text, kind, scope in texts]
    preference = await add(env, c1, 'The user likes pasta')
    c2 = await session(env, 2)
    sequence.append('forget')
    def response(payload):
        data = input_of(payload)
        assert data['primary_ref'] is None and len(data['candidates']) == 6
        return targets_response([texts[i][0] for i in (0, 2, 3)])(payload)
    env.responses.append(response)
    focus = dict(pending(c2, 'Forget my relationship with James'), entities=['James'])
    assert (await env.executor.process(focus, {}, c2))[0] == []
    for key in [records[i] for i in (0, 2, 3)]:
        assert (await row(env, key))['status'] == 'deleted'
    for key in [records[i] for i in (1, 4)]:
        assert (await row(env, key))['status'] == 'fresh'
    assert (await row(env, preference))['status'] == 'fresh'


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', ['new', 'correction', 'forget'])
async def test_no_action_never_saves_request(env, jev, intent):
    sequence, _ = jev
    c = await session(env, 1)
    old = await add(env, c, 'Existing fact')
    candidates = {old: await row(env, old)}
    sequence.append(intent)
    env.responses.append({'target_refs': []} if intent == 'forget' else {'operations': []})
    assert await env.executor.process(pending(c, 'Unresolved request'), candidates, c) == ([], [])
    assert len(await env.store.current_rows(env.trace)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [
    {'action': 'new', 'target_ref': None, 'text': None, 'entities': []},
    {'action': 'update', 'target_ref': 'M999', 'text': 'invented', 'entities': []},
    {'action': 'forget', 'target_ref': 'M1', 'text': None, 'entities': []},
])
async def test_whole_plan_validated_before_any_write(env, jev, bad):
    sequence, _ = jev
    c = await session(env, 1)
    old = await add(env, c, 'Home Shanghai')
    original = await row(env, old)
    sequence.append('new')
    valid = dict(action='new', target_ref=None, text='Likes tea', entities=[])
    env.responses.extend([{'operations': [valid, bad]}] * 2)
    assert await env.executor.process(pending(c, 'New input'), {old: original}, c) == ([], [])
    assert await row(env, old) == original
    assert len(await env.store.current_rows(env.trace)) == 1
    assert len(env.prompts) == 2


@pytest.mark.asyncio
async def test_second_write_failure_rolls_back_first(env, jev, monkeypatch):
    sequence, _ = jev
    c = await session(env, 1)
    a = await add(env, c, 'Home Shanghai')
    b = await add(env, c, 'Work Shanghai')
    originals = {key: await row(env, key) for key in [a, b]}
    sequence.append('correction')
    env.responses.append(plan_response(('update', 'Home Shanghai', 'Home Beijing'),
                                       ('update', 'Work Shanghai', 'Work Suzhou')))
    insert, calls = env.store._insert, 0
    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('fixture storage failure')
        return await insert(*args, **kwargs)
    monkeypatch.setattr(env.store, '_insert', fail_second)
    with pytest.raises(RuntimeError, match='storage failure'):
        await env.executor.process(pending(c, 'Change the stored home and work'), originals, c)
    for key, original in originals.items():
        assert await row(env, key) == original
        assert len(await env.store.versions(env.trace, split_ref(key)[0])) == 1


@pytest.mark.asyncio
async def test_pipeline_shares_multiple_outputs_then_binds_and_propagates(env, jev):
    sequence, _ = jev
    env.retriever.config = env.retriever.config.model_copy(
        update={'hybrid_limit': 20, 'text_k': 20, 'vector_k': 20})
    c1 = await session(env, 1)
    home = await add(env, c1, 'Home Shanghai')
    work = await add(env, c1, 'Work Shanghai')
    exercise = await add(env, c1, 'Exercise swimming')
    rule = 'If home changes, then exercise becomes cycling'
    request = 'Change saved home to Beijing and work to Suzhou'
    sequence.extend(['correction', 'new'])
    def confirm(payload):
        data = input_of(payload)
        assert {'Home Beijing', 'Work Suzhou'} <= {r['text'] for r in data['candidates']}
        return plan_response(('duplicate', 'Home Beijing', None))(payload)
    env.responses.extend([
        {'items': [rule_item(rule)]}, {'items': [item(request), item('I still live in Beijing')]},
        plan_response(('update', 'Home Shanghai', 'Home Beijing'), ('update', 'Work Shanghai', 'Work Suzhou')),
        confirm,
        bindings_response(['Home Beijing'], ['Exercise swimming']),
        trigger_response(['Exercise swimming'], ['C2']),
        repair_response('Exercise cycling', [rule, 'Home Beijing']),
    ])
    refs = await env.full_pipeline.ingest(env.trace, 2, [{'role': 'user', 'content': rule + '. ' + request}])
    assert len(refs) == 4 and refs[1] == refs[3]
    current = await env.store.current_rows(env.trace)
    assert {'Home Beijing', 'Work Suzhou', 'Exercise cycling'} <= {r['text'] for r in current}
    assert (await row(env, home))['status'] == (await row(env, work))['status'] == 'stale'
    assert (await row(env, exercise))['status'] == 'stale'
    assert not env.responses


@pytest.mark.asyncio
@pytest.mark.parametrize('intent,name', [('new', 'p2_new'), ('correction', 'p2_correction'), ('forget', 'p2_forget_scope')])
async def test_branch_prompt_is_loaded_from_active_frozen_directory(env, jev, tmp_path, monkeypatch, intent, name):
    active = tmp_path / 'active-prompts'
    active.mkdir()
    for filename in ('p2_intent_jev.json', name + '.txt'):
        (active / filename).write_bytes((PromptSet().root / filename).read_bytes())
    marker = 'FROZEN_BRANCH_' + name
    path = active / (name + '.txt')
    path.write_text(path.read_text() + '\n' + marker)
    env.store.prompts = PromptSet(active)
    c = await session(env, 1)
    old = await add(env, c, 'Existing fact')
    jev[0].append(intent)
    env.responses.append({'target_refs': []} if intent == 'forget' else {'operations': []})
    await env.executor.process(pending(c, 'fixture'), {old: await row(env, old)}, c)
    assert marker in env.prompts[0]['messages'][0]['content']
    assert len(env.prompts) == 1


@pytest.mark.asyncio
async def test_correction_rejects_new_and_wrong_type_before_writing(env, jev):
    c = await session(env, 1)
    fact = await add(env, c, 'Home Shanghai')
    original = await row(env, fact)
    jev[0].append('correction')
    env.responses.extend([
        plan_response(('new', None, 'Please change address')),
        plan_response(('revoke', 'Home Shanghai', None)),
    ])
    assert await env.executor.process(pending(c, 'Change the address'), {fact: original}, c) == ([], [])
    assert await row(env, fact) == original


def test_cli_p2_only_and_manifest(tmp_path):
    parser = argument_parser()
    common = ['--config', 'fixture', '--mode', 'check', '--output', str(tmp_path)]
    assert parser.parse_args(common).p2_direct_backend == 'llm'
    args = parser.parse_args(common + ['--p2-direct-backend', 'jev', '--memory-model', 'gpt-6-luna'])
    manifest = freeze(tmp_path / 'frozen', {'index': {'embedding_model': 'text-embedding-3-small', 'dimensions': 1536}}, args)
    assert manifest['model_parameters']['p2_direct']['processing_model'] == 'gpt-6-luna'
    assert manifest['model_parameters']['p2_direct']['processing_reasoning_effort'] == 'low'
    assert manifest['maintenance_prompts']['p2_direct'] == 'p2_intent_jev.json'
    assert manifest['maintenance_prompts']['p2_new'] == 'p2_new.txt'
    assert manifest['maintenance_prompts']['p2_correction'] == 'p2_correction.txt'
    assert manifest['model_output_attempts']['p2_intent'] == 1
    assert manifest['model_output_attempts']['p2_direct'] == 2
    assert 529 in manifest['p2_direct_retry']['retry_status_codes']


@pytest.mark.parametrize('change', [
    {'choice': 'missing'}, {'choice': []}, {'confidence': True},
    {'confidence': float('nan')}, {'probabilities': {'a': .8}},
    {'probabilities': {'a': .8, 'b': .8}},
    {'probabilities': {'a': .2, 'b': .8}},
    {'probabilities': {'a': True, 'b': False}},
])
def test_invalid_choice_rejected(change):
    answer = {'type': 'choice', 'choice': 'a', 'confidence': .8, 'probabilities': {'a': .8, 'b': .2}}
    answer.update(change)
    with pytest.raises(ResponseSchemaError):
        JevClient._answers({'answers': {'q': answer}}, {'q': {'type': 'choice', 'criteria': {'a': '', 'b': ''}}})


def test_choice_accepts_rounded_probability_total_without_normalizing():
    # Observed in a real target selection: .55 + .04 + .34 + .06 = .99.
    answer = {
        'type': 'choice', 'choice': 'M1', 'confidence': .52,
        'probabilities': {'M1': .55, 'M16': .04, 'none': .34, 'unclear': .06},
    }
    data = {'answers': {'target': answer}}
    questions = {'target': {'type': 'choice', 'criteria': {
        k: '' for k in answer['probabilities']
    }}}
    assert JevClient._answers(data, questions) == data['answers']



@pytest.mark.asyncio
@pytest.mark.parametrize('scope,text', [
    ('current', 'The user was recently diagnosed with high blood pressure.'),
    ('historical', 'The user lived in Oxford until 2020.'),
    ('current', 'The user has a company offsite coming up.'),
])
async def test_empty_new_saves_upstream_content_and_time_without_luna(env, jev, scope, text):
    env.responses.append({'items': [dict(item(text, temporal_scope=scope), entities=['Oxford'] if scope == 'historical' else [])]})
    jev[0].append('new')
    await env.full_pipeline.ingest(env.trace, 1, [{'role': 'user', 'content': text}])
    assert len(env.prompts) == 1  # extraction only, no p2_new rewrite
    assert len(jev[1]) == 1
    assert not env.responses
    async with env.store.pool.acquire() as conn:
        rows = await conn.fetch(f'SELECT * FROM memories WHERE {SCOPE}', *env.trace.args)
        stages = await conn.fetch(f'SELECT stage FROM memory_steps WHERE {SCOPE}', *env.trace.args)
    assert len(rows) == 1
    saved = rows[0]
    assert saved['text'] == text and saved['temporal_scope'] == scope and saved['accepted']
    assert saved['created_in_session'] == 1 and saved['origin'] == 'extracted'
    assert ('oxford' in saved['entities']) == (scope == 'historical')
    assert 'p2_new.direct_save' in [r['stage'] for r in stages]
    assert 'p2_new.input' not in [r['stage'] for r in stages]
