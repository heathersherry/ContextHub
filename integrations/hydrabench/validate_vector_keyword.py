"""Compare original vector-only and vector+keyword retrieval using saved vectors.

One source, two fixed queries, final top_k=5 and vector candidate budget 15.
No provider client is created, no index is written, and external HTTP is blocked.
Only keyword strategy registration differs between arms. Production defaults
and the existing ranking implementation are not changed.
"""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from fastapi import FastAPI
from validate_chunk_vectors import URI, QUERIES, ROOT, RollbackRepository, ReplayQueries, save, sha
from validate_originals_only import RecordedStrategy
from validate_passage_topk import SAVED, data_state
from contexthub.api.routers import search, documents
from contexthub.retrieval.long_doc import LongDocRetrievalCoordinator, KeywordRetriever
from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService
import contexthub.services.retrieval_service as service_module
from contexthub_sdk import ContextHubClient
from contexthub_deepagents import create_contexthub_tools

OUTPUT=ROOT/'docs/hydrabench/artifacts/20261105-vector-vs-hybrid'


async def protected_state(repo):
    result=await data_state(repo)
    result['unchanged_files']={}
    for relative in ('src','sdk/src','plugins/deepagents/src','tests','plugins/deepagents/tests',
                     'docs/hydrabench/artifacts/20261105-passage-topk'):
        for path in (ROOT/relative).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                result['unchanged_files'][str(path.relative_to(ROOT))]=sha(path.read_bytes())
    return result


