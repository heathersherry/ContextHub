"""Lossless IDs, request scope and transactional failure regressions; no network."""
import asyncio
import copy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.services.session_memory import (
    SessionInput, Checkpoint, bind_span, extraction_evidence_view,
)
from contexthub.services.session_models import Extraction, RecordedSessionModels, MemoryProposal
from contexthub.services.session_knowledge_service import SessionKnowledgeService
from contexthub.llm.chat_client import OpenAIChatClient
from test_session_service import Models, config, service, source
from test_trial_wiring import Sink, parameters

FIXTURE = Path(__file__).parent / 'fixtures/trial-extraction.json'


def test_partition_preserves_tags_case_real_and_literal_newlines_and_duplicates():
    text = '<Label>Aa\r\nBB</Label>\\n' + ('Repeat\n' * 3) + 'x' * 1100 + '\u2028Z\n'
    src = source(0, text, text, '')
    view, registry = extraction_evidence_view(src)
    assert len(registry) > 6
    for i, message in enumerate(view['messages']):
        assert ''.join(u['text'] for u in message['evidence_units']) == src.messages[i].content
        assert 'content' not in message  # No second normalized or duplicate copy of raw text.
        for u in message['evidence_units']:
            e = registry[u['id']]
            assert (e.message, e.source_id, e.source_version, e.content_sha256) == (
                i, src.source_id, src.source_version, src.content_sha256)
            assert u['text'] == src.messages[i].content[e.start:e.end] == e.quote
    repeats = [e for e in registry.values() if e.quote == 'Repeat\n']
    assert len(repeats) == 6 and len({e.id for e in repeats}) == 6
    cp = Checkpoint(stream_id=uuid4(), account_id='offline', configuration_sha256='a'*64,
        revision=0, status='processing', sources=(src,), evidence=tuple(registry.values()))
    assert Checkpoint.model_validate_json(cp.model_dump_json()) == cp


@pytest.mark.parametrize('field,value', [('start', 1), ('end', 3), ('quote', 'lower'),
                                        ('source_version', 2), ('content_sha256', '0'*64)])
def test_checkpoint_rejects_tampered_evidence_location(field, value):
    src = source(0, 'Repeat\nRepeat\n')
    evidence = bind_span(src, 0, 7, 14).model_copy(update={field: value})
    with pytest.raises(ValidationError):
        Checkpoint(stream_id=uuid4(), account_id='offline', configuration_sha256='a'*64,
            revision=0, status='failed', sources=(src,), evidence=(evidence,))


def test_old_quote_wire_is_removed_not_accepted_as_fallback():
    raw = json.loads(FIXTURE.read_text())['original_http_response']['choices'][0]['message']['content']
    with pytest.raises(ValidationError): Extraction.model_validate_json(raw)
    fields = MemoryProposal.model_json_schema()['properties']
    assert 'evidence_ids' in fields and 'citations' not in fields


class InvalidLast(Models):
    async def ask(self, role, operation, payload, schema, **kw):
        result = await super().ask(role, operation, payload, schema, **kw)
        if operation == 'extract':
            data = result.model_dump(mode='json')
            data['items'][-1]['evidence_ids'] = ['E999999']
            return schema.model_validate(data)
        return result


@pytest.mark.asyncio
async def test_invalid_last_id_commits_none_of_batch_and_preserves_response(tmp_path):
    s = service(tmp_path, InvalidLast())
    with pytest.raises(ContractError, match='evidence_not_supplied') as caught:
        await s.ingest_session(source(0, 'My lead is Alice.', 'My hobby is Chess.'))
    cp = await s.store.load()
    assert cp.status == 'failed' and cp.revision == 1 and len(cp.sources) == 1
    assert not cp.state.nodes and not cp.evidence and not cp.read_ids and not cp.attempts
    assert not s.state.nodes and not s.read_ids and not s.evidence and not s._busy
    assert [call[1] for call in s.models.calls] == ['extract']
    response = next(a for a in cp.audits if a['operation'] == 'model_response')['response']
    assert response['items'][0]['evidence_ids'] != ['E999999']
    assert response['items'][1]['evidence_ids'] == ['E999999']
    failure = next(a for a in cp.audits if a['operation'] == 'failed')
    assert failure['error']['code'] == caught.value.code
    assert failure['error']['message'] == str(caught.value)
    assert cp.previous_sha256 == s.store.repository.rows[0]['sha256']
    with pytest.raises(ContractError, match='not_drained'): await s.snapshot()
    with pytest.raises(ContractError, match='previous_incomplete'):
        await s.ingest_session(source(1, 'ordinary chat'))


