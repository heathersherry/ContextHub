"""Three-group extraction, conservative hints, batched intents and ordered deletion."""
import json

import httpx
import pytest

from contexthub.llm.jev_client import JevClient
from contexthub.llm.retry_policy import ResponseSchemaError
from contexthub.memory.extractor import parse_extraction, parse_items
from contexthub.memory.jev_candidate_gate import JevCandidateGate, forget_candidates
from contexthub.memory.jev_intent_classifier import JevIntentClassifier
from contexthub.memory.storage import SCOPE
from integrations.memebench.memory_research.cli import decision_retry_policy
from .helpers import extraction_response, forget_item, item, input_of, add, session, row, targets_response
from .test_jev import result
from .test_jev_direct import Context, choice_client, jev, plan_response  # noqa: F401
from .test_jev_rule_gate import gate_client


def test_forget_hints_are_case_insensitive_whole_words_with_original_offsets():
    text = 'A removable label. DELETE that. Forget or REMOVE this. Forgotten and deleted are not exact keywords.'
    candidates = forget_candidates([
        {'role': 'system', 'content': 'Forget system data'},
        {'role': 'user', 'content': text},
        {'role': 'assistant', 'content': 'Please forget that.'},
        {'role': 'tool', 'content': 'remove this'},
    ])
    assert [c['ref'] for c in candidates] == ['F1', 'F2', 'F3']
    assert [c['message_no'] for c in candidates] == [2, 2, 3]
    assert [c['text'] for c in candidates[:2]] == ['DELETE that.', 'Forget or REMOVE this.']
    for c in candidates[:2]:
        assert text[c['sentence_start']:c['sentence_start'] + len(c['text'])] == c['text']


@pytest.mark.asyncio
async def test_rule_and_forget_use_independent_questions_in_one_gate_call():
    requests, context = [], Context()
    labels = {'R1': 'rule', 'F1': 'unclear', 'F2': 'not_forget', 'F3': 'forget'}
    client = gate_client(labels, requests)
    try:
        selected = await JevCandidateGate(client, retry=decision_retry_policy('jev')).select([
            {'role': 'user', 'content': 'If I leave the company, forget my work address.'},
            {'role': 'user', 'content': 'Remove the comma in that document.'},
            {'role': 'user', 'content': 'Forget my home address.'},
        ], context)
    finally:
        await client.close()
    assert len(requests) == 1
    assert [c['ref'] for c in selected['rules']] == ['R1']
    assert [c['ref'] for c in selected['forget_requests']] == ['F1', 'F3']
    payload = requests[0]
    assert set(payload['state']) == {'rule_policy', 'forget_policy'}
    assert set(payload['questions']['R1']['criteria']) == {'rule', 'not_rule', 'unclear'}
    assert set(payload['questions']['F1']['criteria']) == {'forget', 'not_forget', 'unclear'}
    assert payload['questions']['R1']['instructions']['candidate'] == payload['questions']['F1']['instructions']['candidate']
    for policy in payload['state'].values():
        assert json.dumps(payload).count(json.dumps(policy['instructions'])) == 1
        for description in policy['criteria'].values():
            assert json.dumps(payload).count(json.dumps(description)) == 1


@pytest.mark.asyncio
async def test_no_keyword_does_not_block_forget_and_hint_does_not_duplicate_text(env):
    client = gate_client({'F1': 'unclear'}, [])
    env.pipeline.extractor.candidate_gate = JevCandidateGate(client, retry=decision_retry_policy('jev'))
    requests = [forget_item('Forget James.', entities=['James']),
                forget_item('Stop retaining my home address.', order=2)]
    def extract(payload):
        data = input_of(payload)
        assert data['forget_candidate_messages'] == [1]
        assert data['accepted_candidates'] == []
        assert data['messages'][1]['content'] == 'Stop retaining my home address.'
        assert set(data) == {'messages', 'accepted_candidates', 'forget_candidate_messages'}
        return extraction_response(rules=[], contents=[], forget_requests=requests)
    env.responses.append(extract)
    try:
        extracted = await env.pipeline.ingest(env.trace, 1, [
            {'role': 'user', 'content': 'Forget James.'},
            {'role': 'user', 'content': 'Stop retaining my home address.'},
        ])
    finally:
        await client.close()
    assert [i.type for i in extracted] == ['forget_request', 'forget_request']
    assert all(i.temporal_scope is None for i in extracted)
    saved = (await env.store.get_session(env.trace, 1))['extraction_output']
    restored, _ = parse_items(saved, kind=None)
    assert restored == extracted
    assert not env.embedded and not await env.store.current_rows(env.trace)


