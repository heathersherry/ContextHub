"""Exercise expansion and reuse through the public discovery/graph boundary."""
import json
from collections import Counter
import httpx
import pytest
from contexthub.discovery.adjudication import Adjudication, AdjudicationService, EvidenceSet
from contexthub.discovery.contracts import Decision
from contexthub.discovery.identity_routing import IdentityRoute
from contexthub.discovery.semantic_runner import SemanticRunner, SemanticConfig, decision_context
from contexthub.discovery.semantic_candidates import RetrievalPolicy
from contexthub.discovery.service import DiscoveryConfig, DiscoveryService
from contexthub.discovery.search import SearchService
from contexthub.discovery.stage0 import Stage0Config, Mention
from contexthub.llm.recorded_embedding import EmbeddingConfig, RecordedEmbeddingClient
from contexthub.models.knowledge import canonical_hash
from contexthub.services.artifact_service import ArtifactService
from discovery_helpers import source,response,extract,history,search_policy,call_identity
from s4_helpers import NodeDB,adjudication_config
from knowledge_helpers import policy
from test_identity_routing import ner, ROUTING


async def run(tmp_path, mode='missing', separate_sources=False, monkeypatch=None):
    db=NodeDB();refs=[];raw={'propositions':[]}
    shared=json.dumps([{'role':'user','content':'amount 0; amount 1; amount 2; amount 3; amount 4'}])
    for i in range(5):
        original=shared if not separate_sources else json.dumps([{'role':'user','content':f'amount {i}'}])
        if i==0 or separate_sources:refs.append(await source(db,original))
        prop=response(refs[-1],original)['propositions'][0];prop['text']=f'amount {i}';raw['propositions'].append(prop)
    artifact,sink,_=await extract(db,tuple(refs),[raw])
    ec=EmbeddingConfig(model='fixture',base_url='https://fixture.invalid',dimensions=2,send_dimensions=True,
        max_batch=8,response_archive_directory=str(tmp_path/'embedding'),retry=policy())
    semantic=SemanticConfig(embedding=ec,retrieval=RetrievalPolicy(initial_candidates=2,expansion_factor=2,
        batch_size=2,max_partial_rounds=3),price=None)
    ac=adjudication_config(stage0=Stage0Config(version='names',rules=(),aliases=(),alias_version='empty',
        mention_patterns=(),mention_view='dialogue-content-v2',spacy_ner=ner()))
    cfg=DiscoveryConfig(search=search_policy(strategy='selective',lambda_p1=10),adjudication=ac,
        timeout_seconds=30,identity_routing=ROUTING.model_copy(update={'unknown_policy':'advisory'}),semantic=semantic)
    requests=[];first={}
    def handle(req):
        b=json.loads(req.content)
        if req.url.path.endswith('/embeddings'):
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.,float(i)]} for i in range(len(b['input']))]})
        p=json.loads(b['messages'][0]['content'].split('\nInput:\n',1)[1]);requests.append(p)
        target=p['target']['text'];first.setdefault(target,[c['text'] for c in p['candidates']]);eid=p['evidence'][0]['id'];items=[]
        for c in p['candidates']:
            j=dict(verdict='INDEPENDENT',citation_ids=[eid],steps=[dict(premise_ids=[eid],previous_steps=[],conclusion_evidence_id=eid)],
                negative_basis='explicit_non_use',exclusion_ids=[eid],conflicts=[],missing=[],semantic_basis='synthetic explicit exclusion')
            if mode!='same' and c['text']==first[target][0] and (not separate_sources or len({i for i in range(5) if any(f'amount {i}' in e['text'] for e in p['evidence'])})<5):
                j.update(verdict='UNKNOWN',missing=['rule not present'])
            if mode=='failure' and c['text']==first[target][1]:j['citation_ids']=['E999999']
            if mode=='positive' and target=='amount 0' and c['text']==first[target][1]:
                j.update(verdict='DEPENDS',negative_basis='none',exclusion_ids=[])
            items.append(dict(candidate_number=c['candidate_number'],judgment=j))
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'items':items})}}],
            'usage':{'prompt_tokens':10,'completion_tokens':5}})
    transport=httpx.MockTransport(handle);adj=AdjudicationService(ac,api_key='fixture',recorder=sink,transport=transport)
    emb=RecordedEmbeddingClient(ec,api_key='fixture',transport=transport)
    runner=SemanticRunner(semantic,embedding=emb,adjudicator=adj,recorder=sink,routing=cfg.identity_routing)
    entity_calls=[]
    if mode=='same':
        import contexthub.discovery.semantic_runner as module
        def route(c,t,mentions,routing,generation):
            e=c.evidence[0];m=Mention(text='amount',evidence=e,start=e.start+e.excerpt.index('amount'))
            return [('fixture-pair',m,m,'fixture')],IdentityRoute(upstream_id=c.id,downstream_id=t.id,generation=generation,
                left_mentions=1,right_mentions=1,possible_position_pairs=1,selected_pairs=1,reason='fixture')
        async def same(db,**kwargs):
            entity_calls.append(kwargs)
            return Adjudication(kind='entity',verdict='SAME',action='complete',reason='fixture',
                evidence_sha256=canonical_hash(kwargs['bundle']),traces=(),generation=kwargs['generation'])
        monkeypatch.setattr(module,'route_pair',route);monkeypatch.setattr(adj,'judge',same)
    try:
        result=await DiscoveryService(cfg,search=SearchService(recorder=sink),adjudicator=adj,recorder=sink,
            semantic_runner=runner).discover(db,artifact.reference(),history(tuple(refs)),identity=call_identity(cfg))
        assert await ArtifactService().get(db,result.reference())==result
        return result,requests,sink,db,entity_calls
    finally:await adj.close();await emb.close()