async def run():
    OUTPUT.mkdir(exist_ok=False)
    pool=await asyncpg.create_pool(os.environ['HYDRA_SAMPLE_DATABASE_URL'],min_size=1,max_size=2)
    repo=RollbackRepository(pool)
    vectors=json.loads((SAVED/'query-vectors.json').read_text())
    original_send=httpx.AsyncClient.send
    http_requests=[]
    async def local_only(client,request,**kwargs):
        assert request.url.host=='sample.test','External HTTP/model access is forbidden'
        http_requests.append(request.url.path)
        return await original_send(client,request,**kwargs)
    async def no_document_recall(*args,**kwargs): return []
    records=[]
    try:
        before=await protected_state(repo)
        save(OUTPUT,'protected-before.json',before)
        async with repo.session('acme') as db:
            assert await db.fetchval('SELECT current_database()')=='hydra_originals_qwen37_20261105'
            assert await db.fetchval('SELECT count(*) FROM contexts')==1
            assert await db.fetchval('SELECT count(*) FROM document_chunk_embeddings')==16
            doc=dict(await db.fetchrow('SELECT * FROM contexts WHERE uri=$1',URI))
            sections={row['section_id']:dict(row) for row in await db.fetch('SELECT * FROM document_sections')}
        source=(Path(doc['file_path'])/'extracted.txt').read_text()
        save(OUTPUT,'configuration.json',{
            'started_at':datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(),
            'database':'hydra_originals_qwen37_20261105','embedding_model':'qwen3.7-text-embedding',
            'dimensions':1536,'top_k':5,'vector_candidate_budget':15,'rrf_constant':60,
            'queries':QUERIES,'source_sha256':sha(source.encode()),'query_vector_file_sha256':sha((SAVED/'query-vectors.json').read_bytes()),
            'document_l0_recall':False,'l1_bm25':False,'tree_navigation':False,
            'changed_between_arms':'Only original keyword strategy registration',
            'provider_calls':0,'answering_model_calls':0})
        with patch.object(httpx.AsyncClient,'send',local_only), patch.object(service_module,'vector_search',no_document_recall):
            for mode in ('vector_only','hybrid'):
                embedding=ReplayQueries(vectors)
                coordinator=LongDocRetrievalCoordinator()
                vector=RecordedStrategy(ChunkVectorRetriever())
                keyword=RecordedStrategy(KeywordRetriever())
                coordinator.register_strategy('vector',vector)
                if mode=='hybrid': coordinator.register_strategy('keyword',keyword)
                acl=ACLService()
                service=RetrievalService(RetrievalRouter(None),embedding,acl,
                    masking_service=MaskingService(),long_doc_coordinator=coordinator)
                app=FastAPI();app.include_router(search.router);app.include_router(documents.router)
                app.state.repo=repo;app.state.retrieval_service=service
                app.state.acl_service=acl;app.state.audit_service=AuditService()
                app.state.masking_service=MaskingService();app.state.lifecycle_service=None
                captured=[]
                async def capture(response):
                    await response.aread()
                    if response.request.url.path.endswith('/search'):
                        captured.append({'request':json.loads(response.request.content),'response':response.json()})
                client=ContextHubClient('http://sample.test','local','acme','query-agent')
                await client._http.aclose()
                client._http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://sample.test',
                    headers={'X-Account-Id':'acme','X-Agent-Id':'query-agent'},event_hooks={'response':[capture]})
                try:
                    tool=next(t for t in create_contexthub_tools(client) if t.name=='contexthub_search')
                    for label,query in QUERIES.items():
                        name=f'{mode}-{label}'
                        args={'query':query,'context_type':['resource'],'level':'L1','top_k':5}
                        message=await tool.ainvoke({'type':'tool_call','id':name,'name':'contexthub_search','args':args})
                        assert message.status=='success',message.content
                        payload=json.loads(message.content)
                        save(OUTPUT,name+'-agent.json',payload)
                        save(OUTPUT,name+'-api.json',captured[-1])
                        assert len(payload['results'])==5 and captured[-1]['request']['top_k']==5
                        assert len(vector.calls[-1])==15
                        hits=[]
                        for hit in payload['results']:
                            start,end=hit['snippet_offset'];body=hit['content']
                            assert hit['uri']==URI and hit['version']==doc['version']
                            assert body==source[start:end] and hit['content_level']=='snippet'
                            assert not {'l0_content','l1_content','l2_content','snippet'} & hit.keys()
                            read=await client.document.read_section(str(doc['id']),hit['section_id'])
                            assert read.content==source[read.start_offset:read.end_offset]
                            assert read.start_offset<=start<end<=read.end_offset
                            hits.append({'section_id':hit['section_id'],'title':sections[hit['section_id']]['title'],
                                'range':[start,end],'score':hit['score'],'strategy':hit['retrieval_strategy'],
                                'characters':len(body),'body_sha256':sha(body.encode())})
                        assert len({tuple(h['range']) for h in hits})==5
                        ranking={}
                        for route,items in [('keyword',keyword.calls[-1] if keyword.calls else []),('vector',vector.calls[-1])]:
                            for rank,item in enumerate(items,1):
                                key=tuple(item['source_range'])
                                row=ranking.setdefault(key,{'section_id':item['section_id'],'range':key,
                                    'title':sections[item['section_id']]['title'],'rrf_score':0.0})
                                row[route+'_rank']=rank;row[route+'_raw_score']=item['score']
                                row['rrf_score']+=1/(60+rank)
                        ranking=sorted(ranking.values(),key=lambda r:r['rrf_score'],reverse=True)
                        assert [h['range'] for h in hits]==[list(r['range']) for r in ranking[:5]]
                        for hit,row in zip(hits,ranking): assert abs(hit['score']-row['rrf_score'])<1e-12
                        if mode=='hybrid':
                            previous=json.loads((ROOT/f'docs/hydrabench/artifacts/20261105-passage-topk/after-originals_only-default5-{label}-agent.json').read_text())
                            assert payload['results']==previous['results']
                        else:
                            assert [h['range'] for h in hits]==[list(r['source_range']) for r in vector.calls[-1][:5]]
                            assert all(hit['strategy']=='vector' for hit in hits)
                        records.append({'mode':mode,'label':label,'query':query,'hits':hits,'range_ranking':ranking,
                            'keyword_candidates':keyword.calls[-1] if keyword.calls else [],'vector_candidates':vector.calls[-1],
                            'body_characters':sum(h['characters'] for h in hits),'tool_message_characters':len(message.content)})
                    assert embedding.calls==list(QUERIES.values())
                    assert len(keyword.calls)==(2 if mode=='hybrid' else 0)
                finally:
                    await client.aclose()
        for label in QUERIES:
            arms=[r for r in records if r['label']==label]
            assert arms[0]['vector_candidates']==arms[1]['vector_candidates']
        after=await protected_state(repo)
        assert before==after
        save(OUTPUT,'protected-after.json',after)
        save(OUTPUT,'comparison.json',{'completed_at':datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(),
            'records':records,'checks':{'identical_vector_candidates':True,'hybrid_matches_previous_trial':True,
            'all_returned_bodies_are_exact_unique_source_ranges':True,'section_reads_verified':True,
            'production_sources_old_indexes_and_artifacts_unchanged':True,'provider_calls':0,'answering_model_calls':0,
            'local_http_search_calls':http_requests.count('/api/v1/search'),'local_http_section_reads':20}})
        print(json.dumps([{'mode':r['mode'],'query':r['label'],'sections':[h['section_id'] for h in r['hits']],
                          'body_characters':r['body_characters']} for r in records]))
    finally:
        await pool.close()


if __name__=='__main__': asyncio.run(run())
