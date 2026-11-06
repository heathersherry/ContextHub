"""Summarize original tool traces without calling a model or retrieval service."""

import argparse
import json
from pathlib import Path

def main(args):
    cases={r['id']:r for r in json.loads(args.cases.read_text())}
    labels=json.loads(args.labels.read_text())['labels']
    records=[]
    for cid,case in cases.items():
        path=args.runs/f'{cid}.json'
        if not path.exists():
            continue
        trace=json.loads(path.read_text())
        if trace['status']=='running':
            continue
        starts={e['run_id']:e for e in trace['tool_events'] if e['event']=='on_tool_start'}
        searches={'table_schema':[],'resource':[]}
        sql=[]
        reads=[]
        for event in trace['tool_events']:
            if event['event']!='on_tool_end' or event['name'] not in ['contexthub_search','contexthub_read','run_sql']:
                continue
            start=starts[event['run_id']]
            arguments=start['data']['input']
            result=event['data']['output']
            if 'content' in result:
                result=json.loads(result['content'])
            if event['name']=='contexthub_search':
                searches[arguments['context_type']].append({'query':arguments['query'],
                    'started_seconds':start['seconds'],'finished_seconds':event['seconds'],
                    'uris':[h['uri'] for h in result.get('results',[])],
                    'retrieval_id':result.get('retrieval_id'),'error':result.get('error')})
            elif event['name']=='run_sql':
                sql.append({'query':arguments['code_query'],'started_seconds':start['seconds'],
                            'finished_seconds':event['seconds'],'result':result})
            else:
                reads.append({'uri':arguments['uri'],'started_seconds':start['seconds'],'error':result.get('error')})
        for values in searches.values():values.sort(key=lambda r:r['started_seconds'])
        sql.sort(key=lambda r:r['started_seconds'])
        wanted={f"ctx://datalake/hydra/{case['db_id']}/{t}".casefold() for t in case['reference_tables']}
        found={u.casefold() for s in searches['table_schema'] for u in s['uris']}
        first={u.casefold() for u in searches['table_schema'][0]['uris']} if searches['table_schema'] else set()
        text_found={u for s in searches['resource'] for u in s['uris']}
        last_ai=next((m['data'] for m in reversed(trace['messages']) if m['type']=='ai'),{})
        records.append({'case_id':cid,'type':case['type'],'db_id':case['db_id'],
            'status':trace['status'],'error':trace.get('error'),'elapsed_seconds':trace['elapsed_seconds'],
            'last_finish_reason':last_ai.get('response_metadata',{}).get('finish_reason'),
            'last_usage_metadata':last_ai.get('usage_metadata'),
            'input':trace['input'],'final_answer':trace.get('final_answer'),
            'reference_answer':case['final_answer'],'reference_sql':case['sql_query'],
            'reference_sql_output':case['sql_output'],'reference_search_question':case['search_question'],
            'reference_search_answer':case['search_answer'],'reference_tables':case['reference_tables'],
            'first_table_hits':len(wanted&first),'cumulative_table_hits':len(wanted&found),
            'all_reference_tables_retrieved':wanted<=found,'missing_reference_tables':sorted(wanted-found),
            'known_text_anchor_hit':bool(set(labels[cid]['known_support_uris'])&text_found),
            'text_coverage':labels[cid]['coverage'],'searches':searches,'reads':reads,'sql':sql,
            'sql_errors':[r for r in sql if r['result'].get('error')],
            'other_tools':[e['name'] for e in starts.values() if e['name'] not in ['contexthub_search','contexthub_read','run_sql']],
            'database_unchanged':trace['database_sha256']==trace['database_sha256_after']})
    complete=[r for r in records if r['status']=='completed']
    summary={'selected':len(cases),'finished_attempts':len(records),'completed':len(complete),
             'statuses':{s:sum(r['status']==s for r in records) for s in sorted({r['status'] for r in records})},
             'reference_table_occurrences':sum(len(r['reference_tables']) for r in complete),
             'first_table_hits':sum(r['first_table_hits'] for r in complete),
             'cumulative_table_hits':sum(r['cumulative_table_hits'] for r in complete),
             'all_reference_tables_retrieved_cases':sum(r['all_reference_tables_retrieved'] for r in complete),
             'known_text_anchor_hit_cases':sum(r['known_text_anchor_hit'] for r in complete),
             'all_finished_reference_table_occurrences':sum(len(r['reference_tables']) for r in records),
             'all_finished_cumulative_table_hits':sum(r['cumulative_table_hits'] for r in records),
             'all_finished_all_reference_tables_retrieved':sum(r['all_reference_tables_retrieved'] for r in records),
             'all_finished_known_text_anchor_hits':sum(r['known_text_anchor_hit'] for r in records),
             'database_unchanged_all_finished_attempts':all(r['database_unchanged'] for r in records)}
    args.output.write_text(json.dumps({'summary':summary,'records':records},ensure_ascii=False,indent=2))
    print(json.dumps(summary,indent=2))
    if args.answers:
        for r in records:
            print('\nCASE',r['case_id'],r['type'],r['status'],r['elapsed_seconds'])
            print('ANSWER',r['final_answer']);print('REFERENCE',r['reference_answer'])

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,required=True)
    p.add_argument('--labels',type=Path,required=True)
    p.add_argument('--runs',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--answers',action='store_true')
    main(p.parse_args())
