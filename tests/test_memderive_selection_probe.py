import json

import pytest

from scripts.memory_research.selection_probe import candidate_union, parse_selection, selector_prompt


def memory(ref, text='A source assertion.'):
    return {'ref': ref, 'text': text, 'entities': ['Subject'], 'timestamp': '2026-01-01',
            'gold_answer': 'MUST_NOT_REACH_PROMPT', 'score': 99}


def test_union_keeps_candidate_lost_by_hybrid_cutoff_and_supplemental_channel():
    a, b, c = memory('a'), memory('b'), memory('c')
    result = candidate_union({'text': [{'memory': a}, {'memory': b}],
        'vector': [{'memory': a}], 'entity': [{'memory': c}], 'topic': [],
        'selected': [{'memory': a}]})
    assert result == [a, b, c]


def test_prompt_excludes_gold_and_does_not_rewrite_selected_memories():
    rows = [memory(str(i), f'Original assertion {i}.') for i in range(18)]
    prompt, labels = selector_prompt('What was booked?', rows)
    assert 'MUST_NOT_REACH_PROMPT' not in prompt
    refs = [f'M{i}' for i in range(18, 3, -1)]
    chosen = parse_selection(json.dumps({'selected_refs': refs}), labels)
    assert len(chosen) == 15
    assert all(row is labels[ref] for row, ref in zip(chosen, refs))
    assert chosen[0]['text'] == 'Original assertion 17.'


@pytest.mark.parametrize('refs', [['M1', 'M1'], ['M1', 'invented'], ['M1'], ['M1', 2]])
def test_rejects_duplicates_inventions_wrong_budget_and_nonstring_ids(refs):
    labels = {'M1': memory('a'), 'M2': memory('b')}
    with pytest.raises(ValueError):
        parse_selection(json.dumps({'selected_refs': refs}), labels)


def test_rejects_extra_answer_or_rewrite_fields():
    with pytest.raises(ValueError):
        parse_selection('{"selected_refs":["M1"],"answer":"invented"}', {'M1': memory('a')})
