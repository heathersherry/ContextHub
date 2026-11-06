"""Replay two saved queries before/after passage top-k, without provider access.

Use the prior source snapshot for before, current sources for after. Both arms
use the same database and stored vectors. Explicit top_k=1 is retained; omitting
top_k exercises the unchanged DeepAgents default of 5. All writes are rollback.
"""
import argparse
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
from validate_originals_only import protected, snapshot, RecordedStrategy
from contexthub.api.routers import search, documents
from contexthub.llm.chat_client import NoOpChatClient
from contexthub.retrieval.long_doc import LongDocRetrievalCoordinator, KeywordRetriever, TreeRetriever
from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService
import contexthub.services.retrieval_service as service_module
from contexthub_sdk import ContextHubClient
from contexthub_deepagents import create_contexthub_tools

SAVED = ROOT/'docs/hydrabench/artifacts/20261105-originals-only-qwen37'
OUTPUT = ROOT/'docs/hydrabench/artifacts/20261105-passage-topk'


async def data_state(repo):
    value = await protected()
    value['existing_source_and_artifacts'] = {
        key: digest for key,digest in value['existing_source_and_artifacts'].items()
        if key.startswith('docs/hydrabench/artifacts/')}
    value['qwen37_artifacts'] = {str(path.relative_to(SAVED)):sha(path.read_bytes())
                               for path in SAVED.rglob('*') if path.is_file()}
    async with repo.session('acme') as db:
        value['qwen37_database'] = await snapshot(db)
    return value


async def run(phase):
    if phase == 'before':
        OUTPUT.mkdir(exist_ok=False)
    pool = await asyncpg.create_pool(os.environ['HYDRA_SAMPLE_DATABASE_URL'],min_size=1,max_size=2)
    repo = RollbackRepository(pool)
    vectors = json.loads((SAVED/'query-vectors.json').read_text())
    async with repo.session('acme') as db:
        assert await db.fetchval('SELECT current_database()') == 'hydra_originals_qwen37_20261105'
        assert await db.fetchval('SELECT count(*) FROM contexts') == 1
        doc = dict(await db.fetchrow('SELECT * FROM contexts WHERE uri=$1',URI))
        sections = {row['section_id']:dict(row) for row in await db.fetch('SELECT * FROM document_sections')}
    source = (Path(doc['file_path'])/'extracted.txt').read_text()
    before = await data_state(repo)
    if phase == 'before':
        save(OUTPUT,'protected-before.json',before)
    else:
        assert before == json.loads((OUTPUT/'protected-before.json').read_text())
    original_send = httpx.AsyncClient.send
    async def local_only(client, request, **kwargs):
        assert request.url.host == 'sample.test', 'External HTTP/model calls are forbidden in this replay'
        return await original_send(client,request,**kwargs)
    records=[]
    original_search=service_module.vector_search
    try:
        with patch.object(httpx.AsyncClient,'send',local_only):
            for mode in ('originals_only','mixed'):
                embedding=ReplayQueries(vectors)
                coordinator=LongDocRetrievalCoordinator()
                tree=RecordedStrategy(TreeRetriever(NoOpChatClient()))
                keyword=RecordedStrategy(KeywordRetriever())
                vector=RecordedStrategy(ChunkVectorRetriever())
                for name,strategy in [('tree',tree),('keyword',keyword),('vector',vector)]:
                    coordinator.register_strategy(name,strategy)
                acl=ACLService()
                service=RetrievalService(RetrievalRouter(None) if mode=='originals_only' else RetrievalRouter.default(),
                    embedding,acl,masking_service=MaskingService(),long_doc_coordinator=coordinator)
                app=FastAPI(); app.include_router(search.router); app.include_router(documents.router)
                app.state.repo=repo; app.state.retrieval_service=service
                app.state.acl_service=acl; app.state.audit_service=AuditService()
                app.state.masking_service=MaskingService(); app.state.lifecycle_service=None
                captured=[]
                async def capture(response):
                    await response.aread()
                    if response.request.url.path.endswith('/search'):
                        captured.append({'request':json.loads(response.request.content),'response':response.json()})
                client=ContextHubClient('http://sample.test','local','acme','query-agent')
                await client._http.aclose()
                client._http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://sample.test',
                    headers={'X-Account-Id':'acme','X-Agent-Id':'query-agent'},event_hooks={'response':[capture]})
                async def route(*args,**kwargs):
                    return [] if mode=='originals_only' else await original_search(*args,**kwargs)
                try:
                    tool=next(t for t in create_contexthub_tools(client) if t.name=='contexthub_search')
                    with patch.object(service_module,'vector_search',route):
                        for budget,k in [('top1',1),('default5',5)]:
                            for label,query in QUERIES.items():
                                args={'query':query,'context_type':['resource'],'level':'L1'}
                                if budget=='top1': args['top_k']=1
                                name=f'{phase}-{mode}-{budget}-{label}'
                                message=await tool.ainvoke({'type':'tool_call','id':name,'name':'contexthub_search','args':args})
                                assert message.status=='success',message.content
                                payload=json.loads(message.content)
                                save(OUTPUT,name+'-agent.json',payload)
                                save(OUTPUT,name+'-api.json',captured[-1])
                                assert captured[-1]['request']['top_k']==k
                                assert 1 <= len(payload['results']) <= k
                                hits=[]
                                for hit in payload['results']:
                                    body=hit['content']; start=source.index(body); end=start+len(body)
                                    assert hit['uri']==URI and hit['version']==doc['version']
                                    assert body==source[start:end] and hit['content_level']=='snippet'
                                    assert not {'l0_content','l1_content','l2_content','snippet'} & hit.keys()
                                    if phase=='after': assert hit['snippet_offset']==[start,end]
                                    read=await client.document.read_section(str(doc['id']),hit['section_id'])
                                    assert read.content==source[read.start_offset:read.end_offset]
                                    assert read.start_offset <= start < end <= read.end_offset
                                    hits.append({'section_id':hit['section_id'],'section_title':sections[hit['section_id']]['title'],
                                        'range':[start,end],'characters':len(body),'body_sha256':sha(body.encode()),
                                        'score':hit['score'],'strategy':hit['retrieval_strategy']})
                                assert len({tuple(hit['range']) for hit in hits})==len(hits)
                                if phase=='before': assert len(hits)==1
                                if phase=='after' and k==5: assert len(hits)==5
                                record={'mode':mode,'budget':budget,'query_label':label,'query':query,
                                    'tool_arguments':args,'actual_top_k':k,'vector_candidate_budget':k*3,
                                    'hits':hits,'keyword_candidates':keyword.calls[-1],'vector_candidates':vector.calls[-1],
                                    'body_characters':sum(h['characters'] for h in hits),'tool_message_characters':len(message.content)}
                                records.append(record)
                    assert len(embedding.calls)==4 and len(tree.calls)==(0 if mode=='originals_only' else 4)
                finally:
                    await client.aclose()
        assert await data_state(repo)==before
        save(OUTPUT,phase+'-report.json',{'completed_at':datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(),
            'records':records,'provider_calls':0,'chat_calls':0,'answering_model_calls':0,
            'all_original_reads_match':True,'no_duplicate_ranges':True,'data_unchanged':True})
        if phase=='after': save(OUTPUT,'protected-after.json',await data_state(repo))
        print(json.dumps([{'mode':r['mode'],'budget':r['budget'],'query':r['query_label'],
                          'sections':[h['section_id'] for h in r['hits']], 'characters':r['body_characters']} for r in records]))
    finally:
        await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('before','after'))
    asyncio.run(run(parser.parse_args().phase))