@pytest.mark.parametrize('bad_order', [None, True, 0, -1, '1'])
def test_order_is_required_positive_integer(bad_order):
    response = extraction_response(rules=[], contents=[item('fact')])
    response['contents'][0]['order'] = bad_order
    if bad_order is None:
        del response['contents'][0]['order']
    with pytest.raises(ValueError, match='order'):
        parse_extraction(json.dumps(response), set())


def test_parser_interleaves_arrays_and_rejects_duplicate_order_or_invalid_forget():
    response = extraction_response(rules=[], contents=[dict(item('Remember James again'), order=2)],
                                   forget_requests=[forget_item('Forget James', entities=['James'], order=1)])
    parsed, _ = parse_extraction(json.dumps(response), set())
    assert [i.type for i in parsed] == ['forget_request', 'content']
    assert all('order' not in i.__dict__ for i in parsed)
    response['contents'][0]['order'] = 1
    with pytest.raises(ValueError, match='unique'):
        parse_extraction(json.dumps(response), set())
    response['contents'][0]['order'] = 2
    response['forget_requests'][0]['temporal_scope'] = 'current'
    with pytest.raises(ValueError):
        parse_extraction(json.dumps(response), set())


@pytest.mark.asyncio
async def test_batch_classifier_shares_rubric_returns_input_order_and_skips_empty():
    requests, context = [], Context()
    client = choice_client(['new', 'correction', 'hold'], requests)
    classifier = JevIntentClassifier(client, retry=decision_retry_policy('jev'))
    try:
        assert await classifier.classify([], context) == [] and not requests
        assert await classifier.classify([{'text': t} for t in ['Likes tea', 'Earlier claim was wrong', 'Why?']], context) == ['new', 'correction', 'hold']
    finally:
        await client.close()
    assert len(requests) == 1
    payload = requests[0]
    assert list(payload['questions']) == ['C1', 'C2', 'C3']
    assert all('forget' not in q['criteria'] for q in payload['questions'].values())
    for description in payload['state']['intent_policy']['criteria'].values():
        assert json.dumps(payload).count(json.dumps(description)) == 1


@pytest.mark.asyncio
async def test_invalid_batch_aborts_before_any_fact_is_written(env):
    client = JevClient('fixture', transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result({}))))
    env.executor.intent_classifier = JevIntentClassifier(client, retry=decision_retry_policy('jev'))
    env.responses.append(extraction_response(rules=[], contents=[item('Likes tea'), item('Likes coffee')]))
    try:
        with pytest.raises(ResponseSchemaError):
            await env.full_pipeline.ingest(env.trace, 1, [{'role': 'user', 'content': 'I like tea and coffee.'}])
    finally:
        await client.close()
    assert not await env.store.current_rows(env.trace)


