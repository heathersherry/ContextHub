"""Actual trial responses replayed offline; edited successes are explicitly synthetic."""
import copy
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.models.knowledge import ContractError
from contexthub.services.session_memory import SessionInput
from contexthub.services.session_models import Extraction, MemoryProposal, PROMPTS, RecordedSessionModels
from test_session_service import service
from test_trial_wiring import Sink, parameters

FIXTURE = json.loads((Path(__file__).parent/'fixtures/trial-extraction-memory-ids.json').read_text())


def renamed(body):
    """Explicit test-only field rename; values preserved even when invalid."""
    body = copy.deepcopy(body)
    data = json.loads(body['choices'][0]['message']['content'])
    for item in data['items']:
        item['read_memory_ids'] = item.pop('read_ids')
    body['choices'][0]['message']['content'] = json.dumps(data)
    return body


async def attach(s, tmp_path, bodies):
    requests, sink = [], Sink()
    def respond(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= len(bodies), 'No retry of malformed output'
        return httpx.Response(200, json=bodies[len(requests)-1])
    params = parameters().model_copy(update={'response_archive_directory':str(tmp_path/'http')})
    s.config.models['extraction'] = s.config.models['extraction'].model_copy(update={'request_parameters':params})
    client = OpenAIChatClient('offline-placeholder', model='offline', request_parameters=params,
                              transport=httpx.MockTransport(respond))
    port = RecordedSessionModels(s.config, {'extraction':client}, None, sink, s.store.account_id, s.store.stream_id)
    synthetic = s.models.ask
    async def ask(role, op, payload, schema, **kwargs):
        s.models.last_response = None
        if op != 'extract':
            return await synthetic(role, op, payload, schema, **kwargs)
        port.ordinal = s.models.ordinal
        try:
            return await port.ask(role, op, payload, schema, **kwargs)
        finally:
            s.models.call_ids = list(port.call_ids)
            s.models.last_response = port.last_response
    s.models.ask = ask
    return client, requests, sink


def test_schema_and_prompt_separate_required_namespaces():
    fields = MemoryProposal.model_json_schema()
    assert 'read_ids' not in fields['properties']
    assert 'read_memory_ids' in fields['required']
    assert 'allowed_memory_ids' in fields['properties']['read_memory_ids']['description']
    assert 'allowed_evidence_ids' in fields['properties']['evidence_ids']['description']
    assert 'allowed_memory_ids' in PROMPTS['extract']
    item = json.loads(renamed(FIXTURE['responses'][0])['choices'][0]['message']['content'])
    del item['items'][0]['read_memory_ids']
    with pytest.raises(ValidationError): Extraction.model_validate(item)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['original', 'renamed', 'raw_as_memory'])
