import json
from uuid import uuid4
from types import SimpleNamespace
import pytest
from contexthub.discovery.identity_routing import IdentityRoutingConfig,route_pair,match_reason
from contexthub.discovery.stage0 import Mention,Stage0Config
from contexthub.discovery.mention_ner import SpacyMentionConfig,entity_spans
from discovery_helpers import source,response,extract,history,search_policy,call_identity
from s4_helpers import NodeDB,evidence,adjudication_config,judgment,judge_transport

ROUTING=IdentityRoutingConfig(matching=('exact','name_token_overlap','initialism','explicit_alias'))
LABELS=('PERSON','ORG','GPE','LOC','FAC','PRODUCT','EVENT','WORK_OF_ART','LAW','NORP')

def ner():
    return SpacyMentionConfig(spacy_version='3.8.14',english_model='en_core_web_sm',english_version='3.8.0',
                             chinese_model='zh_core_web_sm',chinese_version='3.8.0',allowed_entity_labels=LABELS)


def candidate(text,refs):
    return SimpleNamespace(id=uuid4(),text=text,evidence=refs,kind='batch_proposition',sources=tuple(r.source for r in refs))


@pytest.mark.asyncio
async def test_pair_signals_are_not_identity_verdicts():
    e=await evidence(NodeDB(),'Giant Eagle aka GE, Alice and Alice Smith, Amazon, 3.')
    mentions={name:Mention(text=name,evidence=e,start=e.excerpt.index(name)) for name in ['Giant Eagle','GE','Alice','Alice Smith','Amazon','3']}
    assert match_reason(mentions['Giant Eagle'],mentions['GE'],ROUTING)=='possible_initialism'
    assert match_reason(mentions['Alice'],mentions['Alice Smith'],ROUTING)=='shared_name_token'
    assert match_reason(mentions['Giant Eagle'],mentions['Amazon'],ROUTING) is None
    assert match_reason(mentions['Giant Eagle'],mentions['3'],ROUTING) is None
    assert match_reason(mentions['Alice'],mentions['Alice'],ROUTING) is None
    alias=await evidence(NodeDB(),'Robert aka Bob')
    assert match_reason(Mention(text='Robert',evidence=alias,start=0),Mention(text='Bob',evidence=alias,start=11),ROUTING)=='source_alias_phrase'
    p,audit=route_pair(candidate('Alice approves.',(e,)),candidate('Amazon sells.',(e,)),tuple(mentions.values()),ROUTING,0)
    assert not p and audit.reason=='no_identity_pair_routed_dependency_still_judged'


@pytest.mark.asyncio
async def test_attachment_requires_fact_name_and_cited_occurrence():
    e=await evidence(NodeDB(),'Alice met Alice.')
    m1=Mention(text='Alice',evidence=e,start=0);m2=Mention(text='Alice',evidence=e,start=10)
    left=candidate('Alice approves.',(e,));right=candidate('Alice receives.',(e,))
    pairs,_=route_pair(left,right,(m1,m2),ROUTING,0)
    assert len(pairs)==1  # two positions retained, symmetric work shared
    assert pairs[0][1].start!=pairs[0][2].start
    other=await evidence(NodeDB(),'Alice elsewhere.')
    pairs,_=route_pair(candidate('Alice approves.',(other,)),right,(m1,m2),ROUTING,0)
    assert not pairs
    ann=await evidence(NodeDB(),'Ann approves.')
    assert not route_pair(candidate('annual budget',(ann,)),right,(Mention(text='Ann',evidence=ann,start=0),),ROUTING,0)[0]


def test_name_labels_exclude_values_without_changing_whole_product(monkeypatch):
    import contexthub.discovery.mention_ner as module
    text='Model 3 costs 300 on Monday'
    monkeypatch.setattr(module,'_recognize',lambda *args:((0,7,'PRODUCT'),(13,16,'CARDINAL'),(20,26,'DATE')))
    assert entity_spans(ner(),text)==((0,7),)
    assert entity_spans(ner().model_copy(update={'allowed_entity_labels':None}),text)==((0,7),(13,16),(20,26))


