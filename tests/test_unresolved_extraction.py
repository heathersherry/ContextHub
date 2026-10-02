import json
from uuid import uuid4,uuid5
import pytest
from contexthub.discovery.extraction import parse_response
from contexthub.models.knowledge import PropositionArtifact
from contexthub.services.artifact_service import ArtifactService
from discovery_helpers import MemoryDB,source,config,extract,history,search_policy,pages,call_identity

CONTRACT='evidence-ids-v4-quarantine'

def output():
    return {'propositions':[{'text':'unverified first','evidence_ids':['E000001','E000018']},
                            {'text':'valid second','evidence_ids':['E000001']},
                            {'text':'unverified third','evidence_ids':['E000099']}]}


def test_partition_preserves_original_ordinals_text_and_all_ids():
    result=parse_response(json.dumps(output()),{uuid4():'valid evidence'},response_contract=CONTRACT)
    assert result.proposition_ordinals==(1,)
    assert [p.text for p in result.propositions]==['valid second']
    assert [p.ordinal for p in result.unresolved]==[0,2]
    assert result.unresolved[0].evidence_ids==('E000001','E000018')
    assert result.unresolved[0].unknown_evidence_ids==('E000018',)
    assert all(p.status=='unresolved_unverified' for p in result.unresolved)


@pytest.mark.parametrize('data',[{'propositions':[{'text':'x','evidence_ids':[]}]},
    {'propositions':[{'text':'x','evidence_ids':['E000001','E000001']}]},
    {'propositions':[{'text':'x','evidence_ids':['E999999'],'start':0}]}])
def test_only_unknown_ids_are_quarantined_other_errors_still_fail(data):
    with pytest.raises(ValueError):parse_response(json.dumps(data),{uuid4():'text'},response_contract=CONTRACT)


@pytest.mark.asyncio
@pytest.mark.parametrize('all_bad',[False,True])
async def test_service_seals_partial_and_two_search_arms_continue_only_valid(all_bad):
    from contexthub.discovery.search import SearchService
    db=MemoryDB();ref=await source(db,'original\nsecond');data=output()
    if all_bad:data['propositions'].pop(1)
    cfg=config(response_contract=CONTRACT,prompt_template='{sources_json}')
    obj,sink,req=await extract(db,(ref,),[data],cfg=cfg)
    assert obj.status=='partial' and obj.failure_reason=='unresolved_evidence_ids'
    assert len(obj.unresolved)==2 and len(obj.propositions)==(0 if all_bad else 1)
    assert await ArtifactService().get(db,obj.reference())==obj
    if not all_bad:assert obj.propositions[0].id==uuid5(obj.id,'1')
    payload=json.loads(req[0]['messages'][0]['content'])
    assert payload['allowed_evidence_ids']==['E000001','E000002']
    assert ''.join(u['text'] for s in payload['sources'] for u in s['evidence_units'])=='original\nsecond'
    assert len(req)==1
    for strategy in ['selective','full-history']:
        result=await pages(SearchService(recorder=sink),db,obj.reference(),history((ref,)),search_policy(strategy=strategy))
        assert result[-1].complete
        candidates=[c for page in result for c in page.candidates if c.kind=='batch_proposition']
        assert [c.text for c in candidates]==([] if all_bad else ['valid second'])
    assert any(r.error is not None and r.error.code=='unresolved_evidence_ids' for r in sink.rows)
    with pytest.raises(ValueError):PropositionArtifact.model_validate({**obj.model_dump(),'status':'complete','failure_reason':None})


