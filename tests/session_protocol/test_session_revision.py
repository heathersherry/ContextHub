"""Synthetic request/state regressions; no model semantics or database claims."""
import copy
import pytest
from test_session_service import Models, service, source
from contexthub.services.session_state import Edge
from contexthub.services.session_knowledge_service import SessionKnowledgeService


@pytest.mark.asyncio
async def test_stopped_target_survives_two_unrelated_sessions_and_new_service(tmp_path):
    m = Models()
    m.reject_repair = True
    s = service(tmp_path, m)
    await s.ingest_session(source(0, 'My lead is Alice.', 'My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    start = len(m.calls)
    s = SessionKnowledgeService(s.config, s.store, m)
    await s.ingest_session(source(1, 'ordinary chat'))
    await s.ingest_session(source(2, 'ordinary chat'))
    assert [c[1] for c in m.calls[start:]] == ['extract', 'extract']


@pytest.mark.asyncio
async def test_direct_verifier_does_not_receive_unrelated_history(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hobby is Chess.'))
    start = len(s.models.calls)
    await s.ingest_session(source(1, 'My lead is Alice.'))
    calls = [c[2] for c in s.models.calls[start:] if c[1] == 'verify_ingest']
    assert calls
    assert all('sources' not in p and 'evidence' not in p for p in calls)


class BasisModels(Models):
    accept = True
    async def ask(self, role, operation, payload, schema, *, stage):
        if operation == 'generate':
            self.calls.append((role, operation, copy.deepcopy(payload)))
            b = next(n for n in payload['memory'] if n['text'].startswith('My shuttle is '))
            return schema.model_validate(dict(outcome='unchanged', text=payload['target']['text'],
                kind=payload['target']['kind'], evidence_ids=payload['target']['evidence'], read_ids=[b['id']]))
        if operation == 'dependency':
            self.calls.append((role, operation, copy.deepcopy(payload)))
            b = next(n for n in payload['candidates'] if n['text'].startswith('My shuttle is '))
            return schema.model_validate(dict(positives=[dict(upstream_id=b['id'],
                memory_ids=[b['id'], payload['target']['id']], evidence_ids=[],
                derivation='synthetic prerequisite', review=False)] if self.accept else [],
                identity_questions=[], missing=None, complete=True, independent=not self.accept))
        return await super().ask(role, operation, payload, schema, stage=stage)


@pytest.mark.asyncio
async def test_unchanged_rechecks_new_read_basis(tmp_path):
    m = BasisModels()
    s = service(tmp_path, m, profile='full/strong-only')
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.'))
    hotel, shuttle = s.state.nodes
    s._apply(searched=(hotel.id,), complete=True)
    s.attempts.get(hotel.id, {}).update(trigger="synthetic-relevant-basis-change")
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    assert any(e.upstream == shuttle.id and e.downstream == hotel.id for e in s.state.edges)
    assert s._nodes()[hotel.id].version == hotel.version
    s._apply(edits=())


@pytest.mark.asyncio
async def test_unchanged_negative_read_does_not_create_required_parent(tmp_path):
    m = BasisModels()
    m.accept = False
    s = service(tmp_path, m, profile='full/strong-only')
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.'))
    hotel, shuttle = s.state.nodes
    s.attempts.get(hotel.id, {}).update(trigger="synthetic-relevant-basis-change")
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    assert not s.state.edges
    assert s._nodes()[hotel.id].status == 'fresh'
    assert not s.read_ids[hotel.id]
    assert s._nodes()[hotel.id].basis_revision == 1


@pytest.mark.asyncio
async def test_unchanged_basis_revision_propagates_and_next_change_is_seen(tmp_path):
    from contexthub.services.session_state import Edit, descendants
    m = BasisModels()
    s = service(tmp_path, m, profile='full/strong-only')
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.'))
    hotel, shuttle = s.state.nodes
    s.attempts.get(hotel.id, {}).update(trigger="synthetic-relevant-basis-change")
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    assert hotel.id in descendants({shuttle.id}, s.state.edges)
    s._apply(edits=(Edit(id=shuttle.id, expected_version=shuttle.version,
        text='My shuttle is cancelled.', kind=shuttle.kind, evidence=shuttle.evidence),), complete=False)
    assert s._nodes()[hotel.id].status == 'stale'
    assert any(a['operation'] == 'basis_revision' and not a['content_changed'] for a in s.audits)


@pytest.mark.asyncio
async def test_generation_and_verifier_share_frozen_local_package(tmp_path):
    s = service(tmp_path, profile='local/base-only')
    await s.ingest_session(source(0, 'My hobby is Chess.'))
    await s.ingest_session(source(1, 'My lead is Alice.', 'My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    generate = next(c[2] for c in s.models.calls if c[1] == 'generate')
    verify = next(c[2] for c in s.models.calls if c[1] == 'verify' and 'proposal' in c[2])
    for key in ('sources', 'evidence', 'memory', 'graph'):
        assert generate[key] == verify[key]
    assert [s['ordinal'] for s in generate['sources']] == [1]


@pytest.mark.asyncio
async def test_p1_memory_only_and_known_parent_not_top_k_capped(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.', 'My hobby is Chess.'))
    target, parent, other = s.state.nodes
    s.config = s.config.model_copy(update={'initial_candidates': 1, 'explicit_rules': False})
    s._apply(searched=(target.id,), edges=(Edge(upstream=parent.id, downstream=target.id,
        status='confirmed', evidence=parent.evidence),))
    s._rank = lambda target: [other.id, parent.id]
    await s._dependencies(target.id)
    request = [c[2] for c in s.models.calls if c[1] == 'dependency'][-1]
    assert not request['sources'] and not request['evidence']
    assert parent.id in {n['id'] for n in request['candidates']}
    assert 'graph' not in request and 'memory' not in request


class RawModels(Models):
    requests = 0
    async def ask(self, role, operation, payload, schema, *, stage):
        if operation == 'dependency':
            self.calls.append((role, operation, copy.deepcopy(payload)))
            self.requests += 1
            if not payload['sources']:
                return schema.model_validate(dict(positives=[], identity_questions=[], missing='office condition',
                    complete=False, raw_requests=[dict(reason='missing location condition', query='office')]))
            assert any('office' in m['content'] for s in payload['sources'] for m in s['messages'])
            return schema.model_validate(dict(positives=[], identity_questions=[], missing=None,
                complete=True, independent=True))
        return await super().ask(role, operation, payload, schema, stage=stage)


@pytest.mark.asyncio
async def test_raw_lookup_finds_unextracted_fact_and_preserves_whole_turns(tmp_path):
    m = RawModels()
    s = service(tmp_path, m)
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    # Immutable prefix material need not already have an extracted memory.
    s.checkpoint = s.checkpoint.model_copy(update={'sources': (*s.checkpoint.sources,
        source(1, 'The office is elsewhere only on Fridays.', 'It is closed otherwise.'))})
    await s._dependencies(s.state.nodes[0].id)
    calls = [c[2] for c in m.calls if c[1] == 'dependency']
    assert len(calls) == 2
    assert not calls[0]['sources']
    assert len(calls[1]['sources']) == 1 and len(calls[1]['sources'][0]['messages']) == 2
    assert calls[1]['evidence']  # Program-bound full-message IDs, not fabricated quotes.
    assert not s.state.pending_targets


@pytest.mark.asyncio
async def test_raw_future_source_rejected_and_repeated_request_bounded(tmp_path):
    class Bad(RawModels):
        future = True
        async def ask(self, role, operation, payload, schema, *, stage):
            if operation == 'dependency':
                self.calls.append((role, operation, copy.deepcopy(payload)))
                return schema.model_validate(dict(positives=[], identity_questions=[], missing='unknown', complete=False,
                    raw_requests=[dict(reason='missing referent', **(
                        dict(source_ids=[source(9, 'x').source_id]) if self.future
                        else dict(memory_ids=[payload['target']['id']])))]))
            return await super().ask(role, operation, payload, schema, stage=stage)
    m = Bad()
    s = service(tmp_path, m)
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    with pytest.raises(ValueError, match='outside_prefix'):
        await s._dependencies(s.state.nodes[0].id)
    m.future = False
    start = len(m.calls)
    await s._dependencies(s.state.nodes[0].id)
    assert len(m.calls) - start == 2
    assert s.state.nodes[0].id in s.state.pending_targets


@pytest.mark.asyncio
async def test_empty_selection_is_not_independence(tmp_path):
    class Empty(Models):
        async def ask(self, role, operation, payload, schema, *, stage):
            if operation == 'dependency':
                return schema.model_validate(dict(positives=[], identity_questions=[], missing=None, complete=True))
            return await super().ask(role, operation, payload, schema, stage=stage)
    s = service(tmp_path, Empty())
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    await s._dependencies(s.state.nodes[0].id)
    assert s.state.pending_targets


@pytest.mark.asyncio
async def test_new_unlinked_gap_material_wakes_once(tmp_path):
    class Gap(Models):
        async def ask(self, role, operation, payload, schema, *, stage):
            result = await super().ask(role, operation, payload, schema, stage=stage)
            if operation == 'generate':
                result = result.model_copy(update={'missing': 'office location', 'search_clues': ('office location',)})
            if operation == 'extract' and any('office' in ''.join(u['text'] for u in m['evidence_units']) for m in payload['session']['messages']):
                result = result.model_copy(update={'items': ()})
            return result
    m = Gap()
    m.reject_repair = True
    s = service(tmp_path, m)
    await s.ingest_session(source(0, 'My lead is Alice.', 'My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    start = len(m.calls)
    await s.ingest_session(source(1, 'The office location is Central.'))
    assert len([c for c in m.calls[start:] if c[1] == 'generate']) == 3
    assert any(a['operation'] == 'wake' for a in s.audits)
    start = len(m.calls)
    await s.ingest_session(source(2, 'The office location is Central.'))
    assert [c[1] for c in m.calls[start:]] == ['extract']


@pytest.mark.asyncio
async def test_upstream_validity_alone_reopens_waiting_target(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.'))
    target, parent = s.state.nodes
    s.state = s.state.model_copy(update={'nodes': tuple(n.model_copy(update={'status': 'unresolved'}) for n in s.state.nodes),
        'edges': (Edge(upstream=parent.id, downstream=target.id, status='confirmed', evidence=parent.evidence),)})
    await s._repair(target.id)
    assert s.attempts[target.id]['reason'] == 'upstream_unresolved'
    assert s._already_stopped(target.id)
    s.state = s.state.model_copy(update={'nodes': (s._nodes()[target.id], parent)})
    assert not s._already_stopped(target.id)


@pytest.mark.asyncio
async def test_verifier_cannot_cite_unsent_evidence(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hobby is Chess.'))
    hidden = s.state.nodes[0].evidence
    await s.ingest_session(source(1, 'My lead is Alice.'))
    original = s.models.ask
    async def bad(role, operation, payload, schema, *, stage):
        result = await original(role, operation, payload, schema, stage=stage)
        return result.model_copy(update={'evidence_ids': hidden})
    s.models.ask = bad
    target = s.state.nodes[1]
    with pytest.raises(ValueError, match='evidence_invalid'):
        await s._verify(target.model_dump(mode='json'), target.evidence)


@pytest.mark.asyncio
async def test_answer_and_judge_mismatch_rejected_before_calls(tmp_path):
    from types import SimpleNamespace as NS
    from integrations.memebench.patha.session_factory import run_session_comparison
    from integrations.memebench.patha.session_evaluation import judge_session_comparison
    profiles = ('local/base-only', 'full/strong-only', 'adaptive')
    components = {}
    for i, profile in enumerate(profiles):
        s = service(tmp_path, profile=profile)
        s.store.account_id = f'maintenance-{i}'
        components[profile] = NS(service=s, answerer=NS(account_id=f'answer-{i}', config={'model': str(i)}))
    with pytest.raises(ValueError, match='same_controls'):
        await run_session_comparison(components, None, None, question_directory=None)
    assert all(not c.service.models.calls for c in components.values())
    judges = {p: NS(config={'model': str(i)}, source={'hash': 'fixed'}) for i, p in enumerate(profiles)}
    with pytest.raises(ValueError, match='judge_configuration_mismatch'):
        await judge_session_comparison({'profiles': dict.fromkeys(profiles)}, judges=judges,
            boundary=None, question_directory=None, labels=[], run_id=None, replicate_id=0)


@pytest.mark.asyncio
async def test_new_stale_basis_blocks_fresh_and_preserves_old_edge(tmp_path):
    m = BasisModels()
    s = service(tmp_path, m, profile='full/strong-only')
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.', 'My office is Central.'))
    hotel, shuttle, office = s.state.nodes
    s._apply(searched=(hotel.id,), edges=(Edge(upstream=office.id, downstream=hotel.id,
        status='confirmed', evidence=office.evidence),))
    s.state = s.state.model_copy(update={'nodes': tuple(n.model_copy(update={'status': 'unresolved'})
        if n.id == shuttle.id else n for n in s.state.nodes)})
    s.attempts.get(hotel.id, {}).update(trigger="synthetic-relevant-basis-change")
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    assert s._nodes()[hotel.id].status != 'fresh'
    assert s.attempts[hotel.id]['reason'] == 'upstream_unresolved'
    assert any(e.upstream == shuttle.id for e in s.state.edges)
    # Removed office edge is still present in the immutable delta audit chain.
    assert any(any(e['upstream'] == office.id for e in a['delta']['edges'])
               for a in s.audits if a['operation'] == 'delta')


@pytest.mark.asyncio
async def test_pending_new_basis_does_not_erase_old_edges(tmp_path):
    class Pending(BasisModels):
        async def ask(self, role, operation, payload, schema, *, stage):
            result = await super().ask(role, operation, payload, schema, stage=stage)
            if operation == 'dependency':
                result = result.model_copy(update={'complete': False, 'missing': 'office condition'})
            return result
    s = service(tmp_path, Pending(), profile='full/strong-only')
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.', 'My office is Central.'))
    hotel, shuttle, office = s.state.nodes
    s._apply(searched=(hotel.id,), edges=(Edge(upstream=office.id, downstream=hotel.id,
        status='confirmed', evidence=office.evidence),))
    s.attempts.get(hotel.id, {}).update(trigger="synthetic-relevant-basis-change")
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    assert s._nodes()[hotel.id].status != 'fresh'
    assert {e.upstream for e in s.state.edges} == {office.id, shuttle.id}
    assert hotel.id in s.state.pending_targets


@pytest.mark.asyncio
async def test_verifier_version_drift_rejected(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    original = s.models.ask
    async def drift(role, operation, payload, schema, *, stage):
        result = await original(role, operation, payload, schema, stage=stage)
        if operation == 'verify':
            s.state = s.state.model_copy(update={'revision': s.state.revision + 1})
        return result
    s.models.ask = drift
    target = s.state.nodes[0]
    with pytest.raises(ValueError, match='basis_drift'):
        await s._verify(target.model_dump(mode='json'), target.evidence)


@pytest.mark.asyncio
async def test_changed_source_version_reverifies_same_text(tmp_path):
    from contexthub.services.session_state import Edit
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.'))
    hotel, shuttle = s.state.nodes
    s._apply(searched=(hotel.id,), edges=(Edge(upstream=shuttle.id, downstream=hotel.id,
        status='confirmed', evidence=shuttle.evidence),))
    s.before = s.state
    s._apply(edits=(Edit(id=shuttle.id, expected_version=shuttle.version,
        text=shuttle.text, kind=shuttle.kind, evidence=shuttle.evidence),))
    s._apply(searched=(shuttle.id,))
    verdict = await s._verify(s._nodes()[shuttle.id].model_dump(mode='json'), shuttle.evidence)
    s._proof(shuttle.id, verdict)
    start = len(s.models.calls)
    s.attempts.get(hotel.id, {}).update(trigger="synthetic-relevant-basis-change")
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    assert any(c[1] == 'dependency' for c in s.models.calls[start:])
    assert any(c[1] == 'verify' for c in s.models.calls[start:])
    assert s._nodes()[hotel.id].version == hotel.version
    assert s._nodes()[hotel.id].basis_revision > hotel.basis_revision


@pytest.mark.asyncio
async def test_full_package_exposes_citable_unextracted_messages(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    s.checkpoint = s.checkpoint.model_copy(update={'sources': (*s.checkpoint.sources,
        source(1, 'Unextracted office condition.'))})
    target = s.state.nodes[0]
    payload = s._payload(target.model_dump(mode='json'), target.evidence, full=True)
    assert any(e['quote'] == 'Unextracted office condition.' for e in payload['evidence'])
    assert [r['ordinal'] for r in payload['sources']] == [0, 1]


@pytest.mark.asyncio
async def test_raw_source_limit_keeps_scope_pending_without_silent_truncation(tmp_path):
    s = service(tmp_path, RawModels())
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    s.config = s.config.model_copy(update={'raw_source_limit': 1})
    s.checkpoint = s.checkpoint.model_copy(update={'sources': (*s.checkpoint.sources,
        source(1, 'office condition one'), source(2, 'office condition two'))})
    await s._dependencies(s.state.nodes[0].id)
    assert s.state.pending_targets
    audit = next(a for a in s.audits if a['operation'] == 'raw_limit')
    assert len(audit['remaining_sources']) == 2


@pytest.mark.asyncio
async def test_incomplete_scope_has_declared_batched_recheck(tmp_path):
    from contexthub.services.session_state import Edit
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    target = s.state.nodes[0]
    s.config = s.config.model_copy(update={'scope_recheck_new_materials': 2})
    s._apply(searched=(target.id,), complete=False)
    s._stop(target.id, 'search_incomplete')
    def edit(text):
        return Edit(id='unlinked', expected_version=0, text=text, kind='assertion', evidence=target.evidence)
    assert not s._wake(source(1, 'movie comedy'), [edit('movie comedy')])
    assert s._wake(source(2, 'garden roses'), [edit('garden roses')]) == {target.id}
    assert any(a.get('reason') == 'bounded_incomplete_scope_review' for a in s.audits)


@pytest.mark.asyncio
async def test_failed_dependency_has_persistent_distinct_stop_reason(tmp_path):
    import httpx
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    target = s.state.nodes[0]
    async def fail(*args, **kwargs):
        raise httpx.ConnectError('synthetic offline failure')
    s.models.ask = fail
    with pytest.raises(httpx.ConnectError):
        await s._discover_targets((target.id,))
    assert s.attempts[target.id]['reason'] == 'network_error'
    assert s.attempts[target.id]['actions'] == ['dependency']


@pytest.mark.asyncio
async def test_m3_can_verify_same_updated_proposal_after_failed_m2(tmp_path):
    class Upgrade(Models):
        enabled = False
        async def ask(self, role, operation, payload, schema, *, stage):
            if self.enabled and operation == 'generate':
                self.calls.append((role, operation, copy.deepcopy(payload)))
                if 'previous_session' in payload and not any(s['ordinal'] == 1 for s in payload['sources']):
                    return schema.model_validate(dict(outcome='unresolved', text=None, kind='assertion', evidence_ids=[], read_ids=[]))
                return schema.model_validate(dict(outcome='updated', text='My hotel is Beta.', kind='assertion',
                    evidence_ids=payload['target']['evidence'], read_ids=[]))
            result = await super().ask(role, operation, payload, schema, stage=stage)
            if self.enabled and operation == 'verify':
                strong_called = any(c[0] == 'strong' and c[1] == 'generate' for c in self.calls)
                result = result.model_copy(update={'supported': strong_called})
            return result
    m = Upgrade()
    s = service(tmp_path, m)
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    s.checkpoint = s.checkpoint.model_copy(update={'sources': (*s.checkpoint.sources, source(1, 'Other material.'))})
    m.enabled = True
    hotel = s.state.nodes[0]
    s.attempts.get(hotel.id, {}).update(trigger="synthetic-relevant-basis-change")
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    calls = [c[2] for c in m.calls if c[1] == 'generate']
    assert len(calls) == 3 and calls[1] == calls[2]
    assert s._nodes()[hotel.id].text == 'My hotel is Beta.'
    assert s._nodes()[hotel.id].version == hotel.version + 1
    assert s._nodes()[hotel.id].status == 'fresh'


@pytest.mark.asyncio
async def test_unchanged_new_parent_change_triggers_next_ingestion(tmp_path):
    m = BasisModels()
    s = service(tmp_path, m, profile='full/strong-only')
    await s.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.'))
    hotel, shuttle = s.state.nodes
    s.attempts[hotel.id]['trigger'] = 'synthetic-relevant-basis-change'
    s._apply(invalidated=(hotel.id,))
    await s._repair(hotel.id)
    await s._save('complete')
    start = len(m.calls)
    s = SessionKnowledgeService(s.config, s.store, m)
    await s.ingest_session(source(1, 'My shuttle is cancelled.'))
    assert any(c[1] == 'generate' and c[2]['target']['id'] == hotel.id for c in m.calls[start:])
    assert any(e.upstream == shuttle.id and e.downstream == hotel.id for e in s.state.edges)
    proof = next(a['proof'] for a in reversed(s.audits) if a['operation'] == 'release' and a['proof']['target'] == hotel.id)
    assert [shuttle.id, 2] in proof['upstream_versions']


@pytest.mark.asyncio
async def test_unrelated_new_fact_does_not_retry_failed_target(tmp_path):
    m = Models()
    m.reject_repair = True
    s = service(tmp_path, m)
    await s.ingest_session(source(0, 'My lead is Alice.', 'My recipient is Alice.',
        'If my lead changes, my recipient will be Bob.'))
    target = next(n.id for n in s.state.nodes if n.text == 'My recipient is Alice.')
    start = len(m.calls)
    await s.ingest_session(source(1, 'My movie is Comedy.'))
    await s.ingest_session(source(2, 'My garden is Roses.'))
    assert not any(c[1] in ('generate', 'dependency', 'verify') and c[2].get('target', {}).get('id') == target
                   for c in m.calls[start:])


def test_new_protocol_configuration_rejects_legacy_identity(tmp_path):
    import json
    from pathlib import Path
    from test_session_service import config
    from contexthub.services.session_models import SessionConfiguration
    example = json.loads(Path('integrations/memebench/patha/config/session-evidence-v2.offline-example.json').read_text())
    assert SessionConfiguration.model_validate(example).protocol == 'session-evidence-v2'
    legacy = config(tmp_path).model_dump(mode='json')
    legacy['protocol'] = 'session-incremental-v1'
    with pytest.raises(ValueError):
        SessionConfiguration.model_validate(legacy)


@pytest.mark.asyncio
async def test_rq1_raw_lookup_state_isolated_but_policy_shared(tmp_path):
    s = service(tmp_path, RawModels())
    await s.ingest_session(source(0, 'My hotel is Alpha.'))
    s.config = s.config.model_copy(update={'paired_rq1': True})
    s.checkpoint = s.checkpoint.model_copy(update={'sources': (*s.checkpoint.sources,
        source(1, 'The office is elsewhere only on Fridays.'))})
    start = len(s.models.calls)
    await s._discover_targets((s.state.nodes[0].id,))
    requests = [c[2] for c in s.models.calls[start:] if c[1] == 'dependency']
    assert len(requests) == 4
    assert not requests[0]['sources'] and not requests[2]['sources']
    assert requests[0] == requests[2] and requests[1] == requests[3]
    pair = next(a for a in reversed(s.audits) if a['operation'] == 'paired_rq1')
    assert pair['evidence_policy'] == 'memory-first-demand-raw-v2'
    assert set(pair['evidence_registries']) == {'selective', 'full-history'}
