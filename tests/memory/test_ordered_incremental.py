"""Source-order execution, ignored duplicates and incremental rule binding."""
import json
import pytest
from contexthub.memory.extractor import parse_extraction
from contexthub.memory.contracts import DirectDecision, split_ref
from .helpers import add, row, session, link, input_of, item, rule_item, forget_item, targets_response, bindings_response, pending
from .test_jev_direct import jev, plan_response  # noqa: F401

@pytest.mark.parametrize('extra', [{'order': 1}, {'candidate_ref': 'R1'}, {'type': 'fact'}, {'temporal_scope': None}])
def test_typed_list_rejects_obsolete_or_invalid_item_fields(extra):
    entry = dict(rule_item('If supplier changes, then delivery changes'), type='rule')
    entry.update(extra)
    with pytest.raises(ValueError):
        parse_extraction(json.dumps({'items': [entry]}), rules_allowed=True, forget_allowed=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('forget_first', [True, False])
async def test_rule_and_forget_follow_model_list_order(env, jev, forget_first):
    c1 = await session(env, 1)
    old_text = 'If supplier changes, then delivery is Tuesday.'
    new_text = 'If supplier changes, then delivery is Friday.'
    old = await add(env, c1, old_text, type='rule')
    request = 'Forget all supplier delivery rules.'
    entries = [forget_item(request), dict(rule_item(new_text), type='rule')]
    env.responses.extend([
        {'items': entries if forget_first else list(reversed(entries))},
        targets_response([old_text] if forget_first else [old_text, new_text]),
    ])
    await env.full_pipeline.ingest(env.trace, 2, [{'role': 'user', 'content':
        request + ' Remember this rule again: ' + new_text if forget_first else new_text + ' ' + request}])
    assert (await row(env, old))['status'] == 'deleted'
    usable = await env.retriever.current(env.trace, 2, purpose='policy')
    assert [r['text'] for r in usable] == ([new_text] if forget_first else [])
    assert not jev[1] and not env.responses

@pytest.mark.asyncio
async def test_existing_rule_only_judges_new_unlinked_fact_that_retrieved_it(env):
    c = await session(env, 1)
    rule = await add(env, c, 'If supplier changes, then delivery changes.', type='rule')
    source = await add(env, c, 'Supplier A')
    target = await add(env, c, 'Delivery Tuesday')
    await link(env, rule, sources=[source], targets=[target])
    c2 = await session(env, 2)
    incoming = await add(env, c2, 'Delivery location is the office')
    unrelated = await add(env, c2, 'An unrelated app request')
    def judge(payload):
        assert [r['text'] for r in input_of(payload)['candidates']] == ['Delivery location is the office']
        return bindings_response([], ['Delivery location is the office'])(payload)
    env.responses.append(judge)
    changed = await env.discovery.bind({incoming: {rule: await row(env, rule)}, unrelated: {}}, c2)
    assert changed == [rule]
    saved = await row(env, rule)
    assert saved['condition_matter_ids'] == [split_ref(source)[0]]
    assert set(saved['affected_matter_ids']) == {split_ref(target)[0], split_ref(incoming)[0]}
    count = len(env.prompts)
    assert await env.discovery.bind({incoming: {rule: saved}}, c2) == []
    assert len(env.prompts) == count and not env.responses

@pytest.mark.asyncio
async def test_revised_rule_version_initially_checks_previous_endpoint_candidates(env):
    c = await session(env, 1)
    text = 'If supplier changes, then delivery changes.'
    rule = await add(env, c, text, type='rule')
    source = await add(env, c, 'Supplier A')
    target = await add(env, c, 'Delivery Tuesday')
    await link(env, rule, sources=[source], targets=[target])
    c2 = await session(env, 2)
    revised, _ = await env.executor.apply_direct(pending(c2, 'If supplier changes, then delivery becomes Friday.', type='rule'),
        DirectDecision(action='update', target_ref=rule), c2)
    def judge(payload):
        assert {r['text'] for r in input_of(payload)['candidates']} == {'Supplier A', 'Delivery Tuesday'}
        return bindings_response(['Supplier A'], ['Delivery Tuesday'])(payload)
    env.responses.append(judge)
    await env.discovery.bind({revised: {}}, c2)
    assert (await row(env, revised))['condition_matter_ids'] == [split_ref(source)[0]]
    assert not env.responses

@pytest.mark.asyncio
async def test_duplicate_in_mixed_plan_does_not_write_or_protect_original(env, jev):
    c = await session(env, 1)
    original = await add(env, c, 'Home Beijing')
    before = dict(await row(env, original))
    c2 = await session(env, 2)
    focus = pending(c2, 'I still live in Beijing and work in Suzhou')
    env.responses.append(plan_response(('duplicate', 'Home Beijing', None), ('new', None, 'Work Suzhou')))
    keys, changes = await env.executor.process(focus, {original: before}, c2, intent='inform')
    assert len(keys) == 1 and (await row(env, keys[0]))['text'] == 'Work Suzhou'
    assert [t['operation'] for t in changes] == ['new']
    assert dict(await row(env, original)) == before
    assert (await env.store.sources(env.trace, *split_ref(original)))['session_seq'] == [1]
    assert not env.responses
