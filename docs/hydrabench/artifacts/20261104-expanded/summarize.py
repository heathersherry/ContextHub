import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CASES = {c['id']: c for c in json.loads((ROOT/'data/hydrabench-expanded/selected-private.json').read_text())}
LABELS = json.loads((ROOT/'data/hydrabench-expanded/text-support-labels.json').read_text())['labels']

def decode(event):
    value = event['data']['output']
    if isinstance(value, dict) and 'content' in value:
        return json.loads(value['content'])
    return value

records = []
for cid, case in CASES.items():
    trace = json.loads((ROOT/f'docs/hydrabench/runs/20261104-expanded-agents/{cid}.json').read_text())
    starts = {e['run_id']: e for e in trace['tool_events'] if e['event']=='on_tool_start'}
    searches = {'table_schema': [], 'resource': []}
    sql = []
    for event in trace['tool_events']:
        if event['event']!='on_tool_end':
            continue
        args = starts[event['run_id']]['data']['input']
        result = decode(event)
        if event['name']=='contexthub_search':
            searches[args['context_type']].append({'query': args['query'], 'uris': [h['uri'] for h in result.get('results', [])], 'started_seconds': starts[event['run_id']]['seconds'], 'finished_seconds': event['seconds'], 'error': result.get('error')})
        elif event['name']=='run_sql':
            sql.append({'query':args['code_query'], 'result':result, 'seconds':event['seconds']})
    for values in searches.values():
        values.sort(key=lambda s:s['started_seconds'])
    expected = {f"ctx://datalake/hydra/{case['db_id']}/{t}".casefold() for t in case['reference_tables']}
    table = searches['table_schema']
    first = {u.casefold() for u in table[0]['uris']} if table else set()
    union = {u.casefold() for s in table for u in s['uris']}
    anchors = set(LABELS[cid]['known_support_uris'])
    resource = searches['resource']
    text_union = {u for s in resource for u in s['uris']}
    row = {'case_id':cid, 'type':case['type'], 'db_id':case['db_id'], 'status':trace['status'], 'error':trace.get('error'), 'elapsed_seconds':trace['elapsed_seconds'],
           'required_tables':case['reference_tables'], 'required_count':len(expected),
           'first_table_hits':len(expected & first), 'cumulative_table_hits':len(expected & union),
           'all_tables_first':expected <= first, 'all_tables_cumulative':expected <= union,
           'missing_reference_tables':sorted(expected-union),
           'known_text_anchor_first':bool(anchors & set(resource[0]['uris'])) if resource else False,
           'known_text_anchor_cumulative':bool(anchors & text_union),
           'text_coverage':LABELS[cid]['coverage'], 'searches':searches,
           'sql_calls':sql, 'final_answer':trace.get('final_answer'), 'reference_answer':case['final_answer'],
           'reference_sql':case['sql_query'], 'reference_sql_output':case['sql_output'],
           'database_unchanged':trace['database_sha256']==trace['database_sha256_after']}
    records.append(row)

complete = [r for r in records if r['status']=='completed']
summary = {'selected':len(records), 'completed':len(complete), 'provider_errors':len(records)-len(complete),
           'completed_a2s':sum(r['type']=='a2s' for r in complete), 'completed_s2a':sum(r['type']=='s2a' for r in complete),
           'completed_required_tables':sum(r['required_count'] for r in complete),
           'completed_first_table_hits':sum(r['first_table_hits'] for r in complete),
           'completed_cumulative_table_hits':sum(r['cumulative_table_hits'] for r in complete),
           'completed_all_tables_first':sum(r['all_tables_first'] for r in complete),
           'completed_all_tables_cumulative':sum(r['all_tables_cumulative'] for r in complete),
           'completed_known_text_anchor_cumulative':sum(r['known_text_anchor_cumulative'] for r in complete),
           'database_unchanged_all':all(r['database_unchanged'] for r in records)}
(Path(__file__).parent/'agent-analysis.json').write_text(json.dumps({'summary':summary,'records':records},ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))

probe_records = []
for cid in CASES:
    p = json.loads((ROOT/f'docs/hydrabench/runs/20261104-expanded-probes/{cid}.json').read_text())
    found = {h['uri'] for h in p['resource']['response']['results']}
    probe_records.append({'case_id':cid, 'table_metrics':p['table_metrics'],
        'known_text_anchor_hit_at_3':bool(found & set(LABELS[cid]['known_support_uris'])),
        'text_coverage':LABELS[cid]['coverage']})
probe_summary = {
    'table_hits':sum(r['table_metrics']['hits_at_3'] for r in probe_records),
    'required_tables':sum(r['table_metrics']['required'] for r in probe_records),
    'all_required_at_3_cases':sum(r['table_metrics']['all_required_at_3'] for r in probe_records),
    'cases':len(probe_records),
    'known_text_anchor_hit_cases':sum(r['known_text_anchor_hit_at_3'] for r in probe_records),
    'text_covered_cases':sum(bool(LABELS[r['case_id']]['known_support_uris']) for r in probe_records),
    'results':probe_records}
(Path(__file__).parent/'probe-summary.json').write_text(json.dumps(probe_summary,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in probe_summary.items() if k!='results'},indent=2))
