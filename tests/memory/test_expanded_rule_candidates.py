"""Expanded prose hints and unchanged model contracts; all models are mocked."""

import json

import pytest

from contexthub.memory.contracts import rule_queries
from contexthub.memory.jev_candidate_gate import JevCandidateGate, rule_candidates
from integrations.memebench.memory_research.cli import decision_retry_policy
from .helpers import extraction_response, input_of, item, rule_item
from .test_jev_direct import Context
from .test_jev_rule_gate import gate_client


@pytest.mark.parametrize('text', [
    "Reimbursement claims are approved by the finance partner of the traveler's group.",
    "Registration for a term is valid only inside that term's enrollment window.",
    'Enrollment in CRT-210 requires completing CRT-101.',
    'Daily meal allowance caps claimable meal costs at 55 CR per day.',
    'Refund escalations are handled by the lead of the marketplace returns desk.',
    'Starting from Monday, orders above 900 CR must be confirmed.',
    'Items marked as gifts are excluded from household spend.',
    'APPROVED\nBY the finance partner is the required approval route.',
])
def test_non_if_policies_are_candidates_once_per_sentence(text):
    candidates = rule_candidates([{'role': 'user', 'content': text}])
    assert len(candidates) == 1
    assert candidates[0]['text'] == text
    assert text[candidates[0]['hint_start']:candidates[0]['hint_end']]


@pytest.mark.parametrize('text', [
    'This gift arrived yesterday.',
    'The capsule contains a policyholder identifier and a whitelisted name.',
    '`approved by` is a label.',
    '```text\nClaims must be approved by Finance.\n```',
    '<code>requires approval</code>',
    'An identifier `requires` is present.',
])
def test_substrings_and_code_only_hints_do_not_nominate_rules(text):
    assert rule_candidates([{'role': 'user', 'content': text}]) == []


def test_non_code_hint_after_inline_code_is_kept_with_original_anchor():
    source = '`only` is a token; deployment requires approval.'
    [candidate] = rule_candidates([{'role': 'user', 'content': source}])
    assert candidate['text'] == source
    assert source[candidate['hint_start']:candidate['hint_end']] == 'requires'


@pytest.mark.asyncio
async def test_gate_rejects_matching_fact_and_keeps_uncertain_policy_without_extra_questions():
    texts = [
        'Orders above 900 CR must be approved by Finance only after receipt review.',
        'The claim was approved by Finance yesterday.',
        'Draft policy: opened electronics are refundable.',
    ]
    requests, context = [], Context()
    client = gate_client({'R1': 'rule', 'R2': 'not_rule', 'R3': 'unclear'}, requests)
    try:
        selected = await JevCandidateGate(client, retry=decision_retry_policy('jev')).select(
            [{'role': 'user', 'content': text} for text in texts], context)
    finally:
        await client.close()
    assert len(requests) == 1
    assert [q['instructions']['candidate']['text'] for q in requests[0]['questions'].values()] == texts
    assert [c['ref'] for c in selected['rules']] == ['R1', 'R3']
    assert all(set(q['criteria']) == {'rule', 'not_rule', 'unclear'}
               for q in requests[0]['questions'].values())


@pytest.mark.asyncio
async def test_non_if_gate_to_full_session_extraction_preserves_existing_contract(env):
    sources = [
        'Enrollment in CRT-210 requires completing CRT-101.',
        'The claim was approved by Finance yesterday.',
    ]
    normalized = 'If enrollment in CRT-210 is to be permitted, then CRT-101 must already be completed.'
    requests = []
    client = gate_client({'R1': 'rule', 'R2': 'not_rule'}, requests)
    env.pipeline.extractor.candidate_gate = JevCandidateGate(client, retry=decision_retry_policy('jev'))

    def extract(payload):
        data = input_of(payload)
        assert [m['content'] for m in data['messages']] == sources
        assert data['accepted_candidates']['rules'] == [
            {'ref': 'R1', 'message_no': 1, 'role': 'user', 'text': sources[0]}]
        return extraction_response(
            rules=[rule_item(normalized)],
            contents=[dict(item(sources[1]), source_message_nos=[2])])

    env.responses.append(extract)
    try:
        extracted = await env.pipeline.ingest(
            env.trace, 1, [{'role': 'user', 'content': text} for text in sources])
    finally:
        await client.close()
    assert [(x.type, x.text) for x in extracted] == [('rule', normalized), ('content', sources[1])]
    assert rule_queries(extracted[0].text) == {
        'condition': 'enrollment in CRT-210 is to be permitted',
        'affected': 'CRT-101 must already be completed.',
    }
    saved = json.loads((await env.store.get_session(env.trace, 1))['extraction_output'])
    assert [x['source_message_nos'] for x in saved['items']] == [[1], [2]]
    assert len(requests) == len(env.prompts) == 1
    assert not env.responses