async def test_first_session_no_old_memories(tmp_path, mode):
    body = copy.deepcopy(FIXTURE['responses'][0]) if mode=='original' else renamed(FIXTURE['responses'][0])
    if mode=='raw_as_memory':
        data = json.loads(body['choices'][0]['message']['content'])
        data['items'][-1]['read_memory_ids'] = data['items'][-1]['evidence_ids'][:1]
        body['choices'][0]['message']['content'] = json.dumps(data)
    s = service(tmp_path)
    client, requests, sink = await attach(s, tmp_path, [body])
    try:
        if mode=='renamed':
            await s.ingest_session(SessionInput.model_validate(FIXTURE['sources'][0]))
            assert len(s.state.nodes)==5
        else:
            with pytest.raises(ValidationError if mode=='original' else ContractError):
                await s.ingest_session(SessionInput.model_validate(FIXTURE['sources'][0]))
            cp = await s.store.load()
            assert cp.status=='failed' and not cp.state.nodes and not cp.read_ids
        wire = json.loads(requests[0]['messages'][0]['content'])
        assert wire['input']['allowed_memory_ids']==[]
        assert wire['input']['allowed_evidence_ids']
        assert len(requests)==1 and len(sink.rows)==2
        (tmp_path/'replay.json').write_text(json.dumps(dict(mode=mode,
            checkpoint=(await s.store.load()).model_dump(mode='json'), failure=s.failure,
            mock_http_requests=len(requests), real_calls=0,
            response_kind='archived unchanged' if mode=='original' else 'explicit test adaptation',
            later_model_responses='synthetic; no semantic accuracy claim'), ensure_ascii=False, indent=2))
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['original', 'renamed_wrong', 'legal_mixed', 'invalid_last', 'memory_as_evidence'])
async def test_second_session_archived_failure_and_batch_atomicity(tmp_path, mode):
    bodies = [renamed(FIXTURE['responses'][0]), copy.deepcopy(FIXTURE['responses'][1])]
    s = service(tmp_path);s.store.stream_id = UUID(FIXTURE['stream_id'])
    client, requests, sink = await attach(s, tmp_path, bodies)
    try:
        await s.ingest_session(SessionInput.model_validate(FIXTURE['sources'][0]))
        prior = await s.store.load();old_ids = {n.id for n in prior.state.nodes}
        assert old_ids == {n['id'] for n in json.loads(FIXTURE['original_second_request']['messages'][0]['content'])['input']['memory']}
        if mode!='original': bodies[1] = renamed(FIXTURE['responses'][1])
        if mode in ('legal_mixed','invalid_last','memory_as_evidence'):
            data = json.loads(bodies[1]['choices'][0]['message']['content'])
            # Explicit synthetic success: two archived assertion texts, real evidence IDs,
            # and one supplied old memory ID. This tests ID binding, not semantic reliance.
            data['items'] = data['items'][:2]
            data['items'][0]['read_memory_ids'] = [sorted(old_ids)[0]]
            if mode=='invalid_last':
                data['items'][-1]['read_memory_ids'] = data['items'][-1]['evidence_ids']
            if mode=='memory_as_evidence':
                data['items'][-1]['evidence_ids'] = [sorted(old_ids)[0]]
            bodies[1]['choices'][0]['message']['content'] = json.dumps(data)
        start = len(s.models.calls)
        if mode=='legal_mixed':
            await s.ingest_session(SessionInput.model_validate(FIXTURE['sources'][1]))
            assert len(s.state.nodes)==7
            checks = [c[2] for c in s.models.calls[start:] if c[1]=='verify_ingest']
            assert checks[0]['candidate_read_memory_ids']==[sorted(old_ids)[0]]
            assert checks[1]['candidate_read_memory_ids']==[]
            for p in checks:
                assert set(p['allowed_memory_ids'])=={p['target']['id'], *(n['id'] for n in p['memory'])}
                assert p['allowed_evidence_ids']==[]
        else:
            with pytest.raises(ValidationError if mode=='original' else ContractError) as caught:
                await s.ingest_session(SessionInput.model_validate(FIXTURE['sources'][1]))
            cp = await s.store.load()
            assert cp.status=='failed' and len(cp.sources)==2
            for field in ['state','evidence','vectors','history','read_ids','attempts','source_watches','gaps']:
                assert getattr(cp,field)==getattr(prior,field)
            assert not s.models.calls[start:]  # No verifier/embed/maintenance after invalid batch.
            failure = next(a for a in cp.audits if a['operation']=='failed')
            assert failure['error']['type']==type(caught.value).__name__
            response = next(a for a in cp.audits if a['operation']=='model_response')
            assert response['raw_response']['text']==bodies[1]['choices'][0]['message']['content']
            if mode!='original':
                assert caught.value.code==('session_extraction_evidence_not_supplied' if mode=='memory_as_evidence' else 'session_extraction_memory_not_supplied')
            with pytest.raises(ContractError, match='not_drained'): await s.snapshot()
        inp = json.loads(requests[1]['messages'][0]['content'])['input']
        assert set(inp['allowed_memory_ids'])==old_ids
        assert not old_ids & set(inp['allowed_evidence_ids'])
        assert all('evidence' not in n for n in inp['memory'])
        assert len(requests)==2 and len(sink.rows)==4
        archives = [json.loads(p.read_text()) for p in (tmp_path/'http').glob('*.json')]
        assert any(json.loads(a['response_text'])==bodies[1] for a in archives)
        (tmp_path/'replay.json').write_text(json.dumps(dict(mode=mode,
            checkpoint=(await s.store.load()).model_dump(mode='json'), failure=s.failure,
            mock_http_requests=len(requests), real_calls=0,
            response_kind='second response archived unchanged' if mode=='original' else 'explicit test adaptation',
            later_model_responses='synthetic; no semantic accuracy claim'), ensure_ascii=False, indent=2))
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['memory_ids', 'dependency_ids'])
async def test_ingest_rejects_raw_id_even_if_program_knows_it(tmp_path, field):
    from test_session_service import source
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My home is Alpha.'))
    target = s.state.nodes[0]
    async def wrong(role, op, p, schema, **kwargs):
        assert p['allowed_memory_ids']==[target.id] and p['allowed_evidence_ids']==[]
        data = dict(applicable=True, prerequisites_satisfied=True, conflicts_resolved=True,
            kind_confirmed=True, independent=False, memory_ids=[target.id], dependency_ids=[],
            complete=True, missing=None)
        data[field] = [target.evidence[0]]
        return schema.model_validate(data)
    s.models.ask = wrong
    with pytest.raises(ContractError, match='session_ingest_memory_references_invalid'):
        await s._verify_ingest(target)


def test_wire_version_changes_configuration_identity(tmp_path, monkeypatch):
    import contexthub.services.session_models as models
    from test_session_service import config
    cfg = config(tmp_path)
    hashes = []
    original = models.canonical_hash
    def capture(value):
        hashes.append(value)
        return original(value)
    monkeypatch.setattr(models, 'canonical_hash', capture)
    fingerprint = cfg.fingerprint()
    payload = hashes[-1]
    assert payload['extraction_wire']=='read-memory-ids-v1'
    assert payload['ingestion_wire']=='allowed-memory-ids-v1'
    assert payload['dependency_wire']=='memory-ids-demand-evidence-v2'
    old = dict(payload);old.pop('extraction_wire');old.pop('ingestion_wire')
    old['dependency_wire']='memory-ids-demand-evidence-v1'
    assert fingerprint != original(old)
