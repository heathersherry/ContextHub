"""Two fixed queries, one existing source, isolated chunk vectors, no chat calls.

Run `queries`, then `before` using the pre-change source snapshot, then `index`
and `after` using the current source. Both searches replay identical query
vectors; tool bookkeeping is rolled back. The fixture must already exist in the
single-document isolated database. No benchmark answers or reference reasoning
are loaded by this script.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path

import asyncpg
import httpx
from fastapi import FastAPI
from contexthub_sdk import ContextHubClient
from contexthub_deepagents import create_contexthub_tools
from contexthub.api.routers import search, documents
from contexthub.config import Settings
from contexthub.db.repository import ScopedRepo, PgRepository
from contexthub.llm.chat_client import NoOpChatClient
from contexthub.llm.factory import create_embedding_client
from contexthub.retrieval.long_doc import KeywordRetriever, LongDocRetrievalCoordinator, TreeRetriever
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService

DATABASE = 'hydra_chunks_sample_20261105'
URI = 'ctx://resources/hydra-layer-validation/microarchitecture'
QUERIES = {
    'literal': '1986 VAX 8800',
    'semantic': 'How can a CPU make progress on several instructions at once by handling different stages simultaneously?',
}
ROOT = Path(__file__).resolve().parents[2]
PROTECTED = [
    'docs/hydrabench/artifacts/20261104-paper-metrics/token-usage.json',
    'docs/hydrabench/artifacts/20261105-document-layers-qwen38-27b/validation.json',
    'docs/hydrabench/artifacts/20261105-document-layers-qwen38-27b/provider-usage.json',
    'docs/hydrabench/artifacts/20261105-section-snippets/comparison.json',
]


def sha(value): return hashlib.sha256(value).hexdigest()


def save(output, name, value):
    path = output / name
    with path.open('x') as file:
        json.dump(value, file, ensure_ascii=False, indent=2, default=str)


async def protected_state():
    base = os.environ['HYDRA_SAMPLE_DATABASE_URL'].rsplit('/',1)[0]
    result = {'files': {p:sha((ROOT/p).read_bytes()) for p in PROTECTED}}
    for name in ('hydra_retrieval_20261104', 'hydra_layers_sample_20261105_qwen38_27b'):
        conn = await asyncpg.connect(base+'/'+name, server_settings={'default_transaction_read_only':'on'})
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.account_id','acme',true)")
                row = await conn.fetchrow("""SELECT count(*) contexts, count(l0_embedding) embedded,
                    md5(string_agg(md5(concat_ws('|',uri,version::text,l0_content,l1_content,l2_content)),'' ORDER BY uri)) content_fingerprint
                    FROM contexts""")
                result[name] = dict(row)
                if name.endswith('qwen38_27b'):
                    for table in ('contexts','document_sections','retrieval_trace'):
                        rows=await conn.fetch(f'SELECT to_jsonb(t)::text AS data FROM {table} t')
                        result[name][table+'_sha256']=sha('\n'.join(sorted(r['data'] for r in rows)).encode())
        finally:
            await conn.close()
    return result


async def snapshot(db):
    result = {}
    for table in ('contexts','document_sections','retrieval_trace'):
        rows=await db.fetch(f'SELECT to_jsonb(t)::text AS data FROM {table} t')
        result[table] = sha('\n'.join(sorted(r['data'] for r in rows)).encode())
    return result


class RollbackRepository:
    def __init__(self, pool): self.pool=pool
    @asynccontextmanager
    async def session(self, account_id):
        async with self.pool.acquire() as conn:
            tx=conn.transaction(); await tx.start()
            try:
                await conn.execute("SELECT set_config('app.account_id',$1,true)",account_id)
                yield ScopedRepo(conn)
            finally:
                await tx.rollback()


class ReplayQueries:
    def __init__(self, vectors): self.vectors=vectors; self.calls=[]
    async def embed(self, text):
        self.calls.append(text)
        return self.vectors[text]


async def provider(output, phase, operation):
    client=create_embedding_client(Settings())
    assert hasattr(client,'embed_batch'), 'Configured embedding model is required'
    usage=[]
    async def capture(response):
        await response.aread()
        payload=response.json()
        request=json.loads(response.request.content)
        inputs=request['input']
        if isinstance(inputs,str): inputs=[inputs]
        usage.append({'route':response.request.url.path,'status_code':response.status_code,
                      'model':request['model'],'input_count':len(inputs),
                      'input_characters':[len(x) for x in inputs],
                      'input_sha256':[sha(x.encode()) for x in inputs],
                      'usage':payload.get('usage')})
    client._client.event_hooks['response']=[capture]
    try:
        return await operation(client)
    finally:
        save(output,phase+'-provider-usage.json',usage)
        await client.close()


async def run(phase, output):
    fixture=json.loads((output/'fixture.json').read_text())
    assert fixture['database']==DATABASE
    source_file=Path(fixture['document']['file_path'])/'extracted.txt'
    source=source_file.read_text()
    assert sha(source.encode())==fixture['source_sha256']
    if phase=='queries':
        save(output,'protected-before.json',await protected_state())
        async def embed(client):
            values=await client.embed_batch(list(QUERIES.values()))
            assert len(values)==2 and all(v is not None for v in values)
            save(output,'query-vectors.json',dict(zip(QUERIES.values(),values)))
        await provider(output,phase,embed)
        print('Saved two fixed query embeddings; no chat model invoked.')
        return
    pool=await asyncpg.create_pool(os.environ['HYDRA_SAMPLE_DATABASE_URL'],min_size=1,max_size=2)
    repo=RollbackRepository(pool)
    try:
        async with repo.session('acme') as db:
            assert await db.fetchval('SELECT current_database()')==DATABASE
            assert await db.fetchval('SELECT count(*) FROM contexts')==1
            state=await snapshot(db)
            doc=dict(await db.fetchrow('SELECT * FROM contexts WHERE uri=$1',URI))
            sections=[dict(r) for r in await db.fetch('SELECT * FROM document_sections WHERE context_id=$1 ORDER BY depth,section_id',doc['id'])]
        if phase=='index':
            from contexthub.services.document_chunk_indexer import DocumentChunkIndexer
            from contexthub.retrieval.long_doc.ranges import document_ranges,embedding_input,enclosing_section
            ranges=document_ranges(source,sections)
            assert ''.join(source[a:b] for a,b in ranges)==source
            assert all(a[1]==b[0] for a,b in zip(ranges,ranges[1:]))
            inputs=[{'start':a,'end':b,'section_id':enclosing_section(sections,a,b)['section_id'],
                     'embedding_input':embedding_input(source,sections,a,b)} for a,b in ranges]
            save(output,'chunk-inputs.json',inputs)
            async def index(client):
                async with PgRepository(pool).session('acme') as db:
                    assert await db.fetchval('SELECT count(*) FROM document_chunk_embeddings')==0
                    count=await DocumentChunkIndexer(client).index(db,doc,source,sections)
                    assert count==len(ranges)
                return count
            count=await provider(output,phase,index)
            async with repo.session('acme') as db:
                assert await snapshot(db)==state
                rows=await db.fetch('SELECT context_id,context_version,section_id,start_offset,end_offset,source_sha256,vector_dims(embedding) dimensions FROM document_chunk_embeddings ORDER BY start_offset')
                save(output,'chunk-index.json',[dict(r) for r in rows])
            print(json.dumps({'indexed_original_ranges':count,'unchanged_document_layers_and_sections':True}))
            return
        vectors=json.loads((output/'query-vectors.json').read_text())
        embedding=ReplayQueries(vectors)
        coordinator=LongDocRetrievalCoordinator()
        coordinator.register_strategy('tree',TreeRetriever(NoOpChatClient()))
        coordinator.register_strategy('keyword',KeywordRetriever())
        if phase=='after':
            from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
            coordinator.register_strategy('vector',ChunkVectorRetriever())
        acl=ACLService()
        service=RetrievalService(RetrievalRouter.default(),embedding,acl,
                                masking_service=MaskingService(),long_doc_coordinator=coordinator)
        app=FastAPI();app.include_router(search.router);app.include_router(documents.router)
        app.state.repo=repo;app.state.retrieval_service=service
        app.state.acl_service=acl;app.state.audit_service=AuditService()
        app.state.masking_service=MaskingService();app.state.lifecycle_service=None
        client=ContextHubClient('http://sample.test','local','acme','query-agent')
        await client._http.aclose()
        client._http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://sample.test',headers={'X-Account-Id':'acme','X-Agent-Id':'query-agent'})
        report=[]
        try:
            tool=next(t for t in create_contexthub_tools(client) if t.name=='contexthub_search')
            for label,query in QUERIES.items():
                msg=await tool.ainvoke({'type':'tool_call','id':phase+'-'+label,'name':'contexthub_search',
                    'args':{'query':query,'context_type':['resource'],'top_k':1,'level':'L1'}})
                assert msg.status=='success',msg.content
                payload=json.loads(msg.content)
                save(output,label+'-agent-'+phase+'.json',payload)
                assert len(payload['results'])==1
                hit=payload['results'][0];body=hit['content']
                assert hit['uri']==URI and hit['version']==doc['version']
                assert body in source and hit['content_level']=='snippet'
                assert not {'l0_content','l1_content','l2_content','snippet'} & hit.keys()
                (output/(label+'-content-'+phase+'.txt')).write_text(body)
                section=next(s for s in sections if s['section_id']==hit['section_id'])
                entry={'label':label,'query':query,'section_id':hit['section_id'],'section_title':section['title'],
                       'strategy':hit['retrieval_strategy'],'source_range':[source.index(body),source.index(body)+len(body)],
                       'content_characters':len(body),'payload_characters':len(json.dumps(payload,ensure_ascii=False)),
                       'estimated_content_tokens_chars_div_4':len(body)/4}
                if phase=='after':
                    async with repo.session('acme') as db:
                        direct=await ChunkVectorRetriever().retrieve(db,vectors[query],[doc],3)
                    entry['vector_candidates']=[{'section_id':r.section_id,'range':r.snippet_offset,'cosine':r.relevance_score} for r in direct]
                    read=await client.document.read_section(str(doc['id']),hit['section_id'])
                    assert read.content==source[read.start_offset:read.end_offset]
                report.append(entry)
            assert embedding.calls==list(QUERIES.values())
            async with repo.session('acme') as db: assert await snapshot(db)==state
            save(output,phase+'-report.json',{'queries':report,'database_snapshot':state,
                 'chat_calls':0,'search_provider_calls':0,
                 'note':'Query vectors replayed identically. Tree uses NoOp chat in both arms; this is not a live LLM tree comparison. SDK/DeepAgents tool messages are actual; no answering model was invoked.'})
            if phase=='after':
                protected=await protected_state()
                assert protected==json.loads((output/'protected-before.json').read_text())
                save(output,'protected-after.json',protected)
            print(json.dumps(report,ensure_ascii=False))
        finally:
            await client.aclose()
    finally:
        await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['queries','before','index','after'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(run(args.phase,args.output))
