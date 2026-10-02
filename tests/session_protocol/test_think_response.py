"""Replay the observed provider response via MockTransport, never real APIs."""
import copy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.response_format import unwrap_think_json
from contexthub.llm.retry_policy import ResponseParseError
from contexthub.models.knowledge import text_hash
from contexthub.services.session_models import RecordedSessionModels, Verdict
from test_session_service import config
from test_trial_wiring import parameters, Sink

FIXTURE=Path(__file__).parent/'fixtures/trial-think-response.json'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_schema',[False,True])
async def test_archived_wrapper_preserves_raw_audits_conversion_and_still_checks_schema(tmp_path,invalid_schema):
    fixture=json.loads(FIXTURE.read_text());body=copy.deepcopy(fixture['original_http_response'])
    original=body['choices'][0]['message']['content']
    if invalid_schema:
        body['choices'][0]['message']['content']=original.split('</think>')[0]+'</think>\n{"supported":true}'
    raw=body['choices'][0]['message']['content'];requests=[]
    def provider(request):
        requests.append(json.loads(request.content));return httpx.Response(200,json=body)
    p=parameters().model_copy(update={'response_archive_directory':str(tmp_path/'responses')})
    cfg=config(tmp_path);cfg.models['verifier']=cfg.models['verifier'].model_copy(update={'request_parameters':p})
    sink=Sink();client=OpenAIChatClient('test-placeholder',model='offline',request_parameters=p,transport=httpx.MockTransport(provider))
    port=RecordedSessionModels(cfg,{'verifier':client},None,sink,'offline',uuid4())
    try:
        if invalid_schema:
            with pytest.raises(ValidationError):await port.ask('verifier','verify',{},Verdict,stage='P2')
        else:
            parsed=await port.ask('verifier','verify',{},Verdict,stage='P2')
            assert parsed==Verdict.model_validate_json(raw.split('</think>',1)[1])
        assert len(requests)==1 and len(sink.rows)==2
        record=sink.rows[-1]
        assert record.response_conversion.original_sha256==text_hash(raw)==record.output_sha256
        assert record.response_conversion.converted_sha256==text_hash(raw.split('</think>',1)[1].strip())
        assert record.response_conversion.rule=='leading-think-json-v1'
        assert record.outcome==('error' if invalid_schema else 'success')
        assert record.usage.total_tokens==9124  # Copied usage, not new spending.
        assert port.last_response['text']==raw
        assert port.last_response['conversion']['rule']=='leading-think-json-v1'
        archive=json.loads(Path(record.response_archive_path).read_text())
        assert json.loads(archive['response_text'])==body
    finally:await client.close()


@pytest.mark.parametrize('text',[
    '<think>unfinished {"supported":true}', '<think>nested <think>x</think>{}',
    '<think>x</think>prefix {}', '<think>x</think>{} suffix', '<think>x</think>{}{}',
    '<think>x</think>```json\n{}\n```', '<think>x</think>', '<think>x</think>[]',
    '<think>x</think><think>again</think>{}',
])
def test_does_not_salvage_incomplete_or_arbitrary_json(text):
    with pytest.raises(ResponseParseError):unwrap_think_json(text)


@pytest.mark.parametrize('text',['prefix <think>x</think>{}', 'prose {}', '{"text":"<think>literal</think>"}'])
def test_non_prefix_content_is_not_rewritten(text):
    assert unwrap_think_json(text)==(text,None)


def test_single_complete_prefix_whitespace_and_json_string_braces():
    raw=' \n<think>reason with {} braces</think>\n {"text":"} {"} \n'
    text,conversion=unwrap_think_json(raw)
    assert text=='{"text":"} {"}' and conversion.original_sha256==text_hash(raw)


@pytest.mark.asyncio
async def test_bad_wrapper_is_not_network_retried(tmp_path):
    cfg=config(tmp_path);model=cfg.models['verifier']
    cfg.models['verifier']=model.model_copy(update={'retry':model.retry.model_copy(update={
        'max_attempts':3,'backoff_seconds':(0.,0.),'retry_status_codes':(500,), 'retry_transport_errors':('ReadError',)})})
    calls=[]
    def provider(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'<think>unfinished'}}],
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
    sink=Sink();client=OpenAIChatClient('test-placeholder',model='offline',transport=httpx.MockTransport(provider))
    port=RecordedSessionModels(cfg,{'verifier':client},None,sink,'offline',uuid4())
    try:
        with pytest.raises(ResponseParseError):await port.ask('verifier','verify',{},Verdict,stage='P2')
        assert len(calls)==1 and sink.rows[-1].stop_reason=='non_retryable'
        assert port.last_response['text']=='<think>unfinished'
    finally:await client.close()
