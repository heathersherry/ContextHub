"""Check live vector results against the original baseline candidate lists."""

import asyncio
import json
from pathlib import Path
from uuid import UUID

from contexthub.config import Settings
from contexthub.db.pool import create_pool
from contexthub.db.repository import PgRepository

def value(v):
    return json.loads(v) if isinstance(v,str) else v

async def main():
    before=json.loads((Path(__file__).parent/'before.json').read_text())
    after=json.loads((Path(__file__).parent/'after.json').read_text())
    assert before==after
    comparison=json.loads((Path(__file__).parent/'comparison.json').read_text())
    jobs=[(r['case_id'],kind,r[kind]) for r in comparison['records'] for kind in ['table_schema','resource']]
    ids=[UUID(job[2][mode]['retrieval_id']) for job in jobs for mode in ['keyword','vector']]
    pool=await create_pool(Settings())
    try:
        async with PgRepository(pool).session('acme') as db:
            rows=await db.fetch('SELECT retrieval_id,candidates,final_versions FROM retrieval_trace WHERE retrieval_id=ANY($1::uuid[])',ids)
            traces={str(r['retrieval_id']):{'candidates':value(r['candidates']),'final_versions':value(r['final_versions'])} for r in rows}
            node_ids={c['node_id'] for t in traces.values() for c in t['candidates']}
            rows=await db.fetch('SELECT id,uri FROM contexts WHERE id=ANY($1::uuid[])',[UUID(i) for i in node_ids])
            uris={str(r['id']):r['uri'] for r in rows}
    finally:
        await pool.close()
    records=[]
    for cid,kind,data in jobs:
        old=traces[data['keyword']['retrieval_id']]
        new=traces[data['vector']['retrieval_id']]
        old_ids=[r['node_id'] for r in old['candidates'] if not r['filtered']]
        new_ids=[r['node_id'] for r in new['candidates'] if not r['filtered']]
        assert len(old_ids)==len(new_ids)==9
        record={'case_id':cid,'context_type':kind,
                'baseline_candidate_uris':[uris[i] for i in old_ids],
                'new_candidate_uris':[uris[i] for i in new_ids],
                'candidate_order_unchanged':old_ids==new_ids,
                'candidate_set_unchanged':set(old_ids)==set(new_ids),
                'vector_result_equals_baseline_first_three':data['vector']['uris']==[uris[i] for i in old_ids[:3]],
                'vector_result_equals_current_first_three':data['vector']['uris']==[uris[i] for i in new_ids[:3]]}
        assert record['vector_result_equals_current_first_three']
        records.append(record)
    summary={'queries':len(records),'index_snapshot_unchanged':before==after,
             'candidate_order_unchanged':sum(r['candidate_order_unchanged'] for r in records),
             'candidate_set_unchanged':sum(r['candidate_set_unchanged'] for r in records),
             'vector_result_equals_baseline_first_three':sum(r['vector_result_equals_baseline_first_three'] for r in records),
             'vector_result_equals_current_first_three':sum(r['vector_result_equals_current_first_three'] for r in records)}
    (Path(__file__).parent/'candidate-audit.json').write_text(json.dumps({'summary':summary,'records':records},indent=2))
    print(json.dumps(summary,indent=2))
    print('Changed candidates:',[(r['case_id'],r['context_type']) for r in records if not r['candidate_order_unchanged']])

asyncio.run(main())
