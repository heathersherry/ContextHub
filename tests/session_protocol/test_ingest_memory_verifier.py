"""Memory-only ingest wire and release gates; synthetic semantics, no network."""
import copy
import json
from pathlib import Path

import pytest

from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.services.session_models import PROMPTS
from contexthub.services.session_state import Edge, Node
from test_session_service import Models, service, source


def assert_memory_only(payload):
    forbidden = {'sources', 'session', 'messages', 'quote', 'evidence', 'evidence_ids', 'source_id', 'source_version', 'content_sha256'}
    def visit(x):
        if isinstance(x, dict):
            assert not forbidden & x.keys()
            for value in x.values(): visit(value)
        elif isinstance(x, list):
            for value in x: visit(value)
    visit(payload)


@pytest.mark.asyncio
async def test_program_retrieves_required_and_ranked_memories_not_whole_inventory(tmp_path):
    s = service(tmp_path)
    await s.ingest_session(source(0, 'My destination is Alpha.'))
    target = s.state.nodes[0]
    specs = [('parent', 'Transport approved.'), ('read', 'Budget approved.'),
             ('rule', 'If destination is Alpha, a permit is required.'),
             ('conflict', 'My destination is Beta.'), ('vector', 'Travel location is allowed.')]
    specs += [(f'filler{i}', f'Unrelated hobby{i}.') for i in range(20)]
    additions = tuple(Node(id=i, version=1, text=t, kind='rule' if i=='rule' else 'assertion',
        status='fresh', evidence=target.evidence) for i,t in specs)
    s.state = s.state.model_copy(update={'nodes':(target,*additions),
        'edges':(Edge(upstream='parent', downstream=target.id, status='confirmed', evidence=target.evidence),)})
    s.read_ids[target.id] = ('read',)
    await s._index()
    # Exercise the same deterministic ranking, including the vector-only hit.
    old_rank = s._rank
    ranking = old_rank(target.id)
    assert set(ranking) == {n.id for n in additions}
    s._rank = lambda _: ['vector', *[i for i in ranking if i!='vector']]
    s.config = s.config.model_copy(update={'initial_candidates':1})
    verdict = await s._verify_ingest(target)
    p = s.models.calls[-1][2]
    assert_memory_only(p)
    ids = {n['id'] for n in p['memory']}
    assert ids == {'parent','read','rule','conflict','vector'}
    assert len(ids) < len(s.state.nodes)
    assert p['known_upstream_ids'] == ['parent'] and p['candidate_read_memory_ids'] == ['read']
    audit = s.audits[-1]
    assert audit['raw_fidelity_verified'] is False
    assert audit['provenance'][target.id]['evidence_ids'] == list(target.evidence)
    assert set(verdict.memory_ids) <= ids | {target.id}


