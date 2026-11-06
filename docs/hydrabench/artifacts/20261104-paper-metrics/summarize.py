"""Compute paper-style trajectory diagnostics from the frozen 20 evaluated runs."""
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

root = Path.cwd()
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
run_dir = root / 'docs/hydrabench/runs/20261104-mixed-qwen-thinking-agents'
reference = json.loads((root / 'docs/hydrabench/artifacts/20261104-mixed-qwen/evaluation.json').read_text())
semantic = json.loads((root / 'docs/hydrabench/artifacts/20261104-semantic-review/review.json').read_text())
case_ids = [r['case_id'] for r in reference['records']]
records = []

def decode(value):
    if isinstance(value, dict) and 'content' in value:
        value = value['content']
    if isinstance(value, str):
        return json.loads(value)
    return value

for cid in case_ids:
    path = run_dir / f'{cid}.json'
    trace = json.loads(path.read_text())
    starts = [e for e in trace['tool_events'] if e['event'] == 'on_tool_start']
    ends = {e['run_id']: e for e in trace['tool_events'] if e['event'] == 'on_tool_end'}
    assert len(starts) == len({e['run_id'] for e in starts}) == len(ends)
    calls = []
    for e in starts:
        name = e['name']
        args = e['data']['input']
        if name == 'contexthub_search':
            kind = {'table_schema': 'schema_search', 'resource': 'text_search'}[args['context_type']]
        elif name == 'contexthub_read':
            kind = 'context_read'
        else:
            assert name == 'run_sql', name
            kind = 'sql'
        result = decode(ends[e['run_id']]['data']['output'])
        calls.append({'kind': kind, 'name': name, 'run_id': e['run_id'], 'input': args,
                      'error': result.get('error'), 'started_seconds': e['seconds'],
                      'finished_seconds': ends[e['run_id']]['seconds']})
    attempted = Counter(c['kind'] for c in calls)
    successful = Counter(c['kind'] for c in calls if not c['error'])
    records.append({'case_id': cid, 'type': trace['type'], 'status': trace['status'],
                    'tool_attempts': len(calls), 'non_error_tool_calls': sum(successful.values()),
                    'calls_by_kind': dict(attempted), 'non_error_calls_by_kind': dict(successful),
                    'uses_sql_and_text_search': bool(attempted['sql'] and attempted['text_search']),
                    'has_non_error_sql_and_text_search': bool(successful['sql'] and successful['text_search']),
                    'has_final_answer': bool(trace['final_answer'].strip()),
                    'elapsed_seconds': trace['elapsed_seconds'], 'calls': calls,
                    'trace_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

def summarize(rows):
    n = len(rows)
    total = Counter()
    successful = Counter()
    histogram = Counter()
    for r in rows:
        total.update(r['calls_by_kind'])
        successful.update(r['non_error_calls_by_kind'])
        histogram[str(r['tool_attempts']) if r['tool_attempts'] < 7 else '7+'] += 1
    return {'cases': n, 'attempted_tool_calls': sum(total.values()),
            'non_error_tool_calls': sum(successful.values()),
            'attempted_calls_by_kind': dict(total), 'non_error_calls_by_kind': dict(successful),
            'mean_tool_attempts': sum(total.values())/n,
            'mean_non_error_tool_calls': sum(successful.values())/n,
            'median_tool_attempts': statistics.median(r['tool_attempts'] for r in rows),
            'min_tool_attempts': min(r['tool_attempts'] for r in rows),
            'max_tool_attempts': max(r['tool_attempts'] for r in rows),
            'exactly_one_tool_call_cases': [r['case_id'] for r in rows if r['tool_attempts'] == 1],
            'tool_attempt_distribution_counts': {k: histogram[k] for k in ['0','1','2','3','4','5','6','7+']},
            'tool_attempt_distribution_percent': {k: 100*histogram[k]/n for k in ['3','4','5','6','7+']},
            'both_sql_and_text_count': sum(r['uses_sql_and_text_search'] for r in rows),
            'both_sql_and_text_percent': 100*sum(r['uses_sql_and_text_search'] for r in rows)/n,
            'both_non_error_sql_and_text_count': sum(r['has_non_error_sql_and_text_search'] for r in rows),
            'final_answer_count': sum(r['has_final_answer'] for r in rows),
            'mean_episode_seconds': statistics.mean(r['elapsed_seconds'] for r in rows),
            'median_episode_seconds': statistics.median(r['elapsed_seconds'] for r in rows)}

summary = summarize(records)
assert len(records) == 20
assert summary['attempted_calls_by_kind'] == {'schema_search': 40, 'sql': 56, 'text_search': 43, 'context_read': 1}
assert summary['attempted_tool_calls'] == 140 and summary['non_error_tool_calls'] == 139
assert summary['both_sql_and_text_count'] == 19
metrics = {
    'method': 'Deterministic extraction from the existing one-run-per-case traces; no new model runs or judging.',
    'paper_source': {'file': '/Users/sherrylin/Documents/PythonProjects/ContextHub/2952_Bridging_Databases_and_Do.pdf',
                     'sha256': hashlib.sha256(Path('/Users/sherrylin/Documents/PythonProjects/ContextHub/2952_Bridging_Databases_and_Do.pdf').read_bytes()).hexdigest(),
                     'sections': ['5.1', '5.2', '5.3', '7.3', '7.4'], 'tables': [2,3,5], 'figure': 5},
    'definitions': {
        'Acc_avg': 'For N questions and K independent runs: sum_{i=1..N,r=1..K} correct[i,r] / (N*K). Paper uses K=8.',
        'Best_at_k': 'Fraction of questions with at least one correct answer in k independent runs. Not a deployed selector success rate and not k tool calls within one episode.',
        'correctness': 'Paper uses GPT-o4-mini semantic judgment against reference natural-language answers. Current 13/20 uses a local coding-assistant review, not that judge.',
        'tool_calls': 'Count each on_tool_start once, including parallel calls separately and the rejected 11th text search. Non-error counts are reported separately. All 20 evaluated attempts, including no-answer dev_35, remain in denominator.',
        'both_tools': 'At least one run_sql and one contexthub_search with context_type=resource. Schema-only search does not count as text retrieval. Presence does not prove correctness or dependency.',
        'comparison_limit': 'Paper uses run_sql for schema discovery as well as data queries. Our schema search/read calls must not simply be removed to make the total look comparable; APIs and fixtures differ.',
    },
    'correctness_available': {'local_single_run_reference_agreement': {'correct':13,'total':20,'rate':0.65},
                              'local_single_run_by_type': reference['summary']['by_type'],
                              'paper_GPT_o4_mini_judgment': None,
                              'Acc_avg_over_8_runs': None, 'Best_at_2': None, 'Best_at_4': None, 'Best_at_8': None,
                              'semantic_review_outcomes': semantic['summary']['mutually_exclusive_outcomes']},
    'summary': summary,
    'by_type': {t: summarize([r for r in records if r['type'] == t]) for t in ['a2s','s2a']},
    'paper_Qwen3_8B_TRACE_GRPO_context_only': {'Acc_avg':0.5391,'Best_at_1':0.5374,'Best_at_2':0.6189,'Best_at_4':0.6830,'Best_at_8':0.7289,'mean_tool_calls':4.72,'both_tools_percent':94.1,'call_distribution_percent':{'3':6.5,'4':41.6,'5':32.6,'6':13.4,'7+':5.7}},
    'not_available_or_not_applicable': ['Independent human-judge agreement study', 'RL hint curriculum evolution/convergence', 'MuSiQue 3-/4-hop generalization'],
    'records': records,
}
(output/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'summary':summary,'by_type':metrics['by_type']},indent=2))