@pytest.mark.asyncio
async def test_same_is_advisory_not_an_expansion_trigger(tmp_path,monkeypatch):
    result,requests,_,_,calls=await run(tmp_path,'same',monkeypatch=monkeypatch)
    assert calls and all(not k['bundle'].at_max_scope for k in calls)
    assert len(result.semantic_selections)==5
    assert all(s.generation==0 and len(s.selected_ids)==2 for s in result.semantic_selections)
    assert len(requests)==10


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['missing','failure','positive'])
async def test_unchanged_evidence_only_sends_new_pairs_preserves_old_results(tmp_path,mode):
    result,requests,sink,db,_=await run(tmp_path,mode)
    pairs=Counter((p['target']['text'],c['text']) for p in requests if p['role']=='normal' for c in p['candidates'])
    assert len(pairs)==20 and set(pairs.values())=={1}
    expanded=[s for s in result.semantic_selections if s.generation==1]
    assert len(expanded)==5 and all(len(s.decision_reuses)==2 for s in expanded)
    assert all(any(r.disposition=='missing_at_max' for r in s.decision_reuses) for s in expanded)
    graph=await ArtifactService().get(db,result.graph)
    assert len(graph.pending)==(10 if mode=='failure' else 5)
    assert len(graph.edges)==(1 if mode=='positive' else 0)
    assert any(d.result.reason=='missing_premise_at_max' for d in result.decisions)
    by_hash={canonical_hash(d):d for d in result.decisions}
    for s in expanded:
        for reuse in s.decision_reuses:
            old=by_hash[reuse.decision_sha256]
            assert old.result.generation==reuse.from_generation==0
    # Reuse is audit metadata, not a fabricated new model attempt or zero-price call.
    assert len([r for r in sink.rows if r.phase=='finished' and r.identity.operation=='judge_dependency_batch'])==len(requests)


@pytest.mark.asyncio
async def test_new_original_evidence_invalidates_prior_decisions(tmp_path):
    result,requests,_,_,_=await run(tmp_path,separate_sources=True)
    assert all(not s.decision_reuses for s in result.semantic_selections)
    pairs=Counter((p['target']['text'],c['text']) for p in requests if p['role']=='normal' for c in p['candidates'])
    assert sum(n==2 for n in pairs.values())==10
    assert any(all(any(f'amount {i}' in e['text'] for e in p['evidence']) for i in range(5)) for p in requests)


@pytest.mark.asyncio
async def test_reuse_context_binds_version_config_and_target_but_not_enumeration(tmp_path):
    from s4_helpers import evidence
    from contexthub.discovery.search import SearchCandidate
    from uuid import uuid4
    db=NodeDB();e=await evidence(db);snapshot=await ArtifactService().get(db,e.source);c=SearchCandidate(id=uuid4(),kind='history_source',proposition_id=None,text='fact',sources=(e.source,),source_versions=(snapshot.source,),evidence=(e,),explicit_provenance=())
    b=EvidenceSet(evidence=(e,),complete=True,at_max_scope=False,issues=())
    def key(bundle=b,target=c,config={'v':1},account='s2-a'):
        return decision_context(account,target,(c,),history((e.source,)),bundle,config,ROUTING)
    assert key()==key(bundle=b.model_copy(update={'at_max_scope':True}))
    changed_snapshot=snapshot.model_copy(update={'source':snapshot.source.model_copy(update={'version':2})})
    changed=e.model_copy(update={'source':changed_snapshot.reference()})
    assert key()!=key(bundle=b.model_copy(update={'evidence':(changed,)}))
    assert key()!=key(target=c.model_copy(update={'text':'changed fact'}))
    assert key()!=key(config={'v':2})
    assert key()!=key(account='other-account')
