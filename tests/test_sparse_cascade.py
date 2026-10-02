"""Public graph tests for sparse selection, targeted review, and actionable gaps."""
import json
import httpx
import pytest
from contexthub.discovery.adjudication import AdjudicationService
from contexthub.discovery.service import DiscoveryConfig, DiscoveryService
from contexthub.discovery.search import SearchService
from contexthub.discovery.semantic_runner import SemanticConfig, SemanticRunner
from contexthub.discovery.semantic_candidates import RetrievalPolicy
from contexthub.discovery.stage0 import Stage0Config
from contexthub.llm.recorded_embedding import EmbeddingConfig, RecordedEmbeddingClient
from contexthub.services.artifact_service import ArtifactService
from discovery_helpers import source, response, extract, history, search_policy, call_identity
from s4_helpers import NodeDB, adjudication_config
from knowledge_helpers import policy
from test_identity_routing import ROUTING, ner


def proposal(p, *, steps=True, question=False):
    c=p['candidates'][0];eid=p['evidence'][0]['id']
    return dict(candidate_number=c['candidate_number'],judgment=dict(verdict='DEPENDS',citation_ids=[eid],
        steps=[dict(premise_ids=[eid],previous_steps=[],conclusion_evidence_id=eid)] if steps else [],
        negative_basis='none',exclusion_ids=[],conflicts=[],missing=[],semantic_basis='fixture derivation'),
        identity_questions=[dict(left_mention=c['mentions'][0],right_mention=p['target']['mentions'][1],
            why_changes_edge='edge requires the two Alice occurrences to denote the same person')] if question else [],review_reason=None)


