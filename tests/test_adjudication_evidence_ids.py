import json
import pytest
from contexthub.discovery.adjudication import (
    expand_judgment, compact_judgment, reference_registry,
)
from contexthub.models.knowledge import canonical_hash, ContractError
from test_knowledge_adjudication import run
from s4_helpers import adjudication_config, evidence
from discovery_helpers import MemoryDB


def selected(p, verdict='DEPENDS', **updates):
    ident=p['evidence'][0]['id']
    d=dict(verdict=verdict,citation_ids=[ident],
           steps=[dict(premise_ids=[ident],previous_steps=[],conclusion_id=ident)],
           negative_basis='none',exclusion_ids=[],conflicts=[],missing=[],semantic_basis='fixture grounded reason')
    d.update(updates);return d


def cfg(**updates):
    return adjudication_config(**updates).model_copy(update={'response_contract':'evidence-ids-v2'})


@pytest.mark.asyncio
async def test_program_expands_exact_trusted_refs_and_stable_ids():
    db=MemoryDB();a=await evidence(db,'same');b=await evidence(db,'same')
    registry,reverse=reference_registry((a,b,a));assert len(registry)==2
    raw=selected({'evidence':[{'id':'E000002'}]})
    j=expand_judgment(json.dumps(raw),registry,'a'*64)
    assert j.citations==(b,) and j.steps[0].conclusion==b and j.evidence_sha256=='a'*64
    assert compact_judgment(j,reverse)=={**raw, 'schema_version':1, 'steps':[dict(schema_version=1,**raw['steps'][0])]}
    with pytest.raises(ContractError,match='undeclared_adjudication_evidence_id'):
        expand_judgment(json.dumps(dict(raw,citation_ids=['E999999'])),registry,'a'*64)
    with pytest.raises(ContractError,match='duplicate_adjudication_evidence_id'):
        expand_judgment(json.dumps(dict(raw,citation_ids=['E000001','E000001'])),registry,'a'*64)
    for field,value in [('evidence_sha256','0'*64),('citations',[a.model_dump(mode='json')])]:
        with pytest.raises(ValueError):expand_judgment(json.dumps(dict(raw,**{field:value})),registry,'a'*64)


@pytest.mark.asyncio
async def test_dual_weak_isolated_and_strong_prior_compact():
    out,req,sink=await run(lambda p,n:selected(p),cfg=cfg(lambda_p1=0))
    assert out.action=='complete' and out.reason=='optional_strong'
    assert [p['role'] for p in req]==['normal','challenge','strong']
    assert req[1]['evidence']==list(reversed(req[0]['evidence']))
    assert 'prior_judgments' not in req[0] and 'prior_judgments' not in req[1]
    assert req[2]['prior_judgments'][0]['judgment']['citation_ids']==['E000001']
    assert 'excerpt' not in json.dumps(req[2]['prior_judgments'])
    assert out.accepted.citations[0].model_dump(mode='json')==req[0]['evidence'][0]['evidence']
    assert canonical_hash(adjudication_config())!=canonical_hash(cfg())
    assert 'response_contract' not in adjudication_config().model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize('problem',['invented','extra','missing_field'])
async def test_contract_error_stops_candidate_without_retry(problem):
    def respond(p,n):
        v=selected(p)
        if problem=='invented':v['steps'][0]['conclusion_id']='NOT_SUPPLIED'
        elif problem=='extra':v['start']=0
        else:del v['negative_basis']
        return v
    out,req,sink=await run(respond,cfg=cfg())
    assert out.action=='pending' and out.reason=='weak_call_failed' and len(req)==1
    assert len([r for r in sink.rows if r.phase=='finished' and r.requested_model])==1


@pytest.mark.asyncio
@pytest.mark.parametrize('problem',['steps','forward','negative','provenance','conflict','unknown'])
async def test_existing_semantic_checks_not_weakened(problem):
    def respond(p,n):
        v=selected(p)
        if problem=='steps':v['steps']=[]
        elif problem=='forward':v['steps'][0]['previous_steps']=[1]
        elif problem in ['negative','provenance']:
            v.update(verdict='INDEPENDENT',negative_basis='alternative_support' if problem=='negative' else 'explicit_non_use',exclusion_ids=v['citation_ids'])
        elif problem=='conflict':v['conflicts']=['unresolved']
        else:v['verdict']='UNKNOWN'
        return v
    out,req,_=await run(respond,cfg=cfg(),provenance=problem=='provenance')
    assert out.action=='pending' and len(req)==3


@pytest.mark.asyncio
async def test_missing_evidence_preserves_expand_or_pending():
    for at_max,action in [(False,'read_more'),(True,'pending')]:
        out,req,_=await run(lambda p,n:selected(p,missing=['premise']),cfg=cfg(),at_max=at_max)
        assert out.action==action and len(req)==1