async def flow(texts, entity_verdict='SAME', expand=False, by_id=False, unknown_policy='block', dependency_mode='normal', identity_failure=False):
    from contexthub.discovery.service import DiscoveryConfig,DiscoveryService
    from contexthub.discovery.search import SearchService
    from contexthub.discovery.adjudication import AdjudicationService
    from contexthub.services.artifact_service import ArtifactService
    db=NodeDB();refs=[];raw={'propositions':[]}
    older=await source(db,json.dumps([{'role':'user','content':'Unrelated background.'}])) if expand else None
    for text in texts:
        original=json.dumps([{'role':'user','content':text}]);ref=await source(db,original);refs.append(ref)
        prop=response(ref,original)['propositions'][0];prop['text']=text;raw['propositions'].append(prop)
    artifact,sink,_=await extract(db,tuple(refs),[raw])
    stage=Stage0Config(version='test-names',rules=(),aliases=(),alias_version='empty',mention_patterns=(),mention_view='dialogue-content-v2',spacy_ner=ner())
    ac=adjudication_config(stage0=stage, response_contract="evidence-ids-v2" if by_id else "references-v1")
    cfg=DiscoveryConfig(timeout_seconds=30,search=search_policy(initial_sources=1 if expand else 10,lambda_p1=10),adjudication=ac,identity_routing=ROUTING.model_copy(update={'unknown_policy':unknown_policy}))
    requests=[]
    def respond(p,n):
        if p['kind']=='entity' and identity_failure:return {'invalid':'schema'}
        from contexthub.discovery.adjudication import compact_judgment, Judgment, reference_registry
        from contexthub.models.knowledge import EvidenceRef
        flat={**p,'evidence':[x['evidence'] for x in p['evidence']]} if by_id else p
        out=judgment(flat,entity_verdict if p['kind']=='entity' else
                     'DEPENDS' if p['left']==texts[0] and p['right']==texts[1] else 'INDEPENDENT')
        if p['kind']=='dependency' and dependency_mode=='identity_required':
            out=judgment(flat,'UNKNOWN',missing=['identity required but not established'])
        if p['kind']=='dependency' and dependency_mode=='unsupported':
            out=judgment(flat,'DEPENDS',steps=[])
        if by_id:
            _, reverse=reference_registry(tuple(EvidenceRef.model_validate(x) for x in flat['evidence']))
            return compact_judgment(Judgment.model_validate(out),reverse)
        return out
    adj=AdjudicationService(ac,api_key='fixture',recorder=sink,transport=judge_transport(requests,respond))
    try:report=await DiscoveryService(cfg,search=SearchService(recorder=sink),adjudicator=adj,recorder=sink).discover(db,artifact.reference(),history(tuple(([older] if older else [])+refs)),identity=call_identity(cfg))
    finally:await adj.close()
    graph=await ArtifactService().get(db,report.graph)
    assert await ArtifactService().get(db,report.reference())==report
    return report,graph,requests


@pytest.mark.asyncio
async def test_no_shared_name_still_judges_cross_entity_dependency():
    report,graph,requests=await flow(['Giant Eagle has a contract.','Amazon receives the payment.'])
    assert requests and all(p['kind']=='dependency' for p in requests)
    assert len(graph.edges)==1 and report.status=='complete'
    assert len(report.identity_routes)==2 and all(r.selected_pairs==0 for r in report.identity_routes)


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict',['SAME','DIFFERENT'])
@pytest.mark.parametrize('by_id',[False,True])
async def test_identity_on_demand_reused_and_passed_to_both_dependency_weaks(verdict,by_id):
    report,graph,requests=await flow(['Alice approves invoices.','Alice receives invoices.'],verdict,by_id=by_id)
    entity=[p for p in requests if p['kind']=='entity'];dependency=[p for p in requests if p['kind']=='dependency']
    assert [p['role'] for p in entity]==['normal','challenge']
    assert dependency and len(graph.edges)==1
    assert all(p['identity_resolutions'][0]['verdict']==verdict for p in dependency)
    if by_id:assert all('citation_ids' in p['identity_resolutions'][0]['accepted'] for p in dependency)
    assert sum(r.reused_resolutions for r in report.identity_routes)==1
    assert all('prior_judgments' not in p for p in dependency if p['role']!='strong')


@pytest.mark.asyncio
async def test_unknown_identity_retains_pending_dependency_no_fake_negative():
    report,graph,requests=await flow(['Alice approves invoices.','Alice receives invoices.'],'UNKNOWN')
    assert all(p['kind']=='entity' for p in requests)
    assert report.status=='pending' and not graph.edges and len(graph.pending)==2
    assert all(p.reason=='required_identity_unresolved' for p in graph.pending)


