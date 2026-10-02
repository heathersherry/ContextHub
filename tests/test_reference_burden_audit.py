"""Offline counterexamples for unchanged downstream wire contracts."""
import json
from uuid import uuid4
from types import SimpleNamespace
import httpx
import pytest
from contexthub.services.change_location import ChangeLocator
from contexthub.models.knowledge import canonical_hash
from contexthub.propagation.candidate import ProposalData
from contexthub.propagation.verifier import parse_verdict
from s6_helpers import model_config, response, sample_data, verdict_payload
from knowledge_helpers import MemorySink, identity


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[False,True])
async def test_location_still_requires_exact_model_offsets(bad):
    sid,oldid,newid=map(str,(uuid4(),uuid4(),uuid4()))
    old=dict(context_id=sid,version=1,snapshot_id=oldid,text='old value')
    raw=dict(snapshot_id=newid,text='new value')
    def span(i,t):return dict(source_id=i,start=0,end=len(t),excerpt=t,provenance=['fixture'])
    item=dict(source_id=sid,version=1,old=span(oldid,'old value'),replacement=span(newid,'new value'),identity_basis=span(newid,'new value'),temporal_basis=span(newid,'new value'),relation='same_subject_same_attribute_replacement')
    if bad:item['replacement']['start']=1
    body=dict(status='located',matches=[item],reason='fixture');calls=[];cfg=model_config()
    def send(req):calls.append(req);return response(body)
    locator=ChangeLocator(cfg,api_key='synthetic',recorder=MemorySink(),transport=httpx.MockTransport(send))
    try:
        if bad:
            with pytest.raises(Exception,match='change_evidence_mismatch'):await locator.locate(raw,[old],identity=identity().model_copy(update={'config_sha256':canonical_hash(cfg)}))
        else:assert (await locator.locate(raw,[old],identity=identity().model_copy(update={'config_sha256':canonical_hash(cfg)}))).status=='located'
    finally:await locator.close()
    assert len(calls)==1


def test_candidate_and_verifier_still_reject_misquoted_reference():
    data=sample_data();raw=data.model_dump(mode='json');raw['claims'][0]['evidence'][0]['excerpt']='changed'
    with pytest.raises(ValueError):ProposalData.model_validate(raw)
    payload=verdict_payload(data);payload['applicability']['evidence'][0]['excerpt']='changed'
    with pytest.raises(ValueError):parse_verdict(json.dumps(payload),SimpleNamespace(data=data),SimpleNamespace(read=[SimpleNamespace(evidence=e) for e in data.claims[0].evidence]))