@pytest.mark.asyncio
async def test_explicit_forget_removes_james_activity_without_content_classification(env, jev):  # noqa: F811
    c = await session(env, 1)
    texts = ['My partner is James.', 'I cook pasta with James.', 'My colleague James handles invoices.', 'I like pasta.']
    refs = [await add(env, c, text) for text in texts]
    request = 'Forget that my partner is James.'
    env.responses.extend([
        extraction_response(rules=[], contents=[], forget_requests=[forget_item(request, entities=['James'], topics=['partner identity'])]),
        targets_response(texts[:2]),
    ])
    await env.full_pipeline.ingest(env.trace, 2, [{'role': 'user', 'content': request}])
    assert not jev[1]
    assert [(await row(env, r))['status'] for r in refs] == ['deleted', 'deleted', 'fresh', 'fresh']
    assert 'document: ' + request not in env.embedded
    async with env.store.pool.acquire() as conn:
        assert await conn.fetchval(f'SELECT count(*) FROM memories WHERE {SCOPE}', *env.trace.args) == 4
        stages = [r['stage'] for r in await conn.fetch(f'SELECT stage FROM memory_steps WHERE {SCOPE}', *env.trace.args)]
    assert 'p2_forget_scope.input' in stages and 'p2_intent.input' not in stages
    assert not env.responses


@pytest.mark.asyncio
@pytest.mark.parametrize('forget_first', [True, False])
async def test_forget_and_content_execution_follows_cross_array_order(env, jev, forget_first):  # noqa: F811
    c = await session(env, 1)
    old = await add(env, c, 'James lives in Paris.')
    request, fact = 'Forget all information about James.', 'James works at Acme.'
    env.responses.append(extraction_response(
        rules=[], contents=[dict(item(fact), order=2 if forget_first else 1)],
        forget_requests=[forget_item(request, entities=['James'], order=1 if forget_first else 2)],
    ))
    jev[0].append('new')
    if forget_first:
        env.responses.append(targets_response(['James lives in Paris.']))
    else:
        env.responses.extend([plan_response(('new', None, fact)), targets_response(['James lives in Paris.', fact])])
    source = request + ' Remember this again: ' + fact if forget_first else fact + ' ' + request
    await env.full_pipeline.ingest(env.trace, 2, [{'role': 'user', 'content': source}])
    assert len(jev[1]) == 1 and len(jev[1][0]['questions']) == 1
    assert (await row(env, old))['status'] == 'deleted'
    usable = await env.retriever.current(env.trace, 2, purpose='answer')
    assert [r['text'] for r in usable] == ([fact] if forget_first else [])
    assert not env.responses


@pytest.mark.asyncio
async def test_bad_forget_scope_skips_only_request_and_continues_content(env, jev):  # noqa: F811
    c = await session(env, 1)
    old = await add(env, c, 'James lives in Paris.')
    env.responses.extend([
        extraction_response(rules=[], contents=[dict(item('Likes tea'), order=2)],
                            forget_requests=[forget_item('Forget James.', entities=['James'])]),
        {'target_refs': ['M999']}, {'target_refs': ['M999']},
        plan_response(('new', None, 'Likes tea')),
    ])
    jev[0].append('new')
    await env.full_pipeline.ingest(env.trace, 2, [{'role': 'user', 'content': 'Forget James. I like tea.'}])
    assert (await row(env, old))['status'] == 'fresh'
    assert {r['text'] for r in await env.store.current_rows(env.trace)} == {'James lives in Paris.', 'Likes tea'}
    assert not env.responses
    async with env.store.pool.acquire() as conn:
        skipped = await conn.fetchval(
            f"SELECT payload FROM memory_steps WHERE {SCOPE} AND stage='p2.direct_skipped'",
            *env.trace.args,
        )
    assert skipped['intent'] == 'forget'


@pytest.mark.asyncio
async def test_bad_forget_group_retries_whole_extraction_before_any_write(env, jev):  # noqa: F811
    response = extraction_response(rules=[], contents=[item('Likes tea')],
                                   forget_requests=[dict(forget_item('Forget James.', order=2), temporal_scope='current')])
    env.responses.extend([response] * 4)
    from contexthub.memory.extractor import ExtractionFailed
    with pytest.raises(ExtractionFailed):
        await env.full_pipeline.ingest(env.trace, 1, [{'role': 'user', 'content': 'I like tea. Forget James.'}])
    assert not jev[1] and not env.embedded and not await env.store.current_rows(env.trace)
