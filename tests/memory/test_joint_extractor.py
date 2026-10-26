from .helpers import extraction_response
"""One full-session call, atomic output validation, and bounded rule sources."""
import json

import pytest

from contexthub.memory.extractor import ExtractionFailed, parse_extraction
from .helpers import input_of, item, rule_item


def output(rule=True):
    return extraction_response(rules=[dict(rule_item('If employer changes, then insurance changes'), candidate_ref='R1')] if rule else [], contents=[item('Employer changed to Acme')])


@pytest.mark.asyncio
@pytest.mark.parametrize('has_rule', [False, True])
async def test_one_call_reads_complete_chat_and_returns_rules_before_content(env, has_rule):
    result = output(has_rule)

    def extract(payload):
        data = input_of(payload)
        assert set(data) == {'messages', 'accepted_candidates', 'forget_candidate_messages'}
        assert len(data['messages']) == 2
        assert data['messages'][1]['content'] == 'My employer changed to Acme.'
        assert len(data['accepted_candidates']) == 1
        assert set(data['accepted_candidates'][0]) == {'ref', 'message_no', 'role', 'text'}
        return result

    env.responses.append(extract)
    extracted = await env.pipeline.ingest(env.trace, 1, [
        {'role': 'user', 'content': 'If employer changes, then insurance changes.' if has_rule else 'Ask if they have stock.'},
        {'role': 'user', 'content': 'My employer changed to Acme.'},
    ])
    assert [i.type for i in extracted] == (['rule', 'content'] if has_rule else ['content'])
    saved = json.loads((await env.store.get_session(env.trace, 1))['extraction_output'])
    assert [i['text'] for i in saved['items']] == [i.text for i in extracted]
    assert all('candidate_ref' not in i for i in saved['items'])
    assert len(env.prompts) == 1 and not env.responses


@pytest.mark.asyncio
async def test_bad_content_retries_whole_response_without_persisting_valid_rule(env):
    bad = output()
    del bad['contents'][0]['temporal_scope']
    env.responses.extend([bad] * 4)
    with pytest.raises(ExtractionFailed, match='joint'):
        await env.pipeline.ingest(env.trace, 1, [{'role': 'user', 'content': 'If employer changes, then insurance changes.'}])
    assert len(env.prompts) == 4 and not env.responses and not env.embedded
    assert not await env.store.current_rows(env.trace)
    async with env.store.pool.acquire() as conn:
        stages = await conn.fetch('SELECT stage FROM memory_steps WHERE tenant_id=$1 AND namespace=$2', *env.trace.args[:2])
    assert sum(r['stage'] == 'extraction.joint.validation_error' for r in stages) == 4
    assert not any(r['stage'] == 'extraction.parsed' for r in stages)


@pytest.mark.parametrize('bad', [
    {'items': []}, {'rules': []}, {'contents': []},
    {'rules': [], 'contents': []},  # The old two-array model contract is rejected.
    {'rules': [], 'contents': [], 'forget_requests': [], 'reason': 'extra'},
    {'rules': {}, 'contents': [], 'forget_requests': []},
    {'rules': [], 'contents': {}, 'forget_requests': []},
    {'rules': [], 'contents': [], 'forget_requests': {}},
    {'rules': [dict(rule_item('If A, then B'), candidate_ref='R99')], 'contents': [], 'forget_requests': []},
    {'rules': [rule_item('If A, then B')], 'contents': [], 'forget_requests': []},
    {'rules': [], 'contents': [dict(item('fact'), order=1, candidate_ref='R1')], 'forget_requests': []},
    {'rules': [], 'contents': [dict(item('fact'), order=1, temporal_scope='past')], 'forget_requests': []},
])
def test_joint_schema_rejects_partial_obsolete_or_unanchored_outputs(bad):
    with pytest.raises(ValueError):
        parse_extraction(json.dumps(bad), {'R1'})


def test_no_candidates_cannot_emit_rules():
    with pytest.raises(ValueError, match='candidate_ref'):
        parse_extraction(json.dumps(output()), set())
    parsed, warnings = parse_extraction(json.dumps(output(False)), set())
    assert len(parsed) == 1 and not warnings


@pytest.mark.asyncio
async def test_no_if_still_extracts_content_once_without_gate_or_old_memories(env):
    def extract(payload):
        data = input_of(payload)
        assert data['accepted_candidates'] == []
        assert 'CONFIRM BEFORE NORMALIZING' not in payload['messages'][0]['content']
        assert [m['message_no'] for m in data['messages']] == [1]
        return output(False)
    env.responses.append(extract)
    extracted = await env.pipeline.ingest(env.trace, 1, [{'role': 'user', 'content': 'My employer changed to Acme.'}])
    assert [i.type for i in extracted] == ['content']
    assert len(env.prompts) == 1
