"""Official protocol, real shared engine/PostgreSQL, deterministic model I/O."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

pytest.importorskip('deriveval', reason='add the pinned DerivEval src directory to PYTHONPATH')

from memory_bench.models import MemoryEvent, RetrievalQuery, Question, GroundTruth, AnswerResult
from memory_bench.benchmarks.derivemem.prompts import (
    build_answer_prompt, build_event_attributed_answer_prompt,
)
from memory_bench.scorers.derivemem import DeriveMemMetricScorer
from integrations.memderive import backend as adapter
from .test_application import engine_for
from .test_runtime_factory import config_dict
from .helpers import (item, rule_item, extraction_response, direct_response,
                      bindings_response, trigger_response, repair_response)


async def open_backend(env, tmp_path, monkeypatch):
    async def create(*args, **kwargs):
        return engine_for(env)
    monkeypatch.setattr(adapter, 'create_memory_engine', create)
    for name in ('DATABASE_URL', 'CHAT_API_KEY', 'EMBEDDING_API_KEY'):
        monkeypatch.setenv('MEMDERIVE_' + name, 'fixture')
    backend = adapter.ContextHubBackend(memory_config=config_dict(tmp_path),
        state_root=tmp_path, source_sha256=adapter.implementation_hash())
    await backend.initialize(None)
    await backend.create_namespace('world', fresh=True)
    return backend


def event(n, text):
    return MemoryEvent(id=f'e{n}', role='user', content=text,
                       timestamp=f'2026-10-0{n}T09:00:00', session_id=f's{n}',
                       metadata={'gold': 'MUST_NOT_ENTER_MEMORY', 'typed_action': 'SECRET_ACTION'})


async def memory_rows(env, scope):
    async with env.store.pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(
            f'SELECT * FROM memories WHERE {adapter.SCOPE} ORDER BY memory_id,version', *scope.args)]


@pytest.mark.asyncio
async def test_event_provenance_updates_read_only_and_namespace_isolation(env, tmp_path, monkeypatch):
    backend = await open_backend(env, tmp_path, monkeypatch)
    env.responses.extend([
        extraction_response(rules=[], contents=[item('Owner Wang')]),
        {'action': 'new', 'target_ref': None},
        extraction_response(rules=[], contents=[item('Owner Li')]),
        direct_response('update', 'Owner Wang'),
    ])
    await backend.ingest([event(1, 'Owner Wang')], 'world')
    await backend.finalize('world')
    first = await backend.retrieve(RetrievalQuery('Who is the owner?', 'q1', '2026-10-01T09:00'), 'world', 15)
    assert first[0].text == 'Owner Wang' and first[0].metadata['event_ids'] == ['e1']
    # Q3 must be able to continue writing after finalize and retrieval.
    await backend.ingest([event(2, 'Owner Li')], 'world')
    await backend.finalize('world')
    scope = backend.namespaces['world'].scope
    before = await memory_rows(env, scope)
    query = RetrievalQuery('Who is the owner?', 'q2', '2026-10-02T09:00', {'gold': 'MUST_NOT_ENTER_MEMORY'})
    second, repeated = await asyncio.gather(
        backend.retrieve(query, 'world', 1), backend.retrieve(query, 'world', 1))
    assert second == repeated and second[0].text == 'Owner Li'
    assert second[0].metadata['event_ids'] == ['e2']
    assert await memory_rows(env, scope) == before
    await backend.create_namespace('another-world', fresh=True)
    assert await backend.retrieve(query, 'another-world', 15) == []
    assert await env.store.latest_session(env.trace) == 0
    assert 'MUST_NOT_ENTER_MEMORY' not in json.dumps(env.prompts)
    assert 'SECRET_ACTION' not in json.dumps(env.prompts)
    with pytest.raises(ValueError, match='predates'):
        await backend.retrieve(replace(query, query_time='2026-10-01'), 'world', 15)
    with pytest.raises(ValueError, match='duplicate'):
        await backend.ingest([event(2, 'Owner Li')], 'world')
    with pytest.raises(ValueError, match='exactly one official session'):
        await backend.ingest([event(3, 'three'), event(4, 'four')], 'world')
    with pytest.raises(ValueError, match='per_session'):
        await backend.ingest([event(3, 'three')], 'world', {'ingest_granularity': 'per_event'})
    with pytest.raises(ValueError, match='resume/restore'):
        await backend.create_namespace('world', fresh=False)
    with pytest.raises(ValueError, match='chronological'):
        await backend.ingest([replace(event(1, 'old'), id='old', session_id='old')], 'world')
    await backend.close()
    ledger = list(tmp_path.glob('*.attempts.jsonl'))
    assert ledger and any(path.stat().st_size for path in ledger)
    assert not env.responses


@pytest.mark.asyncio
async def test_derived_sources_and_official_answer_citation_scoring(env, tmp_path, monkeypatch):
    backend = await open_backend(env, tmp_path, monkeypatch)
    rule = 'If owner changes, then recipient follows owner'
    env.responses.extend([
        extraction_response(rules=[dict(rule_item(rule), candidate_ref='R1')],
                            contents=[dict(item('Owner Wang'), source_message_nos=[2]), dict(item('Recipient Wang'), source_message_nos=[3])]),
        {'action': 'new', 'target_ref': None}, {'action': 'new', 'target_ref': None},
        {'action': 'new', 'target_ref': None},
        bindings_response(['Owner Wang'], ['Recipient Wang']), trigger_response([], []),
        extraction_response(rules=[], contents=[item('Owner Li')]),
        direct_response('update', 'Owner Wang'),
        trigger_response(['Recipient Wang'], ['C1']),
        repair_response('Recipient Li', [rule, 'Owner Li']),
    ])
    await backend.ingest([event(1, rule), replace(event(2, 'Owner Wang'), session_id='s1'),
                          replace(event(3, 'Recipient Wang'), session_id='s1')], 'world')
    await backend.ingest([event(4, 'Owner Li')], 'world')
    evidence = await backend.retrieve(RetrievalQuery('Who is the recipient?', 'q', '2026-10-05'), 'world', 15)
    derived = next(e for e in evidence if e.text == 'Recipient Li')
    assert derived.metadata['origin'] == 'derived'
    assert derived.metadata['event_ids'] == ['e1', 'e4']
    gold = dict(answerability='answerable', gold_answer='Li', witness=['e1', 'e4'],
                alternative_witnesses=[], valid_support=['e1', 'e4'],
                gold_evidence=['e1', 'e4'], invalid_evidence=['old'], distractors=['noise'])
    question = Question('q', 'Who is the recipient?', 0,
                        ground_truth=GroundTruth(metadata=gold), metadata={'query_time': '2026-10-05'})
    context = {'world_id': 'tripops_w1', '_question': replace(question, ground_truth=None)}
    # Same official v9 text, including multi-event witness rendering.
    assert build_answer_prompt(question.text, evidence, context) == build_event_attributed_answer_prompt(question.text, evidence, context)
    assert 'event_ids=[e1, e4]' in build_answer_prompt(question.text, evidence, context)
    async def score(ids, metric):
        raw = json.dumps({'answerability': 'answerable', 'answer': 'Li', 'witness_ids': ids})
        result = await DeriveMemMetricScorer(metric).score(
            question, AnswerResult(raw_response=raw, answer=raw), evidence, {}, None)
        return result.value
    assert await score(['e1', 'e4'], 'witnessed_accuracy') == 1
    assert await score(['old'], 'witnessed_accuracy') == 0
    assert await score(['old'], 'stale_belief_rate') == 1
    # Empty witnesses retain the official v2 meaning; no gold-evidence fallback.
    question = replace(question, ground_truth=GroundTruth(metadata={**gold, 'witness': [], 'valid_support': []}))
    assert await score([], 'witness_recall') == 1
    await backend.close()


@pytest.mark.asyncio
async def test_failed_ingestion_cannot_be_read_or_retried(env, tmp_path, monkeypatch):
    backend = await open_backend(env, tmp_path, monkeypatch)
    env.responses.extend([503, 503])
    with pytest.raises(Exception):
        await backend.ingest([event(1, 'Owner Wang')], 'world')
    assert not backend.namespaces['world'].events
    with pytest.raises(ValueError, match='ingestion failed'):
        await backend.retrieve(RetrievalQuery('owner', 'q'), 'world', 15)
    with pytest.raises(ValueError, match='ingestion failed'):
        await backend.ingest([event(1, 'Owner Wang')], 'world')
    await backend.close()


@pytest.mark.asyncio
async def test_changed_source_fails_before_opening_database(tmp_path):
    backend = adapter.ContextHubBackend(memory_config=config_dict(tmp_path),
        state_root=tmp_path, source_sha256='different')
    with pytest.raises(ValueError, match='source changed'):
        await backend.initialize(None)


@pytest.mark.asyncio
async def test_real_factory_configuration_and_resource_cleanup(env, tmp_path, monkeypatch):
    import os
    import httpx
    from contexthub.memory.factory import create_memory_engine

    def respond(request):
        if request.url.path.endswith('/embeddings'):
            inputs = json.loads(request.content)['input']
            payload = {'model': 'fixture-embedding-v1',
                       'data': [{'index': i, 'embedding': [1., 0., 0.]} for i in range(len(inputs))],
                       'usage': {'prompt_tokens': 1, 'total_tokens': 1}}
        else:
            payload = {'model': 'fixture-chat',
                       'choices': [{'message': {'content': '{"rules":[],"contents":[],"forget_requests":[]}'}}],
                       'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        return httpx.Response(200, json=payload)

    async def create(dsn, config, **kwargs):
        return await create_memory_engine(dsn, config, transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(adapter, 'create_memory_engine', create)
    monkeypatch.setenv('MEMDERIVE_DATABASE_URL', os.environ['MEMORY_DATABASE_URL'])
    monkeypatch.setenv('MEMDERIVE_CHAT_API_KEY', 'fixture')
    monkeypatch.setenv('MEMDERIVE_EMBEDDING_API_KEY', 'fixture')
    backend = adapter.ContextHubBackend(memory_config=config_dict(tmp_path), state_root=tmp_path,
        source_sha256=adapter.implementation_hash())
    await backend.initialize(None)
    engine = backend.engine
    assert engine.pipeline.retriever.indexer.client.config.response_archive_directory == str(tmp_path / backend.run_id / 'responses')
    await backend.create_namespace('factory', fresh=True)
    await backend.ingest([event(1, 'Hello')], 'factory')
    assert await backend.retrieve(RetrievalQuery('anything', 'q'), 'factory', 15) == []
    await backend.close()
    assert engine.store.pool._closed and engine.store.lease_pool._closed
    assert engine.pipeline.extractor.client._client.is_closed


@pytest.mark.asyncio
async def test_session_sources_exclude_neighbors_and_merge_duplicates(env, tmp_path, monkeypatch):
    backend = await open_backend(env, tmp_path, monkeypatch)
    env.responses.extend([
        extraction_response(rules=[], contents=[dict(item('Owner Wang'), source_message_nos=[2])]),
        {'action': 'new', 'target_ref': None},
        extraction_response(rules=[], contents=[item('Owner Wang')]),
        direct_response('duplicate', 'Owner Wang'),
    ])
    await backend.ingest([event(1, 'Weather noise'), replace(event(2, 'Owner Wang'), session_id='s1'),
                          replace(event(3, 'Calendar noise'), session_id='s1')], 'world')
    query = RetrievalQuery('Who is the owner?', 'q', '2026-10-06')
    first = await backend.retrieve(query, 'world', 15)
    assert first[0].metadata['event_ids'] == ['e2']
    await backend.ingest([event(4, 'Owner Wang'), replace(event(5, 'Unrelated email'), session_id='s4')], 'world')
    second = await backend.retrieve(query, 'world', 15)
    assert second[0].metadata['event_ids'] == ['e2', 'e4']
    assert await backend.engine.store.latest_session(backend.namespaces['world'].scope) == 2
    assert len(backend.namespaces['world'].events) == 5
    with pytest.raises(ValueError, match='predates'):
        await backend.retrieve(replace(query, query_time='2026-10-04'), 'world', 15)
    await backend.close()