@pytest.mark.asyncio
async def test_evidence_expansion_invalidates_identity_cache():
    report,graph,requests=await flow(['Alice approves invoices.','Alice receives invoices.'],expand=True)
    normal=[p for p in requests if p['kind']=='entity' and p['role']=='normal']
    assert len(normal)==2 and normal[0]['evidence_sha256']!=normal[1]['evidence_sha256']
    dependency=[p for p in requests if p['kind']=='dependency']
    assert dependency and all(p['evidence_sha256']==normal[-1]['evidence_sha256'] for p in dependency)
    assert {r.generation for r in report.identity_routes}=={0,1}


@pytest.mark.asyncio
async def test_stale_resolution_rejected_before_model_call():
    from contexthub.discovery.adjudication import AdjudicationService,IdentityResolution,Adjudication,EvidenceSet
    db=NodeDB();e=await evidence(db,'Alice meets Alice.')
    mentions=(Mention(text='Alice',evidence=e,start=0),Mention(text='Alice',evidence=e,start=12))
    stale=IdentityResolution(mentions=mentions,result=Adjudication(kind='entity',verdict='SAME',action='complete',reason='fixture',evidence_sha256='0'*64,traces=(),generation=0))
    cfg=adjudication_config();from knowledge_helpers import MemorySink
    requests=[];service=AdjudicationService(cfg,api_key='fixture',recorder=MemorySink(),transport=judge_transport(requests))
    try:
        with pytest.raises(ValueError,match='identity_resolution_not_current_complete'):
            await service.judge(db,kind='dependency',left='a',right='b',identity=call_identity(cfg),bundle=EvidenceSet(evidence=(e,),complete=True,at_max_scope=True,issues=()),identity_resolutions=(stale,))
    finally:await service.close()
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize('by_id',[False,True])
async def test_advisory_unknown_continues_dependency_with_reference(by_id):
    report,graph,requests=await flow(['Alice approves invoices.','Alice receives invoices.'],
        'UNKNOWN',by_id=by_id,unknown_policy='advisory')
    deps=[p for p in requests if p['kind']=='dependency']
    assert deps and graph.edges and report.status=='complete'
    for p in deps:
        ref=p['identity_resolutions'][0]
        assert ref['verdict']=='UNKNOWN' and ref['accepted'] is None
        assert ref['action']=='pending' and ref['reason'] and ref['uncertainty']
        assert ref['failure_notes']
    assert sum(p['kind']=='entity' and p['role']=='normal' for p in requests)==1


@pytest.mark.asyncio
async def test_advisory_unknown_still_pending_when_dependency_needs_identity():
    report,graph,requests=await flow(['Alice approves invoices.','Alice receives invoices.'],
        'UNKNOWN',unknown_policy='advisory',dependency_mode='identity_required')
    assert any(p['kind']=='dependency' for p in requests)
    assert report.status=='pending' and len(graph.pending)==2 and not graph.edges
    assert all(p.reason=='missing_premise_at_max' for p in graph.pending)


@pytest.mark.asyncio
async def test_identity_wire_failure_is_explicit_uncertainty_no_retry():
    report,graph,requests=await flow(['Alice approves invoices.','Alice receives invoices.'],
        unknown_policy='advisory',identity_failure=True,by_id=True)
    assert len([p for p in requests if p['kind']=='entity'])==1
    deps=[p for p in requests if p['kind']=='dependency']
    assert deps
    for p in deps:
        ref=p['identity_resolutions'][0]
        assert ref['verdict']=='UNKNOWN' and ref['reason']=='weak_call_failed'
        assert ref['accepted'] is None and ref['failure_notes'][0]['errors']
    assert any(d.result.kind=='entity' and d.result.action=='pending' for d in report.decisions)


@pytest.mark.asyncio
async def test_advisory_unknown_does_not_bypass_dependency_evidence_checks():
    report,graph,requests=await flow(['Alice approves invoices.','Alice receives invoices.'],
        'UNKNOWN',unknown_policy='advisory',dependency_mode='unsupported')
    assert any(p['kind']=='dependency' for p in requests)
    assert report.status=='pending' and not graph.edges


def test_legacy_routing_policy_serialization_unchanged():
    assert 'unknown_policy' not in ROUTING.model_dump()
    assert ROUTING.unknown_policy=='block'
