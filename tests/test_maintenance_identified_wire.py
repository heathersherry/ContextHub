import json
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.propagation.identified_wire import registry_view, expand_generation, expand_verdict
from contexthub.propagation.verifier import parse_verdict
from contexthub.propagation.candidate import CandidateGenerator
from contexthub.services.change_location import ChangeLocator, location_view, expand_location
from s6_helpers import sample_data, model_config, response, clock_frame
from knowledge_helpers import MemorySink, identity


def gen():
    def part(text):return dict(text=text,evidence_ids=['E000001'],derivation='fixture')
    return dict(proposal=dict(outcome='updated',content_parts=dict(l0_content=None,l1_content=[],l2_content=[part('中文😀 '),part(' next\n')]),causes=[dict(cause_id='C000001',evidence_ids=['E000001'],rationale='fixture')]),unresolved_reason=None)


def maps():
    data=sample_data();e,_=registry_view(data.claims[0].evidence,'E');c,_=registry_view((x.cause for x in data.causes),'C');return data,e,c


def verdict():
    return {k:dict(verdict='PASS',reason='fixture',evidence_ids=['E000001'],claim_indexes=[0],cause_ids=['C000001']) for k in ['applicability','complete_support','conflicts_resolved']}


def test_generation_exact_join_offsets_and_null_empty_preserved():
    data,e,c=maps();p=expand_generation(json.dumps(gen()),e,c).proposal
    assert p.content.l0_content is None and p.content.l1_content==''
    assert p.content.l2_content=='中文😀  next\n'
    assert [(x.start,x.end) for x in p.claims]==[(0,4),(4,10)]
    for x in p.claims:assert p.content.l2_content[x.start:x.end]==x.conclusion and x.evidence==data.claims[0].evidence
    assert expand_generation('{"proposal":null,"unresolved_reason":"missing"}',e,c).proposal is None
    with pytest.raises(ValueError):expand_generation('{"proposal":null,"unresolved_reason":null}',e,c)


@pytest.mark.parametrize('bad',['unknown_evidence','duplicate_evidence','unknown_cause','missing_cause','duplicate_cause','empty_part','extra_coordinates','extra_content','self_grade'])
def test_generation_fail_closed(bad):
    _,e,c=maps();x=gen();p=x['proposal'];part=p['content_parts']['l2_content'][0]
    if bad=='unknown_evidence':part['evidence_ids']=['E999999']
    elif bad=='duplicate_evidence':part['evidence_ids']*=2
    elif bad=='unknown_cause':p['causes'][0]['cause_id']='C999999'
    elif bad=='missing_cause':p['causes']=[]
    elif bad=='duplicate_cause':p['causes']*=2
    elif bad=='empty_part':part['text']=''
    elif bad=='extra_coordinates':part['start']=0
    elif bad=='extra_content':p['content']={'l2_content':'unsupported'}
    else:p['fresh']=True
    with pytest.raises(ValueError):expand_generation(json.dumps(x),e,c)


@pytest.mark.parametrize('bad',['none','unknown_evidence','empty_pass','bad_claim','missing_claim','unknown_cause','missing_cause'])
def test_verifier_original_checks_remain(bad):
    data,e,c=maps();v=verdict()
    if bad=='unknown_evidence':v['applicability']['evidence_ids']=['E999999']
    elif bad=='empty_pass':v['applicability']['evidence_ids']=[]
    elif bad=='bad_claim':v['complete_support']['claim_indexes']=[8]
    elif bad=='missing_claim':v['complete_support']['claim_indexes']=[]
    elif bad=='unknown_cause':v['conflicts_resolved']['cause_ids']=['C999999']
    elif bad=='missing_cause':v['conflicts_resolved']['cause_ids']=[]
    def parse():return parse_verdict(expand_verdict(json.dumps(v),e,c).model_dump_json(),SimpleNamespace(data=data),SimpleNamespace(read=[SimpleNamespace(evidence=x) for x in e.values()]))
    if bad=='none':assert parse().complete_support.verdict=='PASS'
    else:
        with pytest.raises(ValueError):parse()


