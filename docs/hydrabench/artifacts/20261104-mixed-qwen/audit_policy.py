"""Verify the actual per-type ranking policy for every successful agent search."""

import asyncio
import json
from pathlib import Path
from uuid import UUID

from contexthub.config import Settings
from contexthub.db.pool import create_pool
from contexthub.db.repository import PgRepository
from contexthub.retrieval.rerank import KeywordRerankStrategy

def decode(value):
    return json.loads(value) if isinstance(value,str) else value

async def main():
    base=Path(__file__).parent
    analysis=json.loads((base/'analysis.json').read_text())
    jobs=[(r['case_id'],kind,s) for r in analysis['records'] for kind,searches in r['searches'].items() for s in searches if s['retrieval_id']]
    pool=await create_pool(Settings())
    try:
        async with PgRepository(pool).session('acme') as db:
            rows=await db.fetch('SELECT retrieval_id,candidates,final_versions FROM retrieval_trace WHERE retrieval_id=ANY($1::uuid[])',[UUID(j[2]['retrieval_id']) for j in jobs])
            traces={str(r['retrieval_id']):{'candidates':decode(r['candidates']),'final_versions':decode(r['final_versions'])} for r in rows}
            ids={c['node_id'] for t in traces.values() for c in t['candidates']}
            rows=await db.fetch('SELECT id,uri,l0_content,l1_content,version,adopted_count,ignored_count FROM contexts WHERE id=ANY($1::uuid[])',[UUID(i) for i in ids])
            nodes={str(r['id']):dict(r) for r in rows}
    finally:
        await pool.close()
    results=[]
    for cid,kind,search in jobs:
        trace=traces[search['retrieval_id']]
        candidates=[nodes[c['node_id']] for c in trace['candidates'] if not c['filtered']]
        assert all(r['version']==1 and r['adopted_count']==0 and r['ignored_count']==0 for r in candidates)
        expected=candidates if kind=='table_schema' else await KeywordRerankStrategy().rerank(search['query'],candidates)
        expected_uris=[r['uri'] for r in expected[:3]]
        actual=[nodes[r['node_id']]['uri'] for r in trace['final_versions']]
        assert actual==search['uris']==expected_uris,(cid,kind,search['query'])
        results.append({'case_id':cid,'context_type':kind,'retrieval_id':search['retrieval_id'],
                        'query':search['query'],'candidate_count':len(candidates),'policy_verified':True,'uris':actual})
    output={'summary':{'searches_verified':len(results),
             'table_vector_searches':sum(r['context_type']=='table_schema' for r in results),
             'text_keyword_searches':sum(r['context_type']=='resource' for r in results)},'records':results}
    (base/'policy-audit.json').write_text(json.dumps(output,indent=2))
    print(json.dumps(output['summary'],indent=2))

asyncio.run(main())