async def run(tmp_path, mode='none', separate=True, full=False):
    db=NodeDB();refs=[];raw={'propositions':[]};texts=['amount 0','amount 1','amount 2','amount 3','tax threshold']
    if mode.startswith('entity'):texts=['Alice amount 0','Alice amount 1','Alice amount 2','Alice amount 3','tax threshold']
    shared=json.dumps([{'role':'user','content':'; '.join(texts)}])
    for i,t in enumerate(texts):
        original=json.dumps([{'role':'user','content':t}]) if separate else shared
        if separate or i==0:refs.append(await source(db,original))
        prop=response(refs[-1],original)['propositions'][0];prop['text']=t;raw['propositions'].append(prop)
    artifact,sink,_=await extract(db,tuple(refs),[raw])
    ec=EmbeddingConfig(model='fixture',base_url='https://fixture.invalid',dimensions=2,send_dimensions=True,
        max_batch=8,response_archive_directory=str(tmp_path/'embedding'),retry=policy())
    semantic=SemanticConfig(execution='sparse-cascade-v1',embedding=ec,retrieval=RetrievalPolicy(
        initial_candidates=2,expansion_factor=2,batch_size=2,max_partial_rounds=3),price=None)
    ac=adjudication_config(lambda_p1=0,stage0=Stage0Config(version='fixture',rules=(),aliases=(),alias_version='empty',
        mention_patterns=(),mention_view='dialogue-content-v2',spacy_ner=ner()))
    cfg=DiscoveryConfig(search=search_policy(strategy='full-history' if full else 'selective',lambda_p1=0),
        adjudication=ac,timeout_seconds=30,identity_routing=ROUTING.model_copy(update={'unknown_policy':'advisory'}),semantic=semantic)
    requests=[]
    def handle(req):
        b=json.loads(req.content)
        if req.url.path.endswith('/embeddings'):
            vecs=[[0.,1.]] if len(b['input'])==1 and 'tax' in b['input'][0] else [[1.,float(i)] for i in range(len(b['input']))]
            return httpx.Response(200,json={'data':[dict(index=i,embedding=v) for i,v in enumerate(vecs)]})
        p=json.loads(b['messages'][0]['content'].split('\nInput:\n',1)[1]);requests.append(p)
        if p['contract']=='cascade-identity-v1':
            out=dict(verdict='UNKNOWN' if mode=='entity_unknown' else 'DIFFERENT' if mode=='entity_different' else 'SAME',citation_ids=[p['evidence'][0]['id']],reason='fixture source alias',conflicts=[])
        else:
            out=dict(proposals=[],gaps=[],unresolved_candidate_numbers=[])
            target=p['target']['text'];active=target==texts[0] or (mode=='entity_reuse' and target==texts[1])
            strong='prior_proposals' in p
            if mode in ('positive','review','bad_ref','reject','uncertain','resolved_gap') and active:
                if mode=='uncertain':out['unresolved_candidate_numbers']=[p['candidates'][0]['candidate_number']]
                elif not (mode=='reject' and strong):
                    q=proposal(p,steps=mode not in ('review','reject') or strong)
                    if mode=='bad_ref':q['judgment']['citation_ids']=['E999999']
                    out['proposals']=[q]
                    if mode=='resolved_gap' and not strong:
                        q['judgment']['missing']=['tax threshold']
                        out['gaps']=[dict(candidate_number=q['candidate_number'],missing_fact='tax threshold',search_terms=['tax'],anchor_evidence_ids=[p['evidence'][0]['id']])]
            if mode in ('gap','repeated_gap','generic_gap','invalid_gap') and active:
                if mode!='gap' or len(p['candidates'])==2:
                    out['gaps']=[dict(candidate_number=None,missing_fact='tax threshold is missing',
                        search_terms=['more'] if mode=='generic_gap' else ['tax'],
                        anchor_evidence_ids=['E999999'] if mode=='invalid_gap' else [p['evidence'][0]['id']])]
            if mode.startswith('entity') and active:
                out['proposals']=[proposal(p,steps=mode!='entity_review' or strong,question=not strong)]
                if mode=='entity_bad_question':out['proposals'][0]['identity_questions'][0]['left_mention']='M999999'
            if mode=='malformed' and active:return httpx.Response(200,json={'choices':[{'message':{'content':'{'}}]})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(out)}}],
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
    transport=httpx.MockTransport(handle);adj=AdjudicationService(ac,api_key='fixture',recorder=sink,transport=transport)
    emb=RecordedEmbeddingClient(ec,api_key='fixture',transport=transport)
    runner=SemanticRunner(semantic,embedding=emb,adjudicator=adj,recorder=sink,routing=cfg.identity_routing)
    try:
        result=await DiscoveryService(cfg,search=SearchService(recorder=sink),adjudicator=adj,recorder=sink,
            semantic_runner=runner).discover(db,artifact.reference(),history(tuple(refs)),identity=call_identity(cfg))
        assert await ArtifactService().get(db,result.reference())==result
        graph=await ArtifactService().get(db,result.graph)
        return result,graph,requests,sink
    finally:await adj.close();await emb.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('full,size',[(False,2),(True,4)])
async def test_empty_selection_one_call_per_target_not_independence(tmp_path,full,size):
    r,g,requests,sink=await run(tmp_path,full=full)
    assert len(requests)==5 and all(len(p['candidates'])==size for p in requests)
    assert all('prior_proposals' not in p for p in requests)
    assert not g.edges and not g.pending
    assert all(d.result.action=='not_selected' and d.result.verdict=='UNKNOWN' for d in r.decisions)
    assert all(s.cascade and s.cascade.not_selected_ids for s in r.semantic_selections)
    assert len([x for x in sink.rows if x.phase=='finished' and x.identity.operation=='select_dependency_upstreams'])==5


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,extra,edges,pending',[
    ('positive',0,1,False),('review',1,1,False),('reject',1,0,False),
    ('bad_ref',0,0,True),('malformed',0,0,True),('uncertain',0,0,True),('resolved_gap',1,1,False)])
