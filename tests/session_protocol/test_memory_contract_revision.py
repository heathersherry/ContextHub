"""Frozen trial evidence + synthetic decisions, offline only; no semantic accuracy claim."""
import copy
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.models.knowledge import ContractError
from contexthub.services.session_memory import SessionInput
from contexthub.services.session_models import Dependencies, IngestVerdict, PROMPTS, RecordedSessionModels
from test_session_service import Models, service, source
from test_trial_wiring import Sink, parameters

FIXTURE = json.loads((Path(__file__).parent/'fixtures/trial-memory-contract.json').read_text())


class TrialExtraction(Models):
    async def ask(self, role, op, payload, schema, **kw):
        if op == 'extract' and self.ordinal == 0:
            adapted = copy.deepcopy(FIXTURE['extraction'])
            # Synthetic test input in the current extraction wire; archived response stays unchanged.
            for item in adapted['items']:
                item['read_memory_ids'] = item.pop('read_ids')
            return schema.model_validate(adapted)
        return await super().ask(role, op, payload, schema, **kw)


async def trial_service(tmp_path):
    s = service(tmp_path, TrialExtraction())
    s.store.stream_id = UUID(FIXTURE['stream_id'])
    await s.ingest_session(SessionInput.model_validate(FIXTURE['source']))
    target = next(n for n in s.state.nodes if 'new apartment' in n.text)
    return s, target


@pytest.mark.asyncio
async def test_archived_wrong_ids_rejected_without_conversion_or_retry(tmp_path):
    s, target = await trial_service(tmp_path)
    s.attempts[target.id]["trigger_sources"] = [s.checkpoint.sources[0].source_id]
    requests, sink = [], Sink()
    def respond(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=FIXTURE['original_http_response'])
    client = OpenAIChatClient('offline-placeholder', model='offline', request_parameters=parameters(),
                              transport=httpx.MockTransport(respond))
    s.models = RecordedSessionModels(s.config, {'p1_weak':client}, None, sink, 'offline', uuid4())
    try:
        with pytest.raises(ContractError, match='raw_memory_outside_candidates'):
            await s._dependencies(target.id)
    finally:
        await client.close()
    assert len(requests) == 1
    inp = json.loads(requests[0]['messages'][0]['content'])['input']
    assert not inp['sources'] and not inp['evidence']
    assert inp['allowed_evidence_ids'] == []
    assert set(inp['allowed_memory_ids']) == {inp['target']['id'], *(n['id'] for n in inp['candidates'])}
    assert not inp['new_source_locators']
    assert all('evidence' not in n for n in [inp['target'], *inp['candidates']])
    # Original request leaked IDs even though it supplied no original text.
    old = json.loads(FIXTURE['original_request']['messages'][0]['content'])['input']
    assert old['target']['evidence'] and not old['sources']
    assert all(e.id not in json.dumps(inp) for e in s.evidence.values())
    assert target.evidence  # Provenance still exists in program state.


@pytest.mark.asyncio
async def test_memory_lookup_exposes_only_actually_provided_evidence(tmp_path):
    s, target = await trial_service(tmp_path)
    await s.ingest_session(source(1, 'My hobby is Chess.'))
    extra = next(n for n in s.state.nodes if n.text == 'My hobby is Chess.')
    calls = []
    async def lookup(role, op, p, schema, **kw):
        calls.append(copy.deepcopy(p))
        if len(calls) == 1:
            assert 'evidence' not in p['target']
            return schema.model_validate(dict(positives=[], identity_questions=[], complete=False,
                independent=False, missing='Need original wording for identity',
                raw_requests=[dict(reason='Resolve identity from original', memory_ids=[p['target']['id']])]))
        assert len(p['sources']) == 1 and p['sources'][0]['ordinal'] == 0
        allowed = {e['id'] for e in p['evidence']}
        assert set(p['allowed_evidence_ids']) == allowed
        assert set(p['allowed_memory_ids']) == {p['target']['id'], *(n['id'] for n in p['candidates'])}
        assert set(target.evidence) <= allowed
        assert not set(extra.evidence) & allowed
        assert all(set(n.get('evidence', [])) <= allowed for n in [p['target'], *p['candidates']])
        return schema.model_validate(dict(positives=[], identity_questions=[], complete=True,
                                         independent=True, missing=None))
    s.models.ask = lookup
    await s._dependencies(target.id)
    assert len(calls) == 2
    assert any(a['operation'] == 'raw_lookup' for a in s.audits)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['memory_ids', 'evidence_ids', 'upstream_id'])
async def test_wrong_namespace_in_positive_is_rejected(tmp_path, field):
    s, target = await trial_service(tmp_path)
    parent = next(n for n in s.state.nodes if n.id != target.id)
    async def wrong(role, op, p, schema, **kw):
        positive = dict(upstream_id=parent.id, memory_ids=[parent.id], evidence_ids=[], derivation='test', review=False)
        positive[field] = target.evidence[0] if field == 'upstream_id' else [target.evidence[0] if field == 'memory_ids' else target.id]
        return schema.model_validate(dict(positives=[positive], identity_questions=[], complete=True, missing=None))
    s.models.ask = wrong
    with pytest.raises(ContractError, match='session_dependency_(references|candidates)_invalid'):
        await s._dependencies(target.id)


