"""Offline policy checks for the approved two-episode development configuration."""
from pathlib import Path
import json
import httpx
import test_knowledge_adjudication as single_tests
import pytest
from contexthub.knowledge_config import KnowledgeConfig
from test_adjudication_evidence_ids import selected
from test_knowledge_adjudication import run
import test_batch_adjudication as batch_tests

ROOT = Path(__file__).resolve().parents[1] / 'artifacts/patha-conditional-review-20260927-v27'

@pytest.fixture(params=['selective', 'full-history'])
def cfg(request, tmp_path):
    cfg = KnowledgeConfig.model_validate_json((ROOT / f'{request.param}.json').read_text()).discovery.adjudication
    # Only archive destination changes; model calls are intercepted by MockTransport.
    return cfg.model_copy(update={role: getattr(cfg, role).model_copy(update={
        'request_parameters': getattr(cfg, role).request_parameters.model_copy(update={
            'response_archive_directory': str(tmp_path / role)})}) for role in ('weak', 'strong')})

@pytest.mark.asyncio
@pytest.mark.parametrize('kind,verdict,basis', [
    ('dependency', 'DEPENDS', 'none'),
    ('dependency', 'INDEPENDENT', 'explicit_non_use'),
    ('entity', 'SAME', 'none'),
    ('entity', 'DIFFERENT', 'identity_exclusion'),
])
async def test_agreement_with_different_valid_citations_skips_strong(cfg, kind, verdict, basis, monkeypatch):
    def respond(p, n):
        eid = p['evidence'][0]['id']
        return selected(p, verdict, negative_basis=basis,
                        exclusion_ids=[eid] if basis != 'none' else [])
    def transport(requests, responder):
        def handle(request):
            body = json.loads(request.content)
            prompt = body['messages'][-1]['content']
            payload = json.loads(prompt.split('Input:\n', 1)[1])
            requests.append(payload)
            return httpx.Response(200, json={'model': 'fixture-model', 'choices': [
                {'message': {'content': json.dumps(responder(payload, len(requests)))}}],
                'usage': {'prompt_tokens': 11, 'completion_tokens': 5, 'total_tokens': 16}})
        return httpx.MockTransport(handle)
    monkeypatch.setattr(single_tests, 'judge_transport', transport)
    out, requests, _ = await run(respond, cfg=cfg, kind=kind)
    assert out.action == 'complete' and out.reason == 'dual_weak', out.model_dump()
    assert [p['role'] for p in requests] == ['normal', 'challenge']
    assert requests[0]['evidence'][0]['id'] != requests[1]['evidence'][0]['id']
    assert 'prior_judgments' not in requests[1]

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['ok', 'disagree', 'unsupported', 'bad', 'missing'])
async def test_batch_policy_preserves_disputes_and_failures(cfg, monkeypatch, mode):
    monkeypatch.setattr(batch_tests, 'adjudication_config', lambda: cfg)
    out, requests, _ = await batch_tests.run(mode)
    roles = [p['role'] for p in requests]
    if mode in ('disagree', 'unsupported'):
        assert roles == ['normal', 'challenge', 'strong']
    else:
        assert roles == ['normal', 'challenge']
    results = list(out.values())
    assert any(r.reason == 'dual_weak' for r in results)
    if mode == 'disagree': assert any(r.reason == 'mandatory_strong' for r in results)
    if mode in ('unsupported', 'bad', 'missing'): assert any(r.action == 'pending' for r in results)
