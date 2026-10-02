import json
from uuid import uuid4
import hashlib
import pytest
import httpx
from pathlib import Path
from contexthub.discovery.extraction import parse_response
from contexthub.models.knowledge import ContractError
from contexthub.llm.request_parameters import RequestParameters
from discovery_helpers import MemoryDB, source, config, extract


def quoted(source_id, excerpt):
    return json.dumps({'propositions':[{'text':'claim','evidence':[{'source_id':str(source_id),'excerpt':excerpt,'provenance':['source']}]}]},ensure_ascii=False)


def test_exact_quote_unicode_and_no_normalization():
    sid=uuid4();text=' prefix 😀中文\\n "quoted" suffix '
    for q in ['😀中文','\\n','"quoted"',' prefix']:
        result=parse_response(quoted(sid,q),{sid:text},response_contract='unique-quote-v2')
        span=result.propositions[0].evidence[0]
        assert span.start==text.index(q) and text[span.start:span.end]==q
    with pytest.raises(ContractError,match='source_quote_not_found'):
        parse_response(quoted(sid,'\n'),{sid:text},response_contract='unique-quote-v2')


@pytest.mark.parametrize('text,excerpt,error', [('abc abc','abc','source_quote_ambiguous'),('aaa','aa','source_quote_ambiguous'),('A possible change','A definite change','source_quote_not_found'),('café','cafe\u0301','source_quote_not_found')])
def test_fail_closed(text,excerpt,error):
    sid=uuid4()
    with pytest.raises(ContractError,match=error):parse_response(quoted(sid,excerpt),{sid:text},response_contract='unique-quote-v2')


def test_no_other_source_or_model_supplied_positions():
    sid=uuid4()
    with pytest.raises(ContractError,match='undeclared_source'):parse_response(quoted(sid,'abc'),{uuid4():'abc'},response_contract='unique-quote-v2')
    raw=json.loads(quoted(sid,'abc'));raw['propositions'][0]['evidence'][0]['start']=0
    with pytest.raises(ValueError):parse_response(json.dumps(raw),{sid:'abc'},response_contract='unique-quote-v2')
    assert parse_response('{"propositions":[]}',{sid:'abc'},response_contract='unique-quote-v2').propositions==()


def audit_config(tmp_path):
    return config().model_copy(update={'response_contract':'unique-quote-v2','request_parameters':RequestParameters(output_limit_field='max_tokens',reasoning_effort=None,response_format='json_object',seed=dict(support='unknown',enabled=False),temperature_supported=True,response_archive_directory=str(tmp_path))})


@pytest.mark.asyncio
@pytest.mark.parametrize('text,excerpt,expected', [('prefix 中文😀 suffix','中文😀',None),('same same','same','source_quote_ambiguous'),('actual','invented','source_quote_not_found')])
async def test_pipeline_keeps_source_and_archives_failures(tmp_path,text,excerpt,expected):
    db=MemoryDB();ref=await source(db,text);raw=quoted(ref.id,excerpt)
    obj,sink,req=await extract(db,(ref,),[raw],cfg=audit_config(tmp_path))
    assert len(req)==1 and obj.failure_reason==expected
    assert obj.status==('pending' if expected else 'complete')
    record=next(x for x in sink.rows if x.phase=='finished' and x.requested_model)
    archive=Path(record.response_archive_path);body=json.loads(archive.read_text())
    assert json.loads(body['response_text'])['choices'][0]['message']['content']==raw
    assert record.response_archive_sha256==hashlib.sha256(archive.read_bytes()).hexdigest()
    assert record.usage.input_tokens==13 and record.cost.amount is None
    assert 'authorization' not in body and 'seed' not in req[0]
    if expected: assert record.stop_reason=='non_retryable' and not obj.propositions
    else: assert obj.propositions[0].evidence[0].excerpt==excerpt


