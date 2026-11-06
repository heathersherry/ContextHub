"""Compare the frozen keyword-reranked and vector-only HyDRA retrieval probes."""

import argparse
import json
from pathlib import Path

def compare(args):
    baseline = {p.stem:json.loads(p.read_text()) for p in args.baseline.glob('*.json')}
    vector = {p.stem:json.loads(p.read_text()) for p in args.vector.glob('*.json')}
    labels = json.loads(args.labels.read_text())['labels']
    assert len(baseline)==20 and baseline.keys()==vector.keys()==labels.keys()
    records = []
    for cid, old in baseline.items():
        new = vector[cid]
        assert old['db_id']==new['db_id'] and old['type']==new['type']
        assert old['reference_tables']==new['reference_tables']
        assert old['top_k']==new['top_k']==3
        result = {'case_id':cid, 'type':old['type'], 'db_id':old['db_id'],
                  'reference_tables':old['reference_tables'], 'text_coverage':labels[cid]['coverage'],
                  'known_text_anchors':labels[cid]['known_support_uris']}
        for kind in ['table_schema','resource']:
            assert old[kind]['query']==new[kind]['query']
            result[kind] = {'query':old[kind]['query']}
            for mode,data in [('keyword',old),('vector',new)]:
                hits=data[kind]['response']['results']
                assert len(hits)<=3
                wanted=({f"ctx://datalake/hydra/{old['db_id']}/{t}".casefold() for t in old['reference_tables']}
                        if kind=='table_schema' else set(labels[cid]['known_support_uris']))
                uris=[h['uri'] for h in hits]
                found={u.casefold() if kind=='table_schema' else u for u in uris}
                intersection=wanted & found
                result[kind][mode]={'uris':uris, 'scores':[h['score'] for h in hits],
                    'retrieval_id':data[kind]['response']['retrieval_id'],
                    'required_count':len(wanted), 'hits':len(intersection),
                    'any_hit':bool(intersection), 'all_required':bool(wanted) and intersection==wanted,
                    'first_hit_rank':next((i for i,u in enumerate(uris,1) if (u.casefold() if kind=='table_schema' else u) in wanted),None)}
                if kind=='table_schema':
                    result[kind][mode]['other_database_hits']=sum(not u.startswith(f"ctx://datalake/hydra/{old['db_id']}/") for u in uris)
        result['table_hit_delta']=result['table_schema']['vector']['hits']-result['table_schema']['keyword']['hits']
        result['text_anchor_hit_delta']=int(result['resource']['vector']['any_hit'])-int(result['resource']['keyword']['any_hit'])
        records.append(result)
    records.sort(key=lambda r:int(r['case_id'].split('_')[1]))
    summary={}
    for mode in ['keyword','vector']:
        summary[mode]={
            'cases':len(records),
            'required_table_occurrences':sum(r['table_schema'][mode]['required_count'] for r in records),
            'table_hits_at_3':sum(r['table_schema'][mode]['hits'] for r in records),
            'all_tables_at_3_cases':sum(r['table_schema'][mode]['all_required'] for r in records),
            'any_table_at_3_cases':sum(r['table_schema'][mode]['any_hit'] for r in records),
            'other_database_table_hits':sum(r['table_schema'][mode]['other_database_hits'] for r in records),
            'text_covered_cases':sum(bool(r['known_text_anchors']) for r in records),
            'text_anchor_hit_at_3_cases':sum(r['resource'][mode]['any_hit'] for r in records),
            'text_anchor_hit_at_1_cases':sum(r['resource'][mode]['first_hit_rank']==1 for r in records),
        }
    summary['paired_changes']={
        'table_more_hits':[r['case_id'] for r in records if r['table_hit_delta']>0],
        'table_fewer_hits':[r['case_id'] for r in records if r['table_hit_delta']<0],
        'table_same_hit_count':[r['case_id'] for r in records if r['table_hit_delta']==0],
        'text_anchor_gained':[r['case_id'] for r in records if r['text_anchor_hit_delta']>0],
        'text_anchor_lost':[r['case_id'] for r in records if r['text_anchor_hit_delta']<0],
    }
    args.output.write_text(json.dumps({'summary':summary,'records':records},ensure_ascii=False,indent=2))
    print(json.dumps(summary,indent=2))
    for r in records:
        print(r['case_id'],r['type'],r['db_id'],
              'tables',r['table_schema']['keyword']['hits'],'->',r['table_schema']['vector']['hits'],'/',len(r['reference_tables']),
              'text',r['resource']['keyword']['first_hit_rank'],'->',r['resource']['vector']['first_hit_rank'])

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--vector',type=Path,required=True)
    parser.add_argument('--labels',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    compare(parser.parse_args())
