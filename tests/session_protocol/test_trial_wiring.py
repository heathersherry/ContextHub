"""Offline regressions for the real trial's JSON-mode and judge identity failures."""
import json
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.request_parameters import RequestParameters
from contexthub.models.knowledge import ContractError
from contexthub.services.session_models import RecordedSessionModels, Extraction
from integrations.memebench.patha.run_gates import FileIdentity
from scripts.patha_preparation import run_session_trial as trial
from test_session_service import config


class Sink:
    def __init__(self): self.rows = []
    async def append(self, row): self.rows.append(row)
    async def records(self, account, call):
        return tuple(r for r in self.rows if r.identity.account_id == account and r.identity.call_id == call)


def parameters():
    return RequestParameters(output_limit_field='max_tokens', reasoning_effort=None,
        response_format='json_object', temperature_supported=True,
        seed=dict(support='unsupported', enabled=False, evidence_reference='offline-fixture'))


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['extract', 'identity', 'dependency', 'generate', 'verify', 'verify_ingest'])
async def test_actual_json_mode_request_is_accepted_without_json_in_input(tmp_path, operation):
    cfg = config(tmp_path)
    cfg.models['base'] = cfg.models['base'].model_copy(update={'request_parameters': parameters()})
    requests = []
    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['response_format'] == {'type': 'json_object'}
        # Reproduce the actual provider's admission rule, not just a prompt substring test.
        if not any('json' in m['content'].lower() for m in body['messages']):
            return httpx.Response(400, json={'error': 'messages must contain json'})
        return httpx.Response(200, json={'model': 'offline', 'usage': {
            'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            'choices': [{'message': {'content': json.dumps({
                'items': [], 'revisit_ids': [], 'routing_complete': True})}}]})
    client = OpenAIChatClient('offline-placeholder', model='offline', temperature=0,
        request_parameters=parameters(), transport=httpx.MockTransport(provider))
    sink = Sink()
    models = RecordedSessionModels(cfg, {'base': client}, None, sink, 'offline', uuid4())
    try:
        result = await models.ask('base', operation, {'memory': []}, Extraction, stage='P1')
        assert result.routing_complete
        assert len(requests) == 1
        assert [r.outcome for r in sink.rows] == ['started', 'success']
        assert sink.rows[-1].usage.total_tokens == 15
    finally:
        await client.close()


def judge_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(trial, 'R', tmp_path)
    cfg = config(tmp_path).models['base'].model_copy(update={'request_parameters': parameters()})
    path = tmp_path / 'official_fixture.py'
    path.write_text('class LLMJudge:\n'
        '    def u_check(self, **kwargs):\n'
        '        return self._call("Return JSON: " + kwargs["agent_answer"])\n')
    source = FileIdentity(path=str(path), sha256=sha256(path.read_bytes()).hexdigest())
    auth = {'protocol': 'session-evidence-v2'}
    for name, obj in [('authorization.json', auth), ('judge-configuration.json', cfg.model_dump(mode='json')),
                      ('official-source.json', {'path': source.path, 'sha256': source.sha256})]:
        (tmp_path / name).write_text(json.dumps(obj))
    return cfg, source, auth


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_default', [False, True])
async def test_judge_normalizes_identity_then_executes_recorded_judgment(tmp_path, monkeypatch, explicit_default):
    cfg, source, auth = judge_inputs(tmp_path, monkeypatch)
    if explicit_default:
        (tmp_path / 'official-source.json').write_text(source.model_dump_json())
    requests = []
    def provider(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'model': 'offline', 'usage': {
            'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            'choices': [{'message': {'content': '{"correct": true}'}}]})
    monkeypatch.setattr(trial, 'Transport', lambda budget: httpx.MockTransport(provider))
    sink = Sink()
    judge = trial.SessionJudge(cfg, source, 'judge', 'offline-placeholder', sink, None, auth)
    label = SimpleNamespace(task_type='Cas', before_entity_values={'owner': 'Alice'},
        record_id='fixture', record_sha256='a'*64, before_question_id='question')
    try:
        verdict, rows = await judge.judge(sidecar=label, phase='before', question='Who?',
            answer_row={'answer': 'Alice', 'error': None, 'state_sha256': 'b'*64},
            run_id=uuid4(), replicate_id=0, profile='adaptive')
        assert verdict.correct is True
        assert verdict.official_code_sha256 == source.sha256
        assert len(requests) == 1 and len(rows) == 2
    finally:
        await judge.close()


@pytest.mark.parametrize('change', ['path', 'hash', 'contents', 'model'])
def test_judge_still_rejects_changed_identity_before_client_creation(tmp_path, monkeypatch, change):
    cfg, source, auth = judge_inputs(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail('client constructed before identity validation')
    monkeypatch.setattr(trial, 'OpenAIChatClient', forbidden)
    if change == 'path': source = source.model_copy(update={'path': str(tmp_path / 'other.py')})
    elif change == 'hash': source = source.model_copy(update={'sha256': '0'*64})
    elif change == 'contents': (tmp_path / 'official_fixture.py').write_text('changed')
    else: cfg = cfg.model_copy(update={'model': 'other'})
    with pytest.raises(ContractError, match='mismatch'):
        trial.SessionJudge(cfg, source, 'judge', 'offline-placeholder', Sink(), None, auth)