@pytest.mark.asyncio
async def test_archive_write_failure_keeps_usage_and_never_retries(tmp_path):
    root=tmp_path/'not-directory';root.write_text('occupied')
    db=MemoryDB();ref=await source(db,'abc')
    obj,sink,requests=await extract(db,(ref,),[quoted(ref.id,'abc')],cfg=audit_config(root))
    assert obj.status=='pending' and len(requests)==1
    record=next(x for x in sink.rows if x.phase=='finished' and x.requested_model)
    assert record.usage.input_tokens==13 and record.stop_reason=='non_retryable'
    assert record.response_archive_path is None


def test_archive_prevalidation_and_secret_redaction(tmp_path):
    from contexthub.llm.response_archive import archive_response
    from discovery_helpers import call_identity
    ident=call_identity(config());key='test-secret-123'
    response=httpx.Response(200,text='invalid JSON '+key,headers={'Authorization':key,'set-cookie':key,'x-request-id':'request'})
    ref=archive_response(str(tmp_path),identity=ident,attempt_no=1,request={'model':'m','messages':[{'content':'echo '+key}]},response=response,api_key=key)
    body=Path(ref['response_archive_path']).read_text();assert key not in body
    data=json.loads(body);assert data['credential_redacted'] and data['response_text']=='invalid JSON [REDACTED]'
    assert data['response_headers']['x-request-id']=='request'
    assert 'authorization' not in data['response_headers'] and 'set-cookie' not in data['response_headers']
    assert Path(ref['response_archive_path']).stat().st_mode & 0o777==0o600
    with pytest.raises(FileExistsError):archive_response(str(tmp_path),identity=ident,attempt_no=1,request={},response=response,api_key=key)


@pytest.mark.asyncio
async def test_archive_exists_before_validator_runs(tmp_path, monkeypatch):
    import contexthub.discovery.extraction as module
    original=module.parse_response
    def validate(raw,sources,**kwargs):
        files=list(tmp_path.glob('*.json'))
        assert len(files)==1
        assert json.loads(json.loads(files[0].read_text())['response_text'])['choices'][0]['message']['content']==raw
        return original(raw,sources,**kwargs)
    monkeypatch.setattr(module,'parse_response',validate)
    db=MemoryDB();ref=await source(db,'abc')
    obj,sink,requests=await extract(db,(ref,),['not JSON'],cfg=audit_config(tmp_path))
    assert obj.failure_reason=='response_schema_invalid' and len(requests)==1
    assert any(x.response_archive_path for x in sink.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed_http', [True, False])
async def test_raw_http_failure_and_retry_have_separate_archives(tmp_path, malformed_http):
    from contexthub.llm.chat_client import OpenAIChatClient
    from contexthub.llm.retry_policy import RecordedCall
    from knowledge_helpers import MemorySink
    from discovery_helpers import call_identity
    cfg=audit_config(tmp_path);sink=MemorySink();sent=[]
    def handle(request):
        sent.append(request)
        if malformed_http:return httpx.Response(200,text='broken HTTP response JSON')
        if len(sent)==1:return httpx.Response(503,text='temporary unavailable')
        return httpx.Response(200,json={'model':'model','choices':[{'message':{'content':'ok'}}],'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}})
    async def no_sleep(_):pass
    client=OpenAIChatClient('secret-key',model='model',transport=httpx.MockTransport(handle),request_parameters=cfg.request_parameters)
    try:
        call=RecordedCall(call_identity(cfg),cfg.retry,sink,sleep=no_sleep)
        if malformed_http:
            with pytest.raises(Exception,match='response_json_invalid'):await client.complete('prompt',call=call)
        else:assert await client.complete('prompt',call=call)=='ok'
    finally:await client.close()
    assert len(sent)==(1 if malformed_http else 2)
    files=sorted(tmp_path.glob('*.json'));assert len(files)==len(sent)
    records=[r for r in sink.rows if r.phase=='finished'];assert all(r.response_archive_path for r in records)
    assert json.loads(files[0].read_text())['response_text']==('broken HTTP response JSON' if malformed_http else 'temporary unavailable')
    if not malformed_http:assert [r.attempt_no for r in records]==[1,2] and records[0].stop_reason=='retry_scheduled'