@pytest.mark.asyncio
async def test_actual_new_session_payload_keeps_raw_and_quotes_out_and_indexes_first(tmp_path):
    marker = 'UNEXTRACTED-RAW-ONLY-MARKER'
    class ExtractOne(Models):
        async def ask(self, role, op, p, schema, **kw):
            if op == 'extract':
                return schema.model_validate(dict(items=[dict(action='add', target_id=None,
                    text='My destination is Alpha.', kind='assertion', evidence_ids=[p['allowed_evidence_ids'][0]], read_memory_ids=[])],
                    revisit_ids=[], routing_complete=True))
            if op == 'verify_ingest':
                assert self.embedded == ['My destination is Alpha.']
            return await super().ask(role, op, p, schema, **kw)
    s=service(tmp_path,ExtractOne())
    await s.ingest_session(source(0,'My destination is Alpha. '+marker, marker))
    p=next(c[2] for c in s.models.calls if c[1]=='verify_ingest')
    assert_memory_only(p)
    assert marker not in json.dumps(p)
    cp=await s.store.load()
    assert marker in cp.model_dump_json()  # Lossless source and program mapping retained.
    assert cp.state.nodes[0].status=='fresh'
    release=next(a for a in cp.audits if a['operation']=='release')
    assert release['verification_scope']=='candidate-consistency-v2'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'applicable':False}, {'prerequisites_satisfied':False}, {'conflicts_resolved':False}, {'kind_confirmed':False},
    {'independent':False}, {'complete':False}, {'missing':'Necessary location is unknown'},
])
async def test_schema_valid_judgment_alone_does_not_release_ingested_memory(tmp_path,change):
    class Reject(Models):
        async def ask(self,role,op,p,schema,**kw):
            result=await super().ask(role,op,p,schema,**kw)
            return result.model_copy(update=change) if op=='verify_ingest' else result
    s=service(tmp_path,Reject());s.models.reject_repair=True
    await s.ingest_session(source(0,'My destination is Alpha.'))
    cp=await s.store.load()
    assert cp.state.nodes[0].status!='fresh'
    assert not any(a['operation']=='release' and a['verification_scope']=='candidate-consistency-v2' for a in cp.audits)


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[
    {'memory_ids':('invented',)}, {'dependency_ids':('invented',)},
    {'memory_ids':('other',)},
])
async def test_ingest_rejects_unprovided_memory_references(tmp_path,change):
    s=service(tmp_path)
    await s.ingest_session(source(0,'My destination is Alpha.'))
    original=s.models.ask
    async def invalid(role,op,p,schema,**kw):
        result=await original(role,op,p,schema,**kw)
        return result.model_copy(update=change) if op=='verify_ingest' else result
    s.models.ask=invalid
    with pytest.raises(ContractError,match='memory_references_invalid'):
        await s._verify_ingest(s.state.nodes[0])


@pytest.mark.asyncio
async def test_ingest_judgment_drift_cannot_release(tmp_path):
    s=service(tmp_path)
    await s.ingest_session(source(0,'My destination is Alpha.'))
    original=s.models.ask
    async def drift(role,op,p,schema,**kw):
        result=await original(role,op,p,schema,**kw)
        s.state=s.state.model_copy(update={'revision':s.state.revision+1})
        return result
    s.models.ask=drift
    with pytest.raises(ContractError,match='basis_drift'):
        await s._verify_ingest(s.state.nodes[0])


@pytest.mark.asyncio
async def test_existing_parent_is_not_erased_by_ingest_independence(tmp_path):
    s=service(tmp_path)
    await s.ingest_session(source(0,'My lead is Alice.','My hobby is Chess.'))
    target,parent=s.state.nodes
    s._apply(searched=(target.id,),edges=(Edge(upstream=parent.id,downstream=target.id,
        status='confirmed',evidence=parent.evidence),))
    await s._save('unresolved')
    observed=[]
    original=s._discover_targets
    async def observe(targets):
        observed.append(tuple(s.state.edges))
        return await original(targets)
    s._discover_targets=observe
    await s.ingest_session(source(1,'My lead is Bob.'))
    assert any(e.upstream==parent.id and e.downstream==target.id for e in observed[0])


@pytest.mark.asyncio
async def test_delete_ingest_uses_memory_ids_and_no_raw_package(tmp_path):
    s=service(tmp_path)
    await s.ingest_session(source(0,'My lead is Alice.'))
    start=len(s.models.calls)
    await s.ingest_session(source(1,'Forget My lead.'))
    p=next(c[2] for c in s.models.calls[start:] if c[1]=='verify_ingest')
    assert p['action']=='delete' and p['previous']['text']=='My lead is Alice.'
    assert_memory_only(p)
    assert not any(c[1]=='verify' for c in s.models.calls[start:])


def test_prompt_separates_memory_consistency_from_raw_fidelity():
    assert 'no original chat' in PROMPTS['verify_ingest'].lower()
    assert 'Do not claim' in PROMPTS['verify_ingest']
    assert 'memory_ids' in PROMPTS['verify_ingest']
    assert 'Cite original evidence IDs' in PROMPTS['verify']  # P2 still uses original evidence.
