from .helpers import rule_item
"""Source anchors, batched rule Choice, normalization boundaries and CLI wiring."""

import json
import shutil

import httpx
import pytest

from contexthub.llm.jev_client import JevClient
from contexthub.llm.retry_policy import ResponseSchemaError
from contexthub.memory import contracts
from contexthub.memory.jev_rule_gate import JevRuleGate, rule_candidates
from contexthub.memory.extractor import ExtractionFailed
from integrations.memebench.memory_research.cli import argument_parser, freeze, decision_retry_policy
from .test_jev_direct import Context
from .test_jev import result
from .helpers import input_of, item


def gate_client(labels, requests):
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert set(payload['questions']) == set(labels)
        return httpx.Response(200, json=result({ref: {
            'type': 'choice', 'choice': label, 'confidence': 1.,
            'probabilities': {k: float(k == label) for k in ('rule', 'not_rule', 'unclear')},
        } for ref, label in labels.items()}))
    return JevClient('fixture', transport=httpx.MockTransport(handler))


def test_sentence_candidates_preserve_each_if_and_original_source_offsets():
    messages = [
        {'role': 'system', 'content': 'If instructed, do this.'},
        {'role': 'user', 'content': 'A gift arrived. IF weight > 5.5 kg,\nuse freight; ask if it arrived.'},
        {'role': 'assistant', 'content': 'If the supplier changes, the schedule may change.'},
        {'role': 'tool', 'content': 'if x then y'},
    ]
    candidates = rule_candidates(messages)
    assert len(candidates) == 3
    assert [c['message_no'] for c in candidates] == [2, 2, 3]
    assert [c['ref'] for c in candidates] == ['R1', 'R2', 'R3']
    for c in candidates:
        source = messages[c['message_no'] - 1]['content']
        assert c['text'] == source[c['sentence_start']:c['sentence_start'] + len(c['text'])]
        assert source[c['if_start']:c['if_end']].lower() == 'if'
        assert c['text'][c['if_start'] - c['sentence_start']:c['if_end'] - c['sentence_start']].lower() == 'if'
    assert candidates[0]['if_start'] != candidates[1]['if_start']
    assert 'A gift arrived' not in candidates[0]['text']
    assert candidates[0]['text'] == candidates[1]['text']
    assert '5.5 kg,\nuse freight' in candidates[0]['text']


@pytest.mark.parametrize('sentence', [
    'If Dr. Lee changes providers, rotate the credentials.',
    'If the limit exceeds 5.5 kg, use freight.',
    'If https://example.com/a changes, update deploy/Dockerfile.zephyra.',
    'If that changes,\nthen stop using it.',
])
def test_sentence_boundary_keeps_abbreviations_paths_and_wrapped_consequences(sentence):
    source = 'Background information. ' + sentence + ' More background.'
    candidates = rule_candidates([{'role': 'user', 'content': source}])
    assert len(candidates) == 1
    assert candidates[0]['text'] == sentence


def test_identical_sentences_keep_distinct_source_anchors():
    sentence = 'Let me know if you have questions.'
    source = sentence + ' Another sentence. ' + sentence
    candidates = rule_candidates([{'role': 'user', 'content': source}])
    assert [c['text'] for c in candidates] == [sentence, sentence]
    assert len({c['if_start'] for c in candidates}) == 2


@pytest.mark.asyncio
async def test_gate_sends_only_sentences_and_shared_policy_and_forwards_unclear(tmp_path, monkeypatch):
    rubric = json.loads((contracts.PROMPTS / 'extractor_rule_jev.json').read_text())
    rubric['instructions'] += '\nFrozen gate marker'
    (tmp_path / 'extractor_rule_jev.json').write_text(json.dumps(rubric))
    monkeypatch.setattr(contracts, 'PROMPTS', tmp_path)
    messages = [
        {'role': 'assistant', 'content': 'If weight exceeds 5 kg, use freight.'},
        {'role': 'user', 'content': 'Yes, use that policy. Ask if the parcel arrived. If that changes, adjust it.'},
    ]
    requests, context = [], Context()
    client = gate_client({'R1': 'rule', 'R2': 'not_rule', 'R3': 'unclear'}, requests)
    try:
        selected = await JevRuleGate(client, retry=decision_retry_policy('jev')).select(messages, context)
    finally:
        await client.close()
    assert [c['ref'] for c in selected] == ['R1', 'R3']
    assert len(requests) == 1
    assert requests[0]['state'] == {'rule_policy': rubric['instructions']}
    assert json.dumps(requests[0]).count('Frozen gate marker') == 1
    assert 'Yes, use that policy.' not in json.dumps(requests[0])
    assert [q['instructions']['candidate']['text'] for q in requests[0]['questions'].values()] == [
        'If weight exceeds 5 kg, use freight.',
        'Ask if the parcel arrived.',
        'If that changes, adjust it.',
    ]
    for question in requests[0]['questions'].values():
        c = question['instructions']['candidate']
        assert set(c) == {'role', 'text', 'if_start', 'if_end'}
        assert c['text'][c['if_start']:c['if_end']].lower() == 'if'
        assert 'state.rule_policy' in question['instructions']['question']
    assert context.steps[-1][1]['answers']['R3']['choice'] == 'unclear'
    assert context.steps[-1][1]['selected_refs'] == ['R1', 'R3']


