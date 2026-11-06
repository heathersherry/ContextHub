import ast,asyncio,json,sys
from pathlib import Path
import asyncpg
ROOT=Path('/Users/sherrylin/.codex/worktrees/hydra-smoke/ContextHub');sys.path.insert(0,str(ROOT/'src'))
from contexthub.db.repository import ScopedRepo
from contexthub.llm.chat_client import NoOpChatClient
from contexthub.retrieval.vector_strategy import vector_search
from contexthub.retrieval.router import RetrievalRouter
from contexthub.retrieval.long_doc import TreeRetriever,KeywordRetriever,LongDocRetrievalCoordinator
from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
from contexthub.services.retrieval_service import _fuse_original_hits
ART=ROOT/'docs/hydrabench/artifacts/20261105-chunk-vectors';OUT=Path('/private/tmp/contexthub-chunk-ranking-audit-20261105')
m=ast.parse(Path('/private/tmp/hydra-expanded/with_env.py').read_text());dsn=next(n.value.value for n in ast.walk(m) if isinstance(n,ast.keyword) and n.arg=='DATABASE_URL')
async def main():
 c=await asyncpg.connect(dsn.rsplit('/',1)[0]+'/hydra_chunks_sample_20261105',server_settings={'default_transaction_read_only':'on'})
 result=[]
 async with c.transaction():
  await c.execute("SELECT set_config('app.account_id','acme',true)");db=ScopedRepo(c)
  doc=dict(await db.fetchrow('SELECT * FROM contexts'));sections={r['section_id']:r['title'] for r in await db.fetch('SELECT section_id,title FROM document_sections')}
  coordinator=LongDocRetrievalCoordinator();coordinator.register_strategy('tree',TreeRetriever(NoOpChatClient()));coordinator.register_strategy('keyword',KeywordRetriever());coordinator.register_strategy('vector',ChunkVectorRetriever())
  for query,vector in json.loads((ART/'query-vectors.json').read_text()).items():
   recalled=await vector_search(db,vector,3,context_types=['resource'],include_stale=True)
   ranked=await RetrievalRouter.default().for_context_types(['resource']).rerank(query,recalled)
   tree=await coordinator.retrieve(db,query,ranked)
   originals=await coordinator.search_originals(db,query,[doc],query_embedding=vector,top_k=3)
   fused=_fuse_original_hits(tree,originals)
   def projection(hit):
    return {'section_id':hit.get('section_id'),'title':sections.get(hit.get('section_id')),
     'snippet_offset':hit.get('snippet_offset'),'cosine':hit.get('cosine_similarity'),
     'rerank_score':hit.get('_rerank_score'),'strategy':hit.get('retrieval_strategy')}
   row={'query':query,'L0_vector':[projection(x) for x in recalled],'L1_BM25':[projection(x) for x in ranked],
    'tree_NoOp':[projection(x) for x in tree],'original_range_fusion':[projection(x) for x in originals],
    'final_document_fusion':[projection(x) for x in fused]}
   result.append(row)
 await c.close()
 with (OUT/'stages.json').open('x') as f:json.dump(result,f,indent=2)
 print(json.dumps(result,indent=2))
asyncio.run(main())
