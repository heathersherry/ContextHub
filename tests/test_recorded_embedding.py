import asyncio
import json
import httpx
import pytest
from knowledge_helpers import MemorySink,identity,policy
from contexthub.llm.recorded_embedding import EmbeddingConfig,RecordedEmbeddingClient
from contexthub.llm.retry_policy import RecordedCall,ResponseSchemaError
from contexthub.models.knowledge import canonical_hash,ContractError


def setup(tmp_path,handle):
    cfg=EmbeddingConfig(model='embedding-fixture',base_url='https://fixture.invalid/v1',
        dimensions=2,send_dimensions=True,max_batch=2,response_archive_directory=str(tmp_path),retry=policy())
    sink=MemorySink(); call=RecordedCall(identity().model_copy(update={'config_sha256':canonical_hash(cfg)}),cfg.retry,sink)
    return RecordedEmbeddingClient(cfg,api_key='TEST-SECRET',transport=httpx.MockTransport(handle)),call,sink


def response(request,rows=None):
    return httpx.Response(200,request=request,json={'data':rows if rows is not None else [
        {'index':1,'embedding':[0.,1.]},{'index':0,'embedding':[1.,0.]}],
        'usage':{'prompt_tokens':7,'total_tokens':7}})


@pytest.mark.asyncio
async def test_order_usage_archive_and_no_model_invention(tmp_path):
    def handle(req):
        assert json.loads(req.content)['dimensions']==2
        return response(req)
    client,call,sink=setup(tmp_path,handle)
    try:
        assert await client.embed_batch(('a','b'),call=call)==((1.,0.),(0.,1.))
        last=sink.rows[-1]
        assert last.usage.input_tokens==7 and last.usage.output_tokens is None
        assert last.usage.completeness=='partial' and last.cost.amount is None
        assert last.actual_model is None
        assert len(list(tmp_path.glob('*.json')))==1
        assert 'TEST-SECRET' not in next(tmp_path.glob('*.json')).read_text()
    finally: await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('rows',[
    [{'index':0,'embedding':[1.,0.]},{'index':0,'embedding':[0.,1.]}],
    [{'index':0,'embedding':[1.]},{'index':1,'embedding':[0.,1.]}],
    [{'index':0,'embedding':[False,1.]},{'index':1,'embedding':[0.,1.]}],
    [{'index':0,'embedding':[0.,0.]},{'index':1,'embedding':[0.,1.]}]])
async def test_schema_failure_has_usage_and_archive_no_retry(tmp_path,rows):
    client,call,sink=setup(tmp_path,lambda req:response(req,rows))
    try:
        with pytest.raises(ResponseSchemaError): await client.embed_batch(('a','b'),call=call)
        assert len(sink.rows)==2 and sink.rows[-1].stop_reason=='non_retryable'
        assert sink.rows[-1].usage.input_tokens==7 and sink.rows[-1].response_archive_path
    finally: await client.close()


@pytest.mark.asyncio
async def test_bounded_retry_no_outer_resend(tmp_path):
    sent=[]
    def handle(req):
        sent.append(req);return httpx.Response(503,request=req,json={})
    client,call,sink=setup(tmp_path,handle)
    try:
        with pytest.raises(httpx.HTTPStatusError): await client.embed_batch(('a',),call=call)
        assert len(sent)==3 and sink.rows[-1].stop_reason=='retry_exhausted'
        with pytest.raises(ContractError): await client.embed_batch(('a',),call=call)
        assert len(sent)==3
    finally: await client.close()


@pytest.mark.asyncio
async def test_cancel_and_reservation_failure(tmp_path):
    async def handle(req): raise asyncio.CancelledError()
    client,call,sink=setup(tmp_path,handle)
    try:
        with pytest.raises(asyncio.CancelledError): await client.embed_batch(('a',),call=call)
        assert sink.rows[-1].outcome=='cancelled' and sink.rows[-1].cost.amount is None
    finally: await client.close()


@pytest.mark.asyncio
async def test_archive_failure_preserves_usage_no_resend(tmp_path):
    client,call,sink=setup(tmp_path,lambda req:response(req))
    (tmp_path/f'{call.identity.call_id}-1.json').write_text('preserve')
    try:
        with pytest.raises(FileExistsError): await client.embed_batch(('a','b'),call=call)
        assert len(sink.rows)==2 and sink.rows[-1].usage.input_tokens==7
        assert sink.rows[-1].stop_reason=='non_retryable'
    finally: await client.close()