@pytest.mark.asyncio
async def test_multispan_selection_does_not_override_missing_prerequisite(tmp_path):
    class Unsupported(Models):
        async def ask(self, role, operation, payload, schema, **kw):
            if operation == 'extract':
                ids = payload['allowed_evidence_ids']
                return schema.model_validate(dict(items=[dict(action='add',target_id=None,
                    text='An unsupported assertion.',kind='assertion',evidence_ids=[ids[0],ids[-1]],read_memory_ids=[])],
                    revisit_ids=[],routing_complete=True))
            result = await super().ask(role, operation, payload, schema, **kw)
            if operation == 'verify': return result.model_copy(update={'supported': False})
            if operation == 'verify_ingest': return result.model_copy(update={
                'prerequisites_satisfied': False, 'complete': False, 'missing': 'Required location is unknown'})
            return result
    s = service(tmp_path, Unsupported());s.models.reject_repair = True
    await s.ingest_session(source(0, '<tag>One\nTwo</tag>', 'Different context.'))
    cp = await s.store.load()
    assert cp.state.nodes[0].status != 'fresh'
    refs = [e for e in cp.evidence if e.id in cp.state.nodes[0].evidence]
    assert {e.message for e in refs} == {0, 1}
    assert len(refs) == 2
    assert any(a['operation']=='ingest_verifier' for a in cp.audits)


@pytest.mark.asyncio
async def test_id_present_only_in_old_inventory_is_not_request_evidence(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My lead is Alice.'))
    prior = await s.store.load();old_evidence = prior.state.nodes[0].evidence[0]
    original = s.models.ask
    async def ask(role, operation, payload, schema, **kw):
        result = await original(role, operation, payload, schema, **kw)
        if operation == 'extract':
            assert old_evidence not in payload['allowed_evidence_ids']
            item = result.items[0].model_copy(update={'evidence_ids': (old_evidence,)})
            return result.model_copy(update={'items': (item,)})
        return result
    s.models.ask = ask
    with pytest.raises(ContractError, match='not_supplied'):
        await s.ingest_session(source(1, 'My hobby is Chess.'))
    failed = await s.store.load()
    assert failed.status == 'failed' and failed.state == prior.state
    assert failed.evidence == prior.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['verify', 'embed', 'cancel'])
async def test_mid_session_failure_restores_complete_state_and_original_error(tmp_path, failure_point):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My lead is Alice.'))
    prior = await s.store.load()
    original = s.models.ask;count = 0
    error = asyncio.CancelledError() if failure_point == 'cancel' else ContractError('injected_mid_session')
    async def ask(role, operation, payload, schema, **kw):
        nonlocal count
        if operation == 'verify_ingest':
            count += 1
            if count == 2 and failure_point != 'embed': raise error
        return await original(role, operation, payload, schema, **kw)
    async def embed(texts):raise error
    s.models.ask = ask
    if failure_point == 'embed':s.models.embed = embed
    with pytest.raises(type(error)) as caught:
        await s.ingest_session(source(1, 'My lead is Bob.', 'My hobby is Chess.'))
    assert caught.value is error
    failed = await s.store.load()
    assert failed.status == 'failed' and not s._busy
    assert len(failed.sources) == 2
    for field in ['state','evidence','vectors','history','read_ids','attempts','gaps','source_watches']:
        assert getattr(failed, field) == getattr(prior, field)
    assert s.state == failed.state
    assert next(a for a in failed.audits if a['operation']=='failed')['error']['type']==type(error).__name__


@pytest.mark.asyncio
async def test_failure_persistence_outage_does_not_mask_original_error(tmp_path):
    s = service(tmp_path, InvalidLast());original = s.store.save
    async def save(cp, expected):
        if cp.status == 'failed': raise OSError('offline storage outage')
        await original(cp, expected)
    s.store.save = save
    with pytest.raises(ContractError, match='evidence_not_supplied') as caught:
        await s.ingest_session(source(0, 'My lead is Alice.'))
    assert s.failure['checkpoint_save_error']['type'] == 'OSError'
    assert 'not persisted' in caught.value.__notes__[0]
    assert not s._busy  # No promise that an unavailable store can persist a final state.