@pytest.mark.asyncio
async def test_first_assertion_candidate_is_not_its_own_unresolved_support(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My home is Alpha.'))
    p = next(c[2] for c in s.models.calls if c[1] == 'verify_ingest')
    assert p['target_role'] == 'current_ingestion_candidate'
    assert 'status' not in p['target'] and p['memory'] == []
    assert s.state.nodes[0].status == 'fresh'
    assert not any(c[1] in ('dependency', 'generate', 'verify') for c in s.models.calls)
    audit = next(a for a in s.audits if a['operation'] == 'ingest_verifier')
    assert audit['provenance'][p['target']['id']]['evidence_ids']
    assert audit['raw_fidelity_verified'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['conflict', 'dependency'])
@pytest.mark.parametrize('status', ['stale', 'unresolved'])
async def test_concrete_conflicts_and_prerequisites_still_block_ingest_release(tmp_path, kind, status):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My home is Alpha.', 'My home is Beta.'))
    target, related = s.state.nodes
    # Existing uncertain facts remain explicitly unusable as current support.
    s.state = s.state.model_copy(update={'nodes':(target, related.model_copy(update={'status':status}))})
    async def verdict(role, op, p, schema, **kw):
        assert p['memory'] == [s._memory_fact(s._nodes()[related.id])]
        return schema.model_validate(dict(applicable=True, prerequisites_satisfied=kind!='dependency',
            conflicts_resolved=kind!='conflict', kind_confirmed=True, independent=kind!='dependency',
            memory_ids=[target.id, related.id], dependency_ids=[related.id] if kind=='dependency' else [],
            complete=False, missing='Unresolved conflicting home values' if kind=='conflict' else 'Required home value unresolved'))
    s.models.ask = verdict
    v = await s._verify_ingest(target)
    assert not s._ingest_passed(v)
    assert bool(v.dependency_ids) == (kind == 'dependency')


@pytest.mark.asyncio
async def test_derived_candidate_cannot_become_root_even_with_positive_ingest_verdict(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My recipient is Alice.'))
    target = s.state.nodes[0].model_copy(update={'kind':'derived', 'status':'stale'})
    s.state = s.state.model_copy(update={'nodes':(target,)})
    v = await s._verify_ingest(target)
    assert not s._proof(target.id, v, evidence_ids=target.evidence, verification_scope='candidate-consistency-v2')
    assert s.state.nodes[0].status == 'stale'


def test_archived_external_support_verdict_is_not_silently_accepted():
    for a in FIXTURE['ingest_verdicts']:
        with pytest.raises(ValidationError):
            IngestVerdict.model_validate(a['verdict'])
    assert 'supported' not in IngestVerdict.model_fields
    assert 'No original chat' in PROMPTS['verify_ingest']
    assert 'Do NOT require' in PROMPTS['verify_ingest']
    assert 'root of the dependency graph' in PROMPTS['dependency']
    assert 'supported' not in Dependencies.model_fields


@pytest.mark.asyncio
async def test_ingest_real_prerequisite_routes_to_p1_not_independent_release(tmp_path):
    class Recipient(Models):
        async def ask(self, role, op, p, schema, **kw):
            result = await super().ask(role, op, p, schema, **kw)
            if op == 'verify_ingest' and p['target']['text'] == 'My recipient is Alice.':
                lead = next(n for n in p['memory'] if n['text'] == 'My lead is Alice.')
                return result.model_copy(update=dict(independent=False,
                    memory_ids=(p['target']['id'], lead['id']), dependency_ids=(lead['id'],)))
            return result
    s = service(tmp_path, Recipient())
    await s.ingest_session(source(0, 'My lead is Alice.', 'My recipient is Alice.'))
    lead, recipient = s.state.nodes
    assert any(e.upstream == lead.id and e.downstream == recipient.id for e in s.state.edges)
    assert not any(a['operation'] == 'release' and a['proof']['target'] == recipient.id
                   and a['verification_scope'] == 'candidate-consistency-v2' for a in s.audits)
    assert any(c[1] == 'dependency' for c in s.models.calls)


@pytest.mark.asyncio
async def test_memory_only_independent_root_needs_no_raw_lookup(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My home is Alpha.'))
    start = len(s.models.calls)
    await s._dependencies(s.state.nodes[0].id)
    calls = s.models.calls[start:]
    assert len(calls) == 1 and calls[0][1] == 'dependency'
    assert calls[0][2]['candidates'] == [] and calls[0][2]['evidence'] == []
    assert not s.state.pending_targets


def test_p2_and_deferred_risk_methods_unchanged_from_trial():
    import ast
    root = Path(__file__).resolve().parents[2]
    path = root/'src/contexthub/services/session_knowledge_service.py'
    cls = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)
               and n.name == 'SessionKnowledgeService')
    methods = {n.name:ast.dump(n, include_attributes=False) for n in cls.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, original in FIXTURE['unchanged_method_sources'].items():
        assert methods[name] == ast.dump(ast.parse(original).body[0], include_attributes=False), name
    assert all(PROMPTS[k] == v for k, v in FIXTURE['unchanged_p2_prompts'].items())


@pytest.mark.asyncio
async def test_unsent_source_locator_is_not_accepted_even_if_in_prefix(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My home is Alpha.'))
    async def guessed(role, op, p, schema, **kw):
        return schema.model_validate(dict(positives=[], identity_questions=[], complete=False,
            missing='identity', raw_requests=[dict(reason='identity', source_ids=[s.checkpoint.sources[0].source_id])]))
    s.models.ask = guessed
    with pytest.raises(ContractError, match='raw_source_not_supplied'):
        await s._dependencies(s.state.nodes[0].id)
