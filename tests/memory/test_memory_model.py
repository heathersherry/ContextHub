"""Memory model selection does not change the evaluation models."""

import json

import httpx
import pytest

from contexthub.llm.chat_client import OpenAIChatClient
from integrations.memebench.memory_research.cli import (
    argument_parser, freeze, memory_request_parameters,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('model,provider', [('gpt-6-luna', 'openlux'), ('deepseek-flash', 'deepseek')])
async def test_low_wire_and_fixed_evaluation_models(tmp_path, model, provider):
    args = argument_parser().parse_args([
        '--config', 'fixture', '--mode', 'check', '--episode', 'pl_001',
        '--output', str(tmp_path), '--memory-model', model, '--memory-provider', provider,
        '--rule-gate-backend', 'jev', '--p1-backend', 'jev',
        '--p2-direct-backend', 'jev',
    ])
    manifest = freeze(tmp_path / 'frozen', {'index': {'embedding_model': 'text-embedding-3-small', 'dimensions': 1536}}, args)
    models = manifest['model_parameters']
    assert manifest['memory_retry']['max_attempts'] == 5
    assert manifest['memory_retry']['timeout_seconds'] == 60
    assert manifest['memory_retry']['timeout_increment_seconds'] == 30
    assert manifest['p2_processing_retry'] == manifest['memory_retry']
    for role in ('retry', 'p1_retry', 'p2_direct_retry', 'rule_gate_retry'):
        assert manifest[role]['max_attempts'] == 3
        assert manifest[role]['timeout_increment_seconds'] == 0
    for role in ('extractor', 'p2'):
        assert models[role]['model'] == model
        assert models[role]['reasoning_effort'] == 'low'
    for role in ('rule_gate', 'p1', 'p2_direct'):
        assert models[role]['backend'] == 'jev'
    assert models['answer'] == {'model': 'gpt-4.1-mini', 'temperature': 0, 'max_tokens': 500}
    assert models['judge']['model'] == 'gpt-4o'

    def handler(request):
        assert json.loads(request.content) == {
            'model': model, 'reasoning_effort': 'low',
            'messages': [{'role': 'user', 'content': 'fixture'}],
        }
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"items":[]}'}}]})

    client = OpenAIChatClient(
        'fixture', model=args.memory_model,
        request_parameters=memory_request_parameters(args.memory_model),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.complete('fixture', max_tokens=None) == '{"items":[]}'
    finally:
        await client.close()


def test_default_memory_model():
    args = argument_parser().parse_args(['--config', 'fixture', '--mode', 'check', '--output', 'fixture'])
    assert args.memory_model == 'gpt-4.1-mini'
    assert args.memory_provider == 'openlux'
    assert memory_request_parameters(args.memory_model) is None


@pytest.mark.asyncio
async def test_deepseek_recorded_usage_and_full_response(tmp_path):
    from contexthub.llm.retry_policy import RecordedCall
    from knowledge_helpers import MemorySink, identity, policy

    response = {
        'model': 'deepseek-flash',
        'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"ok":true}', 'reasoning_content': 'fixture reasoning',
        }}],
        'usage': {
            'prompt_tokens': 41, 'completion_tokens': 23, 'total_tokens': 64,
            'prompt_tokens_details': {'cached_tokens': 32},
            'completion_tokens_details': {'reasoning_tokens': 17},
            'prompt_cache_hit_tokens': 32, 'prompt_cache_miss_tokens': 9,
        },
    }

    def handler(request):
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert request.headers['Authorization'] == 'Bearer fixture-deepseek-key'
        assert json.loads(request.content) == {
            'model': 'deepseek-flash', 'messages': [{'role': 'user', 'content': 'fixture'}],
            'reasoning_effort': 'low', 'max_tokens': 256,
        }
        return httpx.Response(200, json=response)

    params = memory_request_parameters('deepseek-flash').model_copy(update={
        'response_archive_directory': str(tmp_path),
    })
    sink = MemorySink()
    client = OpenAIChatClient(
        'fixture-deepseek-key', base_url='https://api.deepseek.com', model='deepseek-flash',
        request_parameters=params, transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.complete('fixture', max_tokens=256,
            call=RecordedCall(identity(), policy(), sink, capture_raw_output=True)) == '{"ok":true}'
    finally:
        await client.close()
    end = sink.rows[-1]
    assert end.usage.cached_input_tokens == 32
    assert end.usage.reasoning_tokens == 17
    assert end.usage.total_tokens == 64
    assert end.cost.amount is None and end.cost.reason == 'price_missing'
    archive = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert json.loads(archive['response_text']) == response
    assert 'fixture-deepseek-key' not in json.dumps(archive)
