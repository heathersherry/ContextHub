import json
from pathlib import Path
import httpx
import pytest
from contexthub.discovery.service import DiscoveryConfig,DiscoveryService
from contexthub.discovery.search import SearchService
from contexthub.discovery.adjudication import AdjudicationService
from contexthub.discovery.semantic_runner import SemanticConfig,SemanticRunner
from contexthub.discovery.semantic_candidates import RetrievalPolicy
from contexthub.llm.recorded_embedding import EmbeddingConfig,RecordedEmbeddingClient
from contexthub.discovery.stage0 import Stage0Config
from contexthub.services.artifact_service import ArtifactService
from discovery_helpers import source,response,extract,history,search_policy,call_identity
from s4_helpers import NodeDB,adjudication_config
from knowledge_helpers import policy
from test_identity_routing import ner,ROUTING


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy,expected_calls,unexamined,expand',[('selective',24,3,False),('full-history',48,0,False),('selective',60,0,True)])
async def test_public_discovery_uses_semantic_batch_and_seals_audit(tmp_path,strategy,expected_calls,unexamined,expand):
    db=NodeDB();refs=[];raw={'propositions':[]}
    for i in range(12):
        text=f'amount {i}';original=json.dumps([{'role':'user','content':text}])
        ref=await source(db,original);refs.append(ref)
        prop=response(ref,original)['propositions'][0];prop['text']=text;raw['propositions'].append(prop)
    artifact,sink,_=await extract(db,tuple(refs),[raw])
    ec=EmbeddingConfig(model='fixture',base_url='https://fixture.invalid',dimensions=2,send_dimensions=True,
        max_batch=8,response_archive_directory=str(tmp_path/'embedding'),retry=policy())
    semantic=SemanticConfig(embedding=ec,retrieval=RetrievalPolicy(initial_candidates=8,expansion_factor=2,
        batch_size=8,max_partial_rounds=3),price=None)
    ac=adjudication_config(stage0=Stage0Config(version='names',rules=(),aliases=(),alias_version='empty',
        mention_patterns=(),mention_view='dialogue-content-v2',spacy_ner=ner()))
    cfg=DiscoveryConfig(search=search_policy(strategy=strategy,lambda_p1=10),adjudication=ac,
        timeout_seconds=30,identity_routing=ROUTING.model_copy(update={'unknown_policy':'advisory'}),semantic=semantic)
    requests=[]
    def handle(req):
        b=json.loads(req.content)
        if req.url.path.endswith('/embeddings'):
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.,float(i)]} for i in range(len(b['input']))],
                'usage':{'prompt_tokens':10,'total_tokens':10}})
        p=json.loads(b['messages'][0]['content'].split('\nInput:\n',1)[1]);requests.append(p)
        eid=p['evidence'][0]['id']
        j=dict(verdict='INDEPENDENT',citation_ids=[eid],steps=[dict(premise_ids=[eid],previous_steps=[],conclusion_evidence_id=eid)],
            negative_basis='explicit_non_use',exclusion_ids=[eid],conflicts=[],missing=[],semantic_basis='synthetic non-use')
        if expand and len({x['text'] for x in p['evidence']})<12:
            j.update(verdict='UNKNOWN',missing=['applicable rule'])
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'items':[
            dict(candidate_number=c['candidate_number'],judgment=j) for c in p['candidates']]})}}],
            'usage':{'prompt_tokens':10,'completion_tokens':5}})
    transport=httpx.MockTransport(handle)
    adj=AdjudicationService(ac,api_key='fixture',recorder=sink,transport=transport)
    emb=RecordedEmbeddingClient(ec,api_key='fixture',transport=transport)
    runner=SemanticRunner(semantic,embedding=emb,adjudicator=adj,recorder=sink,routing=cfg.identity_routing)
    try:
        result=await DiscoveryService(cfg,search=SearchService(recorder=sink),adjudicator=adj,recorder=sink,
            semantic_runner=runner).discover(db,artifact.reference(),history(tuple(refs)),identity=call_identity(cfg))
        assert len(requests)==expected_calls
        assert result.status=='complete'
        assert len(result.semantic_index.facts)==12 and len(result.semantic_selections)==(24 if expand else 12)
        assert all(len(s.unexamined_ids)==unexamined for s in result.semantic_selections if not expand or s.generation>0)
        assert all(len(p['candidates'])<=8 for p in requests)
        assert all(len({x['text'] for x in p['evidence']})<=(12 if expand or strategy=='full-history' else 9) for p in requests)
        assert await ArtifactService().get(db,result.reference())==result
        assert len({r.identity.call_id for r in sink.rows if r.requested_model=='fixture' and r.phase=='finished' and r.identity.operation=='embed_facts'})==2
    finally: await adj.close();await emb.close()
