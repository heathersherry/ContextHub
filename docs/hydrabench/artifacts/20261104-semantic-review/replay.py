"""Read-only, no-model evidence checks for the post-hoc 20-case semantic review."""
import hashlib
import json
import sys
from pathlib import Path

from integrations.hydrabench.sqlite_tool import run_readonly_sql

root = Path.cwd()
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
analysis_path = root / 'docs/hydrabench/artifacts/20261104-mixed-qwen/analysis.json'
records = json.loads(analysis_path.read_text())['records']
index_path = root / 'data/hydrabench-expanded/index/index-records.jsonl'
index = {d['uri']: d for d in map(json.loads, index_path.open())}
dbs = {r['db_id']: root / f"data/hydrabench-expanded/databases/{r['db_id']}/{r['db_id']}.sqlite" for r in records}

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

before = {k: sha(p) for k, p in dbs.items()}
replays = []
observed = {}
schema_checks = []
for r in records:
    trace = json.loads((root / f"docs/hydrabench/runs/20261104-mixed-qwen-thinking-agents/{r['case_id']}.json").read_text())
    assert before[r['db_id']] == trace['database_sha256'] == trace['database_sha256_after']
    for kind, queries in [('agent', r['sql']), ('reference', [{'query': r['reference_sql']}])]:
        for q in queries:
            result = run_readonly_sql(dbs[r['db_id']], q['query'])
            entry = {'case_id': r['case_id'], 'kind': kind, 'sql': q['query'], 'result': result}
            assert not result.get('error'), entry
            if kind == 'agent':
                entry['identical_to_saved_result'] = all(result[k] == q['result'][k] for k in ['columns', 'rows', 'truncated'])
                assert entry['identical_to_saved_result'], entry
            replays.append(entry)
    seen = []
    for event in trace['tool_events']:
        if event['event'] != 'on_tool_end' or event['name'] != 'contexthub_search':
            continue
        payload = event['data']['output']
        if isinstance(payload, dict) and 'content' in payload:
            payload = json.loads(payload['content'])
        if isinstance(payload, str):
            payload = json.loads(payload)
        for item in payload.get('results', []):
            uri = item['uri']
            same = item['l1_content'] == index[uri]['content']
            assert same, uri
            schema_checks.append({'case_id': r['case_id'], 'uri': uri, 'type': item['context_type'], 'identical_to_frozen_index': same})
            seen.append(uri)
    observed[r['case_id']] = list(dict.fromkeys(seen))

extra_queries = {
    'dev_46_largest_district_total': ('financial', "SELECT d.district_id,d.A2,d.A3,SUM(l.amount) AS amount,COUNT(*) AS loans FROM loan l JOIN account a ON a.account_id=l.account_id JOIN district d ON d.district_id=a.district_id WHERE l.status IN ('C','D') GROUP BY d.district_id ORDER BY amount DESC LIMIT 5"),
    'dev_46_largest_region_total': ('financial', "SELECT d.A3,SUM(l.amount) AS amount,COUNT(*) AS loans FROM loan l JOIN account a ON a.account_id=l.account_id JOIN district d ON d.district_id=a.district_id WHERE l.status IN ('C','D') GROUP BY d.A3 ORDER BY amount DESC LIMIT 5"),
    'dev_42_district_grain': ('financial', "SELECT district_id,A2,A3,A5,A6,A7,A8 FROM district WHERE A2='Sokolov'"),
    'dev_42_with_without_account': ('financial', "SELECT COUNT(*) AS district_female_pre1950, SUM(EXISTS(SELECT 1 FROM disp p JOIN account a ON a.account_id=p.account_id WHERE p.client_id=c.client_id)) AS with_actual_account FROM client c JOIN district d ON d.district_id=c.district_id WHERE c.gender='F' AND c.birth_date<'1950-01-01' AND d.A2='Sokolov'"),
    'dev_35_ties': ('superhero', "SELECT MAX(attribute_value) AS maximum,COUNT(*) AS maximum_rows,COUNT(DISTINCT hero_id) AS heroes,COUNT(DISTINCT r.race) AS non_null_race_labels FROM hero_attribute ha JOIN superhero s ON s.id=ha.hero_id LEFT JOIN race r ON r.id=s.race_id WHERE ha.attribute_value=(SELECT MAX(attribute_value) FROM hero_attribute)"),
    'dev_55_exact_department': ('student_club', "SELECT m.first_name,m.last_name,mj.major_name,mj.department FROM member m JOIN major mj ON mj.major_id=m.link_to_major WHERE mj.department='Art and Design Department'"),
    'dev_0_11_club_position': ('student_club', "SELECT m.first_name,m.last_name,m.position,mj.major_name FROM member m JOIN major mj ON mj.major_id=m.link_to_major WHERE m.position='Vice President'"),
}
old_audit = json.loads((root / 'docs/hydrabench/artifacts/20261104-sql-semantics/data-audit.json').read_text())
for k, v in old_audit.items():
    extra_queries[k] = (v['db_id'], v['sql'])
additional = {k: {'db_id': db, 'sql': q, 'result': run_readonly_sql(dbs[db], q)} for k, (db, q) in extra_queries.items()}
assert all('error' not in d['result'] for d in additional.values())
hashes = json.loads((root / 'docs/hydrabench/artifacts/20261104-mixed-qwen/trace-sha256.json').read_text())
traces_unchanged = all(sha(root / p) == h for p, h in hashes.items())
assert traces_unchanged
after = {k: sha(p) for k, p in dbs.items()}
assert before == after
summary = {
    'cases': len(records),
    'agent_sql_replayed_identically': sum(x['kind'] == 'agent' for x in replays),
    'reference_sql_replayed': sum(x['kind'] == 'reference' for x in replays),
    'additional_queries': len(additional),
    'observed_search_result_occurrences_verified': len(schema_checks),
    'observed_unique_documents_verified': len({x['uri'] for x in schema_checks}),
    'all_40_original_trace_hashes_unchanged': traces_unchanged,
    'database_sha256_before': before,
    'database_sha256_after': after,
    'source_sha256': {str(p.relative_to(root)): sha(p) for p in [analysis_path, index_path]},
    'half_bred_name_occurrences_in_frozen_passages': [uri for uri, d in index.items() if d['context_type'] == 'resource' and any(s in d['content'].lower().replace('‐','-').replace('‑','-') for s in ['half-bred', 'half bred'])],
}
for name, data in [('checks.json', summary), ('sql-replay.json', replays), ('additional-data.json', additional), ('observed-content-checks.json', schema_checks), ('evidence-by-case.json', {r['case_id']: {'input': r['input'], 'final_answer': r['final_answer'], 'reference_answer': r['reference_answer'], 'reference_sql': r['reference_sql'], 'retrieved_documents': [{'uri': uri, 'context_type': index[uri]['context_type'], 'content': index[uri]['content']} for uri in observed[r['case_id']]]} for r in records})]:
    (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, indent=2))
print(json.dumps({k: v['result'] for k, v in additional.items() if k.startswith('dev_')}, indent=2))
