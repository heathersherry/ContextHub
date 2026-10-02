import json
from uuid import UUID
import httpx
import pytest
from contexthub.discovery.adjudication import AdjudicationService,EvidenceSet
from contexthub.discovery.batch_adjudication import BatchAdjudicator
from contexthub.discovery.dependency_batch import BatchFact
from contexthub.models.knowledge import canonical_hash
from contexthub.llm.request_parameters import RequestParameters, SeedPolicy
from discovery_helpers import MemoryDB,call_identity
from knowledge_helpers import MemorySink
from s4_helpers import evidence,adjudication_config


async def run(mode='ok',at_max=True):
    db=MemoryDB();e=await evidence(db);sink=MemorySink();requests=[]
    def handle(req):
        body=json.loads(req.content)
        # Reproduce the provider's JSON-mode prerequisite on the actual wire request.
        if body.get('response_format', {}).get('type') == 'json_object':
            if not any('json' in m['content'].lower() for m in body['messages']):
                return httpx.Response(400, json={'error': {'message': 'messages must contain JSON'}})
        p=json.loads(body['messages'][0]['content'].split('\nInput:\n',1)[1]);requests.append(p)
        items=[]
        for c in p['candidates']:
            eid=p['evidence'][0]['id']
            j=dict(verdict='DEPENDS',citation_ids=[eid],steps=[dict(premise_ids=[eid],previous_steps=[],conclusion_evidence_id=eid)],
                negative_basis='none',exclusion_ids=[],conflicts=[],missing=[],semantic_basis='original evidence')
            if c['candidate_number']==(2 if mode=='disagree_second' else 1):
                if mode=='missing': j.update(verdict='UNKNOWN',missing=['tax rule'])
                if mode=='bad': j['citation_ids']=['E999999']
                if mode=='unsupported': j['steps']=[]
                if mode in ('disagree','disagree_second') and p['role']=='challenge': j['verdict']='UNKNOWN'
            items.append(dict(candidate_number=c['candidate_number'],judgment=j))
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'items':items})}}],
            'model':'fixture','usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
    cfg=adjudication_config()
    parameters=RequestParameters(output_limit_field='max_tokens',reasoning_effort=None,
        response_format='json_object',temperature_supported=True,
        seed=SeedPolicy(support='unsupported',enabled=False,evidence_reference='offline-fixture'))
    cfg=cfg.model_copy(update={role:getattr(cfg,role).model_copy(update={'request_parameters':parameters})
        for role in ('weak','strong')})
    base=AdjudicationService(cfg,api_key='fixture',recorder=sink,transport=httpx.MockTransport(handle))
    batch=BatchAdjudicator(base)
    try:
        out=await batch.judge(db,target=BatchFact(id=UUID(int=1),text='target'),
            candidates=tuple(BatchFact(id=UUID(int=i),text=f'candidate {i}') for i in (2,3)),
            bundle=EvidenceSet(evidence=(e,),complete=True,at_max_scope=at_max,issues=()),
            identity=call_identity(base.config).model_copy(update={'config_sha256':canonical_hash(batch.configuration)}))
    finally: await base.close()
    return out,requests,sink


@pytest.mark.asyncio
async def test_two_candidates_two_calls_not_four_and_shared_cost_identity():
    out,req,sink=await run()
    assert len(req)==2 and all(r.verdict=='DEPENDS' and r.action=='complete' for r in out.values())
    assert 'prior_judgments' not in req[1]
    assert out[UUID(int=2)].traces[0].call_id==out[UUID(int=3)].traces[0].call_id
    assert len([r for r in sink.rows if r.requested_model and r.phase=='finished'])==2


@pytest.mark.asyncio
async def test_missing_json_instruction_reproduces_provider_rejection(monkeypatch):
    import contexthub.discovery.batch_adjudication as module
    monkeypatch.setattr(module, 'BATCH_INSTRUCTIONS', module.BATCH_INSTRUCTIONS.replace('JSON', ''))
    out,requests,sink=await run()
    assert not requests
    assert all(r.reason=='weak_call_failed' for r in out.values())
    finished=[r for r in sink.rows if r.phase=='finished' and r.requested_model]
    assert len(finished)==1 and finished[0].status_code==400


@pytest.mark.asyncio
@pytest.mark.parametrize('at_max,action',[(True,'pending'),(False,'read_more')])
async def test_missing_one_item_does_not_discard_other(at_max,action):
    out,req,_=await run('missing',at_max)
    assert out[UUID(int=2)].action==action
    assert out[UUID(int=3)].action=='complete'
    assert len(req[1]['candidates'])==1


@pytest.mark.asyncio
async def test_bad_reference_retained_not_retried():
    out,req,_=await run('bad')
    assert out[UUID(int=2)].reason=='weak_call_failed'
    assert out[UUID(int=3)].action=='complete' and len(req)==2


@pytest.mark.asyncio
async def test_strong_only_receives_disputed_candidates():
    out,req,_=await run('disagree')
    assert [p['role'] for p in req]==['normal','challenge','strong']
    assert [c['candidate_number'] for c in req[2]['candidates']]==[1]
    assert out[UUID(int=2)].reason=='mandatory_strong'


@pytest.mark.asyncio
async def test_unsupported_positive_still_fails_original_checks():
    out,req,_=await run('unsupported')
    assert out[UUID(int=2)].action=='pending'
    assert 'unsupported_derivation' in out[UUID(int=2)].traces[-1].errors
    assert out[UUID(int=3)].action=='complete'


@pytest.mark.asyncio
async def test_strong_subset_second_item_keeps_internal_identity():
    out,req,_=await run('disagree_second')
    assert [c['candidate_number'] for c in req[-1]['candidates']]==[2]
    assert out[UUID(int=3)].reason=='mandatory_strong'
    assert out[UUID(int=2)].reason=='dual_weak'
    wire=json.dumps(req)
    assert all(str(UUID(int=i)) not in wire for i in (1,2,3))
