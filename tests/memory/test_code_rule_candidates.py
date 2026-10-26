"""Code syntax is not a source of memory-rule candidates; prose stays anchored."""

import pytest

from contexthub.memory.jev_candidate_gate import candidate_text, rule_candidates


@pytest.mark.parametrize('code', [
    '```python\nif ready:\n    run()\n```',
    '~~~go\nif ready { run() }\n~~~',
    '````text\nIf the user moves, change gyms.\n```\n````',
    '<pre><code><span>if</span> ready: run()</code></pre>',
    '<code class="python">if ready: run()</code>',
    '`if ready: run()`',
    '``if ready: print(`example`)``',
    'if ready:\n    run()',
    'if ready:  # execute only when ready\n    run()',
    '    if res:\n        userid = res[0]',
    'if ready: run()',
    'return (rv[0] if rv else None) if one else rv',
    '[item for item in items if item.ready]',
    '  if item &gt; 10 {\n    finalResult = append(finalResult, item)\n  }',
])
def test_code_is_excluded_but_adjacent_prose_keeps_original_offsets(code):
    source = 'If I move, change gyms.\n' + code + '\nIf my shift changes, move the meeting.'
    candidates = rule_candidates([{'role': 'user', 'content': source}])
    assert len(candidates) == 2
    assert [source[c['if_start']:c['if_start'] + 16] for c in candidates] == [
        'If I move, chang', 'If my shift chan',
    ]
    for c in candidates:
        assert c['text'] == source[c['sentence_start']:c['sentence_start'] + len(c['text'])]
        assert source[c['if_start']:c['if_end']] == 'If'


def test_inline_code_does_not_remove_the_enclosing_natural_language_rule():
    source = 'If `if ready: run()` fails, alert the team.'
    candidates = rule_candidates([{'role': 'user', 'content': source}])
    assert len(candidates) == 1
    assert candidates[0]['text'] == source
    assert candidate_text(candidates[0]) == '<target>If</target> `if ready: run()` fails, alert the team.'


def test_unclosed_fence_excludes_remaining_code_only():
    source = 'If I move, change gyms.\n```python\nif ready: run()'
    candidates = rule_candidates([{'role': 'user', 'content': source}])
    assert len(candidates) == 1
    assert candidates[0]['if_start'] == 0


@pytest.mark.parametrize('source', [
    'If weight > 5.5 kg,\nuse freight.',
    'If the deployment platform changes, update deploy/Dockerfile.',
    'if hungry:\n    eat a sandwich.',
    'if ready:\n    tell the team.',
    'The callback returns true if the item is even and false if it is odd.',
])
def test_natural_language_including_code_explanations_is_left_to_semantic_gate(source):
    candidates = rule_candidates([{'role': 'user', 'content': source}])
    assert len(candidates) == (2 if 'callback' in source else 1)