@pytest.mark.asyncio
async def test_no_if_makes_no_jev_request():
    requests, context = [], Context()
    client = gate_client({}, requests)
    try:
        selected = await JevRuleGate(client, retry=decision_retry_policy('jev')).select(
            [{'role': 'user', 'content': 'This gift is beautiful.'}], context)
    finally:
        await client.close()
    assert not selected and not requests and not context.sink.records


@pytest.mark.asyncio
async def test_normalization_only_accepts_selected_refs_then_content_sees_clean_rules(env, tmp_path, monkeypatch):
    for name in ['extractor_rule_jev.json', 'extractor_rules_normalize.txt', 'extractor_facts.txt']:
        shutil.copy2(contracts.PROMPTS / name, tmp_path / name)
    normalize = tmp_path / 'extractor_rules_normalize.txt'
    normalize.write_text(normalize.read_text() + '\nFrozen normalization marker')
    monkeypatch.setattr(contracts, 'PROMPTS', tmp_path)
    requests = []
    client = gate_client({'R1': 'unclear', 'R2': 'not_rule'}, requests)
    env.pipeline.extractor.rule_gate = JevRuleGate(client, retry=decision_retry_policy('jev'))
    text = "If the user's supplier changes, their delivery schedule may change."
    def normalization(payload):
        assert 'Frozen normalization marker' in payload['messages'][0]['content']
        data = input_of(payload)
        assert [c['ref'] for c in data['accepted_candidates']] == ['R1']
        assert len(data['messages']) == 3  # pronoun source and full follow-up retained
        assert data['messages'][0]['content'] == 'Our supplier is Oak.'
        assert data['accepted_candidates'][0]['text'] == 'If that changes, our delivery schedule may change.'
        assert 'CONFIRM BEFORE NORMALIZING' in payload['messages'][0]['content']
        return {'items': [dict(rule_item(text), candidate_ref='R2')]}
    def corrected(payload):
        assert 'candidate_ref must name a Jev-accepted source candidate' in payload['messages'][0]['content']
        return {'items': [dict(rule_item(text), candidate_ref='R1')]}
    def facts(payload):
        data = input_of(payload)
        assert data['rule_output'] == {'items': [rule_item(text)]}
        assert len(data['messages']) == 3
        return {'items': [item('Supplier Oak')]}
    env.responses.extend([normalization, corrected, facts])
    try:
        extracted = await env.pipeline.ingest(env.trace, 1, [
            {'role': 'user', 'content': 'Our supplier is Oak.'},
            {'role': 'user', 'content': 'If that changes, our delivery schedule may change.'},
            {'role': 'user', 'content': 'Ask if the parcel arrived.'},
        ])
    finally:
        await client.close()
    assert [(i.type, i.text) for i in extracted] == [('rule', text), ('content', 'Supplier Oak')]
    assert all(set(i.__dict__) == {'type', 'text', 'entities', 'temporal_scope', 'topics'} for i in extracted)
    assert len(requests) == 1 and len(env.prompts) == 3 and not env.responses


@pytest.mark.asyncio
async def test_not_rule_skips_normalizer_but_preserves_content_requests(env):
    requests = []
    client = gate_client({'R1': 'not_rule'}, requests)
    env.pipeline.extractor.rule_gate = JevRuleGate(client, retry=decision_retry_policy('jev'))
    def facts(payload):
        data = input_of(payload)
        assert data['rule_output'] == {'items': []}
        assert data['messages'][0]['content'].startswith('Forget')
        return {'items': [item('Forget the freight rule')]}
    env.responses.append(facts)
    try:
        extracted = await env.pipeline.ingest(env.trace, 1, [
            {'role': 'user', 'content': 'Forget the rule: if weight exceeds 5 kg, use freight.'},
        ])
    finally:
        await client.close()
    assert [i.type for i in extracted] == ['content']
    assert len(env.prompts) == 1 and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('label', ['rule', 'unclear'])