def location_inputs():
    raw=dict(snapshot_id=str(uuid4()),text='identity\ntemporal\nnew value')
    candidates=[dict(context_id=str(uuid4()),version=3,snapshot_id=str(uuid4()),text='complete old value')]
    def unit(i):return dict(first_id=f'N{i:06d}',last_id=f'N{i:06d}')
    result=dict(status='located',reason='fixture',matches=[dict(candidate_id='C000001',replacement=unit(3),identity_basis=unit(1),temporal_basis=unit(2),relation='same_subject_same_attribute_replacement')])
    return raw,candidates,result


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['none','unknown','reverse','ambiguous','extra','multiple'])
async def test_location_actual_client_and_original_checks(bad):
    raw,candidates,body=location_inputs();m=body['matches'][0]
    if bad=='unknown':m['identity_basis']['first_id']='N999999'
    elif bad=='reverse':m['replacement']=dict(first_id='N000003',last_id='N000001')
    elif bad=='ambiguous':candidates.append(dict(candidates[0],context_id=str(uuid4())))
    elif bad=='extra':m['old']={}
    elif bad=='multiple':body['matches']*=2
    cfg=model_config().model_copy(update={'response_contract':'location-ids-v2'});sent=[]
    def send(req):sent.append(json.loads(req.content));return response(body)
    loc=ChangeLocator(cfg,api_key='fixture',recorder=MemorySink(),transport=httpx.MockTransport(send))
    try:
        if bad=='none':
            result=await loc.locate(raw,candidates,identity=identity())
            assert result.matches[0].old.excerpt==candidates[0]['text']
            assert result.matches[0].replacement.excerpt=='new value'
        else:
            with pytest.raises(ValueError):await loc.locate(raw,candidates,identity=identity())
    finally:await loc.close()
    assert len(sent)==1
    payload=json.loads(sent[0]['messages'][0]['content'].removeprefix('Input '))
    assert ''.join(x['text'] for x in payload['change_units'])==raw['text']


@pytest.mark.asyncio
async def test_generator_actual_wire_and_usage():
    data,e,c=maps();cfg=model_config().model_copy(update={'response_contract':'candidate-parts-v2'});sent=[];sink=MemorySink()
    binding=SimpleNamespace(profile='adaptive',action='M1',causes=tuple(c.values()),model_dump=lambda **k:{'fixture':'binding'})
    bundle=SimpleNamespace(read=[SimpleNamespace(evidence=x) for x in e.values()],model_dump=lambda **k:{'fixture':'bundle'})
    def send(req):sent.append(json.loads(req.content));return response(gen())
    generator=CandidateGenerator(cfg,api_key='fixture',recorder=sink,transport=httpx.MockTransport(send))
    try:out,_=await generator.generate(clock_frame(),binding,bundle,[])
    finally:await generator.close()
    assert out.proposal.content.l2_content=='中文😀  next\n' and len(sent)==1
    payload=json.loads(sent[0]['messages'][0]['content'].removeprefix('Input '));assert payload['evidence_ids'][0]['value']==data.claims[0].evidence[0].model_dump(mode='json')
    assert sink.rows[-1].usage.input_tokens==11
    assert 'response_contract' not in model_config().model_dump()
    assert canonical_hash(cfg)!=canonical_hash(model_config())


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['PASS','UNKNOWN','bad_id','hard_fail'])
async def test_fixed_verifier_real_adapter_path_keeps_live_grant_boundary(outcome):
    from contextlib import asynccontextmanager
    from contexthub.models.knowledge import SealRef
    from contexthub.models.maintenance import HardCheckResult, CheckItem
    from contexthub.propagation.hard_checks import REQUIRED_CHECKS, CHECKER_VERSION
    from contexthub.propagation.verifier import FixedVerifier
    from knowledge_helpers import HASH
    def ref(kind):return SealRef(account_id='fixture',id=uuid4(),kind=kind,sha256=HASH)
    data,e,c=maps();proposal_ref,candidate_ref,bundle_ref,binding_ref,frame_ref=[ref(k) for k in ['maintenance_proposal','candidate','evidence_bundle','maintenance_action','frame']]
    check=HardCheckResult(account_id='fixture',id=uuid4(),candidate=candidate_ref,checker_version=CHECKER_VERSION,checks=tuple(CheckItem(code=k,verdict='FAIL' if outcome=='hard_fail' else 'PASS',reason='fixture') for k in REQUIRED_CHECKS))
    frame=clock_frame();frame.reference=lambda:frame_ref
    binding=SimpleNamespace(account_id='fixture',frame=frame_ref,bundle=bundle_ref,profile='adaptive',action='M1',reference=lambda:binding_ref)
    proposal=SimpleNamespace(action=binding_ref,candidate=candidate_ref,data=data)
    bundle=SimpleNamespace(read=[SimpleNamespace(evidence=x) for x in e.values()],model_dump=lambda **kw:{'fixture':'bundle'})
    objects={proposal_ref:proposal,binding_ref:binding,frame_ref:frame,bundle_ref:bundle,check.reference():check,candidate_ref:SimpleNamespace(generator_call_id=uuid4())};sealed=[]
    class Artifacts:
        async def get(self,db,r):return objects[r]
        async def seal(self,db,obj):sealed.append(obj)
    class Repo:
        @asynccontextmanager
        async def session(self,account):yield object()
    async def check_again(*args,**kwargs):return check
    async def sources(*args):return []
    checks=SimpleNamespace(executor=SimpleNamespace(repo=Repo()),artifacts=Artifacts(),check=check_again,sources=sources)
    sent=[]
    def send(req):
        sent.append(json.loads(req.content));v=verdict()
        if outcome=='UNKNOWN':v['complete_support']['verdict']='UNKNOWN'
        if outcome=='bad_id':v['applicability']['evidence_ids']=['E999999']
        return response(v)
    cfg=model_config().model_copy(update={'response_contract':'verifier-ids-v2'})
    verifier=FixedVerifier(cfg,checks=checks,api_key='fixture',recorder=MemorySink(),transport=httpx.MockTransport(send))
    try:
        if outcome in ['bad_id','hard_fail']:
            with pytest.raises(ValueError):await verifier.verify(proposal_ref,check.reference(),frame=frame,binding=binding)
            assert not sealed and not verifier._grants
        else:
            result=await verifier.verify(proposal_ref,check.reference(),frame=frame,binding=binding)
            if outcome=='PASS':
                verifier.consume(result.reference(),proposal_ref,check.reference(),binding_ref)
                with pytest.raises(ContractError):verifier.consume(result.reference(),proposal_ref,check.reference(),binding_ref)
            else:assert not verifier._grants
    finally:await verifier.close()
    assert len(sent)==(0 if outcome=='hard_fail' else 1)
    if sent:
        payload=json.loads(sent[0]['messages'][0]['content'].removeprefix('Input '))
        assert 'profile' not in payload and 'prior_judgments' not in payload
        assert payload['cause_ids'][0]['id']=='C000001'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['none','wrong_owner','overlap','duplicate_target','unknown_assertion'])