@pytest.mark.asyncio
async def test_real_discovery_excludes_unresolved_and_audits_artifact():
    from contexthub.discovery.service import DiscoveryConfig,DiscoveryService
    from contexthub.discovery.search import SearchService
    from contexthub.discovery.adjudication import AdjudicationService
    from s4_helpers import NodeDB,adjudication_config,judge_transport,judgment
    db=NodeDB();ref=await source(db,'original')
    obj,sink,_=await extract(db,(ref,),[output()],cfg=config(response_contract=CONTRACT))
    cfg=DiscoveryConfig(timeout_seconds=30,search=search_policy(lambda_p1=10),adjudication=adjudication_config(lambda_p1=10))
    req=[];adj=AdjudicationService(cfg.adjudication,api_key='fixture',recorder=sink,transport=judge_transport(req,lambda p,n:judgment(p,'INDEPENDENT')))
    try:
        report=await DiscoveryService(cfg,search=SearchService(recorder=sink),adjudicator=adj,recorder=sink).discover(db,obj.reference(),history((ref,)),identity=call_identity(cfg))
    finally:await adj.close()
    assert report.status=='complete'
    assert report.excluded_unresolved_extractions==(obj.reference(),)
    assert [m.text for m in report.mappings if m.proposition_id]==['valid second']
    assert 'unverified first' not in json.dumps(req)
    assert await ArtifactService().get(db,report.reference())==report
    # Exercise the real finalizer: exclusion lineage must survive graph handoff.
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from contexthub.services.knowledge_service import KnowledgeService
    from contexthub.models.knowledge_phases import PreparedInput, FinalizeGraphRequest
    from contexthub.models.knowledge import canonical_hash
    snapshot=await ArtifactService().get(db,ref)
    prepared=PreparedInput(account_id=db.account,id=uuid4(),operation_id=uuid4(),agent_id='agent',
        scope='private',owner_space='agent',source_ids=(snapshot.source,),extraction=obj.reference(),
        extraction_config_sha256=obj.extraction_config_sha256,prior_extractions=(),previous=None,history=history((ref,)))
    await ArtifactService().seal(db,prepared)
    ctx=SimpleNamespace(account_id=db.account,agent_id='agent')
    class Finalizer:
        artifacts=ArtifactService()
        config=SimpleNamespace(discovery=cfg,maintenance=None)
        config_hash='0'*64
        @asynccontextmanager
        async def _session(self,*args):yield db
        async def _prepared_access(self,*args):return prepared
        async def _reserve_phase(self,*args):return SimpleNamespace(id=uuid4()),True
        async def _authorize(self,*args,**kwargs):return {'version':1}
        async def register_graph(self,db,reference,*args):
            self.handoff=await self.artifacts.get(db,reference)
            return ()
        async def get_maintenance_result(self,*args):return self.handoff
    finalizer=Finalizer();finalizer.executor=finalizer
    handoff=await KnowledgeService.finalize_graph.__wrapped__(finalizer,
        FinalizeGraphRequest(request_id=uuid4(),reports=(report.reference(),),prepared=(prepared.reference(),)),ctx)
    assert handoff.excluded_unresolved_extractions==(obj.reference(),)
    assert all(m.text!='unverified first' for m in handoff.mappings)


@pytest.mark.asyncio
async def test_actual_public_prepare_and_access_allow_partial_without_success_label():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from contexthub.services.knowledge_service import KnowledgeService
    from contexthub.models.knowledge_phases import PrepareRequest
    db=MemoryDB();ref=await source(db,'original')
    cfg=config(response_contract=CONTRACT)
    obj,_,_=await extract(db,(ref,),[output()],cfg=cfg)
    snapshot=await ArtifactService().get(db,ref)
    body=PrepareRequest(request_id=uuid4(),content='original')
    op=SimpleNamespace(id=uuid4(),scope='private',owner_space='agent',source_ids=(snapshot.source,))
    ctx=SimpleNamespace(account_id=db.account,agent_id='agent')
    class Harness:
        artifacts=ArtifactService()
        config=SimpleNamespace(extraction=cfg,ingest_timeout_seconds=30)
        config_hash='0'*64
        async def _reserve_input(self,*args):return op,True
        async def _extract_input(self,*args):return obj,history((ref,)),()
        @asynccontextmanager
        async def _session(self,*args):yield db
        async def _finish(self,operation,result,**kwargs):self.result=result
        async def get_maintenance_result(self,*args):return self.result
        async def _get(self,*args):return SimpleNamespace(result=self.result)
        async def _authorize(self,*args,**kwargs):return {'version':1}
    service=Harness()
    result=await KnowledgeService.prepare.__wrapped__(service,body,ctx)
    assert result.prepared is not None and result.status=='complete'
    assert result.result.reason=='prepared_with_unresolved_exclusions'
    prepared=await KnowledgeService._prepared_access(service,db,result.prepared,ctx)
    assert prepared.extraction==obj.reference()
    assert (await service.artifacts.get(db,prepared.extraction)).status=='partial'


def test_diagnostic_mode_cannot_enter_formal_factory():
    from examples.knowledge_fixture import configuration
    from contexthub.knowledge_config import KnowledgeConfig
    from integrations.memebench.patha.system_factory import SystemConfiguration
    knowledge=configuration()
    data=knowledge.model_dump(mode='json');data['extraction']['response_contract']=CONTRACT
    knowledge=KnowledgeConfig.model_validate(data)
    with pytest.raises(ValueError,match='diagnostic_extraction_not_formal'):
        SystemConfiguration(mode='formal',knowledge=knowledge)