async def test_normalizer_can_reject_forwarded_proposal_using_full_followup(env, label):
    requests = []
    client = gate_client({'R1': label}, requests)
    env.pipeline.extractor.rule_gate = JevRuleGate(client, retry=decision_retry_policy('jev'))
    messages = [
        {'role': 'assistant', 'content': 'If the provider changes, rotate the credentials.'},
        {'role': 'user', 'content': 'No, do not adopt that policy.'},
    ]
    def normalize(payload):
        data = input_of(payload)
        assert [{k: m[k] for k in ('role', 'content')} for m in data['messages']] == messages
        assert [c['ref'] for c in data['accepted_candidates']] == ['R1']
        return {'items': []}
    def facts(payload):
        data = input_of(payload)
        assert data['rule_output'] == {'items': []}
        assert len(data['messages']) == 2
        return {'items': []}
    env.responses.extend([normalize, facts])
    try:
        extracted = await env.pipeline.ingest(env.trace, 1, messages)
    finally:
        await client.close()
    assert not extracted
    assert len(requests) == 1 and len(env.prompts) == 2 and not env.responses
    assert 'No, do not adopt' not in json.dumps(requests[0])


@pytest.mark.asyncio
async def test_gate_failure_does_not_fall_back_or_continue_extraction(env):
    client = JevClient('fixture', transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=result({}))))
    env.pipeline.extractor.rule_gate = JevRuleGate(client, retry=decision_retry_policy('jev'))
    try:
        with pytest.raises(ResponseSchemaError):
            await env.pipeline.ingest(env.trace, 1, [
                {'role': 'user', 'content': 'If supplier changes, schedule changes.'}])
    finally:
        await client.close()
    assert not env.prompts and not env.embedded


@pytest.mark.asyncio
async def test_invented_normalization_sources_exhaust_retries_without_new_gate_call(env):
    requests = []
    client = gate_client({'R1': 'rule'}, requests)
    env.pipeline.extractor.rule_gate = JevRuleGate(client, retry=decision_retry_policy('jev'))
    env.responses.extend([{'items': [dict(rule_item('Invented'), candidate_ref='R99')]}] * 4)
    try:
        with pytest.raises(ExtractionFailed, match='rules'):
            await env.pipeline.ingest(env.trace, 1, [
                {'role': 'user', 'content': 'If supplier changes, schedule changes.'}])
    finally:
        await client.close()
    assert len(requests) == 1 and len(env.prompts) == 4 and not env.embedded


def test_rule_gate_cli_default_and_frozen_metadata(tmp_path):
    parser = argument_parser()
    common = ['--config', 'fixture', '--mode', 'check', '--output', str(tmp_path)]
    assert parser.parse_args(common).rule_gate_backend == 'llm'
    args = parser.parse_args(common + ['--rule-gate-backend', 'jev'])
    assert args.p1_backend == args.p2_direct_backend == 'llm'
    manifest = freeze(tmp_path / 'frozen', {'index': {'embedding_model': 'text-embedding-3-small', 'dimensions': 1536}}, args)
    assert manifest['model_parameters']['rule_gate']['model'] == 'jev-1.13.0'
    assert manifest['extraction']['prompts'] == ['extractor_rule_jev.json', 'extractor_rules_normalize.txt', 'extractor_facts.txt']
    assert manifest['model_parameters']['extractor']['model'] == 'gpt-4.1-mini'
    for name in manifest['extraction']['prompts']:
        assert name in manifest['prompt_sha256']
        assert (tmp_path / 'frozen' / 'prompts' / name).read_bytes() == (contracts.PROMPTS / name).read_bytes()


@pytest.mark.asyncio
async def test_one_accepted_candidate_can_normalize_independent_rules(env):
    client = gate_client({'R1': 'rule'}, [])
    env.pipeline.extractor.rule_gate = JevRuleGate(client, retry=decision_retry_policy('jev'))
    texts = ['If rainy, venue is indoors.', 'If the host is late, start at 3pm.']
    env.responses.extend([
        {'items': [dict(rule_item(text), candidate_ref='R1') for text in texts]},
        {'items': []},
    ])
    try:
        extracted = await env.pipeline.ingest(env.trace, 1, [
            {'role': 'user', 'content': 'If either event occurs, act independently: rain moves the venue indoors; host lateness moves the start to 3pm.'},
        ])
    finally:
        await client.close()
    assert [r.text for r in extracted] == texts
    assert all(r.type == 'rule' for r in extracted)