@pytest.mark.asyncio
@pytest.mark.parametrize('response_mode', ['original', 'ids', 'invalid_last_id'])
async def test_archived_real_response_offline_replay(tmp_path, response_mode):
    fixture = json.loads(FIXTURE.read_text());src = SessionInput.model_validate(fixture['source'])
    raw_body = copy.deepcopy(fixture['original_http_response'])
    if response_mode != 'original':
        adapted = copy.deepcopy(fixture['adapted_response'])
        # Explicit test-only migration of this previously adapted fixture; no production fallback.
        for item in adapted['items']:
            item['read_memory_ids'] = item.pop('read_ids', [])
        if response_mode == 'invalid_last_id':adapted['items'][-1]['evidence_ids']=['E999999']
        raw_body['choices'][0]['message']['content']=json.dumps(adapted,ensure_ascii=False)
    requests=[]
    def provider(request):
        payload=json.loads(request.content);requests.append(payload)
        wire=json.loads(payload['messages'][0]['content'])
        for i,m in enumerate(wire['input']['session']['messages']):
            assert ''.join(u['text'] for u in m['evidence_units']) == src.messages[i].content
        return httpx.Response(200,json=raw_body)
    s=service(tmp_path);cfg=s.config
    params=parameters().model_copy(update={'response_archive_directory':str(tmp_path/'responses')})
    cfg.models['extraction']=cfg.models['extraction'].model_copy(update={'request_parameters':params})
    client=OpenAIChatClient('offline-placeholder',model='offline',temperature=0,
        request_parameters=params,transport=httpx.MockTransport(provider))
    sink=Sink();port=RecordedSessionModels(cfg,{'extraction':client},None,sink,s.store.account_id,s.store.stream_id)
    original_ask=s.models.ask
    async def ask(role,operation,payload,schema,**kw):
        s.models.last_response = None
        if operation=='extract':
            try:return await port.ask(role,operation,payload,schema,**kw)
            finally:
                s.models.call_ids=list(port.call_ids)
                s.models.last_response=port.last_response
        # Synthetic verifier only; never present this as semantic model evaluation.
        return await original_ask(role,operation,payload,schema,**kw)
    s.models.ask=ask
    try:
        if response_mode=='ids':
            await s.ingest_session(src)
            cp=await s.store.load();assert len(cp.state.nodes)==6
            target=cp.state.nodes[-1]
            assert {e.message for e in cp.evidence if e.id in target.evidence}=={10}
        else:
            with pytest.raises(ValidationError if response_mode=='original' else ContractError):
                await s.ingest_session(src)
            cp=await s.store.load()
            assert cp.status=='failed' and not cp.state.nodes and not cp.read_ids
        assert len(requests)==1 and len(sink.rows)==2
        saved=json.loads(next((tmp_path/'responses').glob('*.json')).read_text())
        assert json.loads(saved['response_text'])==raw_body
        assert str(port.call_ids[0]) in cp.model_dump_json()
        audit = next(a for a in cp.audits if a['operation']=='model_response' and a['model_operation']=='extract')
        assert audit['raw_response']['text'] == raw_body['choices'][0]['message']['content']
    finally:await client.close()


@pytest.mark.asyncio
async def test_whole_batch_validated_before_delete_verifier(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My lead is Alice.'))
    prior = await s.store.load()
    broken = InvalidLast();s.models = broken
    with pytest.raises(ContractError, match='not_supplied'):
        await s.ingest_session(source(1, 'Forget My lead.', 'My hobby is Chess.'))
    assert [call[1] for call in broken.calls] == ['extract']
    failed = await s.store.load()
    assert failed.state == prior.state and failed.status == 'failed'


@pytest.mark.asyncio
async def test_selected_repeat_resolves_to_its_exact_occurrence(tmp_path):
    class SelectSecond(Models):
        async def ask(self, role, operation, payload, schema, **kw):
            if operation == 'extract':
                selected = payload['session']['messages'][1]['evidence_units'][1]['id']
                return schema.model_validate(dict(items=[dict(action='add', target_id=None,
                    text='Repeat', kind='assertion', evidence_ids=[selected],read_memory_ids=[])],
                    revisit_ids=[], routing_complete=True))
            return await super().ask(role, operation, payload, schema, **kw)
    s = service(tmp_path, SelectSecond())
    await s.ingest_session(source(0, 'Repeat\nRepeat\n', 'Repeat\nRepeat\n'))
    cp = await s.store.load();node = cp.state.nodes[0]
    refs = [e for e in cp.evidence if e.id in node.evidence]
    assert len(refs)==1
    assert (refs[0].message, refs[0].start, refs[0].end, refs[0].quote)==(1,7,14,'Repeat\n')