async def test_actual_assertion_entry_keeps_grounding_and_overlap_checks(bad):
    from contexthub.services.assertion_change import locate_assertions
    data,_,_=maps();raw,candidates,body=location_inputs()
    candidates[0]['assertions']=[data.claims[0].evidence[0].model_dump(mode='json')]
    candidates.append(dict(context_id=str(uuid4()),version=1,snapshot_id=str(uuid4()),text='another old value',assertions=[sample_data().claims[0].evidence[0].model_dump(mode='json')]))
    m=body['matches'][0];m['assertion_id']='A000001';m['relation']='same_subject_same_attribute_direct_assertion_replacement'
    if bad=='wrong_owner':m['assertion_id']='A000002'
    elif bad=='unknown_assertion':m['assertion_id']='A999999'
    elif bad=='duplicate_target':body['matches']*=2
    elif bad=='overlap':body['matches'].append(dict(m,candidate_id='C000002',assertion_id='A000002'))
    cfg=model_config().model_copy(update={'response_contract':'location-ids-v2'});sent=[]
    def send(req):sent.append(json.loads(req.content));return response(body)
    loc=ChangeLocator(cfg,api_key='fixture',recorder=MemorySink(),transport=httpx.MockTransport(send))
    try:
        if bad=='none':
            result=await locate_assertions(loc,raw,candidates,identity=identity())
            assert result.matches[0].original_assertion.excerpt==data.claims[0].evidence[0].excerpt
        else:
            with pytest.raises(ValueError):await locate_assertions(loc,raw,candidates,identity=identity())
    finally:await loc.close()
    assert len(sent)==1
    payload=json.loads(sent[0]['messages'][0]['content'].removeprefix('Input '))
    assert payload['contract']=='direct-assertion-change-ids-v2'
