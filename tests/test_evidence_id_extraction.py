import json
from uuid import uuid4
import pytest
from contexthub.discovery.evidence_units import evidence_unit_view, partition_source
from contexthub.discovery.extraction import parse_response
from contexthub.models.knowledge import ContractError
from discovery_helpers import MemoryDB, source, config, extract


def selected(*ids):
    return json.dumps({'propositions':[{'text':'claim','evidence_ids':ids}]})


@pytest.mark.parametrize('text',['','😀中文\\n<code>model1</code>\n end ','same\nsame\n','a'*1600,'\\'*513+'n', 'word '*500,'café cafe\u0301'])
def test_lossless_partition(text):
    parts=list(partition_source(text))
    assert ''.join(p[2] for p in parts)==text
    pos=0
    for start,end,value in parts:
        assert start==pos and end>start and end-start<=512
        assert text[start:end]==value
        pos=end
    assert pos==len(text)


def test_repeated_text_keeps_distinct_locations_and_sources():
    a,b=uuid4(),uuid4();texts={a:'same\nsame\n',b:'same\n'}
    view,registry=evidence_unit_view(texts)
    assert list(registry)==['E000001','E000002','E000003']
    result=parse_response(selected('E000002','E000003'),texts,response_contract='evidence-ids-v3')
    spans=result.propositions[0].evidence
    assert spans[0].source_id==a and spans[0].start==5
    assert spans[1].source_id==b and spans[1].start==0
    assert all(texts[x.source_id][x.start:x.end]==x.excerpt for x in spans)


@pytest.mark.parametrize('ids,error',[(('E999999',),'undeclared_evidence_id'),(('E000001','E000001'),'duplicate_evidence_id')])
def test_invalid_ids_fail_closed(ids,error):
    with pytest.raises(ContractError,match=error):parse_response(selected(*ids),{uuid4():'text'},response_contract='evidence-ids-v3')


def test_model_cannot_inject_excerpt_or_coordinates():
    for field,value in [('evidence',[{'excerpt':'fake'}]),('start',0),('source_id',str(uuid4()))]:
        data=json.loads(selected('E000001'));data['propositions'][0][field]=value
        with pytest.raises(ValueError):parse_response(json.dumps(data),{uuid4():'text'},response_contract='evidence-ids-v3')
    with pytest.raises(ValueError):parse_response(selected(),{uuid4():'text'},response_contract='evidence-ids-v3')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',[False,True])
async def test_service_uses_same_full_view_and_strict_original_refs(invalid):
    db=MemoryDB();text='prefix\\n<code>model1</code> 😀\n'+'long '*200
    ref=await source(db,text);cfg=config(response_contract='evidence-ids-v3',prompt_template='{sources_json}')
    obj,sink,requests=await extract(db,(ref,),[selected('E000001' if not invalid else 'E999999')],cfg=cfg)
    assert len(requests)==1
    payload=json.loads(requests[0]['messages'][0]['content'])
    assert ''.join(x['text'] for x in payload[0]['evidence_units'])==text
    assert 'text' not in payload[0] and payload[0]['snapshot']['id']==str(ref.id)
    if invalid:
        assert obj.status=='pending' and obj.failure_reason=='undeclared_evidence_id' and not obj.propositions
    else:
        assert obj.status=='complete'
        e=obj.propositions[0].evidence[0]
        assert e.excerpt==text[e.start:e.end]=='prefix\\n'
