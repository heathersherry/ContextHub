import json
from uuid import uuid4
import httpx
import pytest
from contexthub.discovery.adjudication import AdjudicationService, EvidenceSet
from contexthub.discovery.sparse_adjudication import SparseAdjudicator
from contexthub.discovery.stage0 import Mention
from contexthub.discovery._work import LocalOperation
from contexthub.models.knowledge import canonical_hash, ContractError
from s4_helpers import evidence, adjudication_config
from discovery_helpers import MemoryDB, call_identity, source
from knowledge_helpers import MemorySink
from test_identity_routing import ROUTING


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict,expected_calls',[('SAME',1),('DIFFERENT',1),('UNKNOWN',2)])
async def test_cache_reuses_structured_result_and_original_trace(verdict,expected_calls):
    db=MemoryDB();left=await evidence(db,'Alice manages a team.');right=await evidence(db,'Alice approves expenses.')
    bundle=EvidenceSet(evidence=(left,right),complete=True,at_max_scope=False,issues=())
    mentions=[Mention(text='Alice',evidence=e,start=e.start) for e in (left,right)]
    sink=MemorySink();requests=[]
    def handle(req):
        p=json.loads(json.loads(req.content)['messages'][0]['content'].split('\nInput:\n')[1]);requests.append(p)
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(dict(verdict=verdict,
            citation_ids=[p['evidence'][0]['id']],reason='source-backed fixture',conflicts=[]))}}]})
    base=AdjudicationService(adjudication_config(),api_key='fixture',recorder=sink,transport=httpx.MockTransport(handle))
    engine=SparseAdjudicator(base,{},ROUTING);ident=call_identity(base.config).model_copy(update={'config_sha256':canonical_hash(engine.configuration)})
    work=LocalOperation(sink,ident.model_copy(update={'call_id':uuid4(),'operation':'fixture','model_role':'local'}),{})
    await work.start()
    try:
        a,reused=await engine.resolve_identity(db,left=mentions[0],right=mentions[1],bundle=bundle,identity=ident,generation=0,work=work)
        b,reused2=await engine.resolve_identity(db,left=mentions[0],right=mentions[1],
            bundle=bundle.model_copy(update={'at_max_scope':True}),identity=ident.model_copy(update={'call_id':uuid4()}),generation=3,work=work)
        assert not reused and reused2 and a==b and b.original_generation==0
        assert b.verdict==verdict and len(requests)==expected_calls
        # A changed visible source invalidates even when the same two names remain.
        extra=await evidence(db,'Another Alice works elsewhere.')
        _,reused=await engine.resolve_identity(db,left=mentions[0],right=mentions[1],
            bundle=bundle.model_copy(update={'evidence':(*bundle.evidence,extra)}),identity=ident.model_copy(update={'call_id':uuid4()}),generation=4,work=work)
        assert not reused and len(requests)==expected_calls*2
        other=SparseAdjudicator(base,engine.cache,ROUTING)
        other.configuration={**other.configuration,'prompt_revision':'new-fixture'}
        _,reused=await other.resolve_identity(db,left=mentions[0],right=mentions[1],bundle=bundle,
            identity=ident.model_copy(update={'call_id':uuid4(),'config_sha256':canonical_hash(other.configuration)}),generation=5,work=work)
        assert not reused and len(requests)==expected_calls*3
        original=await base.evidence.artifacts.get(db,left.source)
        version2=await source(db,left.excerpt,version=2,context_id=original.source.context_id)
        left2=await base.evidence.reference(db,version2,start=left.start,end=left.end,provenance=('version2',))
        m2=Mention(text='Alice',evidence=left2,start=left2.start)
        _,reused=await engine.resolve_identity(db,left=m2,right=mentions[1],
            bundle=bundle.model_copy(update={'evidence':(left2,right)}),
            identity=ident.model_copy(update={'call_id':uuid4()}),generation=6,work=work)
        assert not reused and len(requests)==expected_calls*4
        with pytest.raises(ContractError,match='account_mismatch'):
            await engine.resolve_identity(db,left=mentions[0],right=mentions[1],bundle=bundle,
                identity=ident.model_copy(update={'account_id':'other-account','call_id':uuid4()}),generation=6,work=work)
        assert len(requests)==expected_calls*4
    finally:await work.finish({});await base.close()