async def test_only_flagged_positive_gets_one_strong_review(tmp_path,mode,extra,edges,pending):
    r,g,requests,_=await run(tmp_path,mode)
    assert len(requests)==5+extra
    assert len(g.edges)==edges and bool(g.pending)==pending
    review=[p for p in requests if 'prior_proposals' in p]
    assert len(review)==extra
    if review:assert len(review[0]['candidates'])==1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,separate,count,status',[
    ('gap',True,6,'new_source_available'),('repeated_gap',True,6,'repeated_gap'),
    ('gap',False,5,'no_new_source_lead'),('generic_gap',True,5,'no_distinctive_search_term'),
    ('invalid_gap',True,5,'invalid_gap_anchor')])
async def test_only_actionable_novel_source_gap_expands(tmp_path,mode,separate,count,status):
    r,g,requests,_=await run(tmp_path,mode,separate=separate)
    assert len(requests)==count
    assert status in [a.status for s in r.semantic_selections for a in s.cascade.gaps]
    if count==6:assert len([p for p in requests if p['target']['text']=='amount 0'])==2
    if mode!='gap' or not separate:assert g.pending


@pytest.mark.asyncio
async def test_entity_called_only_after_positive_proposal(tmp_path):
    r,g,requests,_=await run(tmp_path,'entity',separate=False)
    assert requests[0]['contract']=='sparse-cascade-v1'
    assert requests[1]['contract']=='cascade-identity-v1'
    assert sum(p['contract']=='cascade-identity-v1' for p in requests)==1
    assert len(g.edges)==1
    uses=[u for s in r.semantic_selections for u in s.cascade.identities]
    assert len(uses)==1 and uses[0].record.verdict=='SAME'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,entity_calls,review_calls,edges',[
    ('entity_review',1,1,1),('entity_unknown',2,0,0),
    ('entity_different',1,1,1),('entity_bad_question',0,0,0)])
async def test_identity_assumptions_cannot_disappear_during_review(tmp_path,mode,entity_calls,review_calls,edges):
    r,g,requests,_=await run(tmp_path,mode,separate=False)
    assert sum(p['contract']=='cascade-identity-v1' for p in requests)==entity_calls
    assert sum('prior_proposals' in p for p in requests)==review_calls
    assert len(g.edges)==edges
    if not edges:assert g.pending
    if mode=='entity_different':
        review=next(p for p in requests if 'prior_proposals' in p)
        assert review['identity_results'][0]['verdict']=='DIFFERENT'


@pytest.mark.asyncio
async def test_public_report_reuses_identity_across_targets_without_new_cost(tmp_path):
    r,g,requests,sink=await run(tmp_path,'entity_reuse',separate=False)
    assert sum(p['contract']=='cascade-identity-v1' for p in requests)==1
    uses=[u for s in r.semantic_selections for u in s.cascade.identities]
    assert len(uses)==2 and [u.reused for u in uses]==[False,True]
    assert uses[0].record==uses[1].record
    assert len([x for x in sink.rows if x.phase=='finished' and x.identity.operation=='judge_entity_cascade'])==1


def test_v30_is_opt_in_and_keeps_legacy_hashes():
    from pathlib import Path
    from contexthub.knowledge_config import KnowledgeConfig
    from contexthub.models.knowledge import canonical_hash
    root=Path(__file__).resolve().parents[1]/'artifacts'
    for arm in ['selective','full-history']:
        old=json.loads((root/'patha-conditional-review-20260927-v27'/f'{arm}.json').read_text())
        parsed=KnowledgeConfig.model_validate(old)
        assert canonical_hash(parsed)==canonical_hash(old)
        new=KnowledgeConfig.model_validate_json((root/'patha-sparse-cascade-20260927-v30'/f'{arm}.json').read_text())
        assert new.discovery.semantic.execution=='sparse-cascade-v1'
        assert parsed.discovery.semantic.execution=='dual-weak-batch-v2'
        assert new.discovery.semantic.retrieval==parsed.discovery.semantic.retrieval
        assert new.discovery.adjudication.weak.model==parsed.discovery.adjudication.weak.model
        assert new.verifier.model==parsed.verifier.model
        assert new.discovery.adjudication.weak.retry==parsed.discovery.adjudication.weak.retry
