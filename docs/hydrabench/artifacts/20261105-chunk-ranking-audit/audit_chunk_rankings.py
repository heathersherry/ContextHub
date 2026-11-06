"""Read-only diagnosis of saved vectors and fixed queries; no model clients called."""
import ast,asyncio,hashlib,json,math,re,sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import asyncpg
import httpx
ROOT=Path('/Users/sherrylin/.codex/worktrees/hydra-smoke/ContextHub')
sys.path.insert(0,str(ROOT/'src'))
from contexthub.db.repository import ScopedRepo
from contexthub.retrieval.long_doc.keyword_retriever import KeywordRetriever,_tokenize
from contexthub.retrieval.long_doc.ranges import document_ranges,embedding_input
from contexthub.retrieval.long_doc.coordinator import LongDocRetrievalCoordinator
from contexthub.retrieval.long_doc.result import LongDocRetrievalResult
from contexthub.llm.openai_client import OpenAIEmbeddingClient
ART=ROOT/'docs/hydrabench/artifacts/20261105-chunk-vectors'
OUT=Path('/private/tmp/contexthub-chunk-ranking-audit-20261105');OUT.mkdir(exist_ok=True);assert not (OUT/"audit.json").exists()
def sha(x):return hashlib.sha256(x).hexdigest()
def cos(x,y):return math.fsum(a*b for a,b in zip(x,y))/math.sqrt(math.fsum(a*a for a in x)*math.fsum(b*b for b in y))
def stats(v):
 return {'dimensions':len(v),'nonzero_prefix_length':max(i+1 for i,x in enumerate(v) if x!=0),
         'norm':math.sqrt(math.fsum(x*x for x in v)), 'finite':all(math.isfinite(x) for x in v)}
def config_hashes():
 return {str(p.relative_to(ROOT)):sha(p.read_bytes()) for d in ['src','sdk/src','plugins/deepagents/src'] for p in (ROOT/d).rglob('*.py')}
def artifact_hashes():return {str(p.relative_to(ART)):sha(p.read_bytes()) for p in ART.rglob('*') if p.is_file()}
async def db_snapshot(c):
 out={}
 for table in ['contexts','document_sections','document_chunk_embeddings','retrieval_trace']:
  rows=await c.fetch(f'SELECT to_jsonb(t)::text data FROM {table} t')
  out[table]=sha('\n'.join(sorted(r['data'] for r in rows)).encode())
 return out
async def main():
 protected_code=config_hashes();protected_artifacts=artifact_hashes()
 inputs=json.loads((ART/'chunk-inputs.json').read_text());queries=json.loads((ART/'query-vectors.json').read_text())
 records=json.loads((ART/'index-provider-usage.json').read_text());qrecords=json.loads((ART/'queries-provider-usage.json').read_text())
 m=ast.parse(Path('/private/tmp/hydra-expanded/with_env.py').read_text());dsn=next(n.value.value for n in ast.walk(m) if isinstance(n,ast.keyword) and n.arg=='DATABASE_URL')
 conn=await asyncpg.connect(dsn.rsplit('/',1)[0]+'/hydra_chunks_sample_20261105',server_settings={'default_transaction_read_only':'on'})
 async with conn.transaction():
  await conn.execute("SELECT set_config('app.account_id','acme',true)")
  before=await db_snapshot(conn)
  doc=dict(await conn.fetchrow('SELECT * FROM contexts'));source=(Path(doc['file_path'])/'extracted.txt').read_text()
  sections=[dict(r) for r in await conn.fetch('SELECT * FROM document_sections ORDER BY depth,section_id')]
  chunks=[dict(r) for r in await conn.fetch('SELECT *,embedding::text AS vector FROM document_chunk_embeddings ORDER BY start_offset')]
  for c in chunks:c['vector']=json.loads(c['vector']);c.pop('embedding')
  requested_hashes=[h for request in records for h in request['input_sha256']]
  requested_lengths=[n for request in records for n in request['input_characters']]
  assert len(chunks)==len(inputs)==16
  assert [(c['start_offset'],c['end_offset']) for c in chunks]==document_ranges(source,sections)
  assert ''.join(source[c['start_offset']:c['end_offset']] for c in chunks)==source
  assert len(set(json.dumps(c['vector']) for c in chunks))==16
  title={s['section_id']:s['title'] for s in sections}
  input_audit=[]
  for i,(saved,c) in enumerate(zip(inputs,chunks)):
   a,b=c['start_offset'],c['end_offset'];recomputed=embedding_input(source,sections,a,b)
   assert saved['embedding_input']==recomputed
   assert sha(recomputed.encode())==requested_hashes[i] and len(recomputed)==requested_lengths[i]
   assert (saved['start'],saved['end'],saved['section_id'])==(a,b,c['section_id'])
   assert c['source_sha256']==sha(source.encode()) and c['context_version']==doc['version']
   input_audit.append({'section_id':c['section_id'],'title':title[c['section_id']],'range':[a,b],
       'body_characters':b-a,'embedding_characters':len(recomputed),'request_batch':1 if i<10 else 2,
       'request_index':i if i<10 else i-10,'request_sha256':requested_hashes[i],
       'vector':stats(c['vector']), 'paragraph_characters':[len(p) for p in source[a:b].split('\n\n') if p]})
  assert [sha(q.encode()) for q in queries]==qrecords[0]['input_sha256']
  retriever=KeywordRetriever();db=ScopedRepo(conn);query_audit=[]
  max_sql_error=0;max_padding_error=0
  for query,qvector in queries.items():
   lexical=await retriever.retrieve(db,query,[doc])
   lexical_ranks={r.snippet_offset:rank for rank,r in enumerate(lexical,1)}
   lexical_scores={r.snippet_offset:r.relevance_score for r in lexical}
   exact=sorted(chunks,key=lambda c:-cos(qvector,c['vector']))
   exact_ranks={(c['start_offset'],c['end_offset']):i for i,c in enumerate(exact,1)}
   db_ranks=await conn.fetch('SELECT section_id,start_offset,end_offset,1-(embedding <=> $1::vector) similarity FROM document_chunk_embeddings ORDER BY embedding <=> $1::vector, context_id,start_offset','['+','.join(map(str,qvector))+']')
   assert [r['section_id'] for r in db_ranks]==[c['section_id'] for c in exact]
   by_range={(r['start_offset'],r['end_offset']):r for r in db_ranks}
   rows=[]
   for c in exact:
    a,b=c['start_offset'],c['end_offset'];key=(a,b);lexrank=lexical_ranks.get(key);vrank=exact_ranks[key];vscore=cos(qvector,c['vector'])
    max_sql_error=max(max_sql_error,abs(vscore-by_range[key]['similarity']))
    max_padding_error=max(max_padding_error,abs(vscore-cos(qvector[:1024],c['vector'][:1024])))
    kw_score=1/(60+lexrank) if lexrank else 0
    v_score=1/(60+vrank)
    actual_rrf=kw_score+(v_score if vrank<=3 else 0)
    lower=source[a:b].lower();matches=[];false_substrings=[]
    for term in _tokenize(query):
     if term in lower:
      matches.append(term)
      if not re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',lower):
       snippets=[lower[max(0,m.start()-15):m.end()+15] for m in re.finditer(re.escape(term),lower)]
       false_substrings.append({'term':term,'examples':snippets[:3]})
    rows.append({'section_id':c['section_id'],'title':title[c['section_id']],'range':key,'vector_rank':vrank,'cosine':vscore,
                 'lexical_rank':lexrank,'lexical_score':lexical_scores.get(key),'literal_matches':matches,
                 'substring_only_matches':false_substrings,'current_rrf':actual_rrf,
                 'untruncated_rrf_arithmetic_only':kw_score+v_score})
   # Reproduce the current coordinator directly, supplying saved vectors only.
   class FixedVector:
    async def retrieve(self,*args):
     return [LongDocRetrievalResult(doc['id'],doc['uri'],'vector',c['section_id'],
            source[c['start_offset']:c['end_offset']],(c['start_offset'],c['end_offset']),cos(qvector,c['vector'])) for c in exact[:3]]
   coord=LongDocRetrievalCoordinator();coord.register_strategy('keyword',retriever);coord.register_strategy('vector',FixedVector())
   result=await coord.search_originals(db,query,[doc],query_embedding=qvector,top_k=3)
   encounter={}
   for key in [r.snippet_offset for r in lexical]+[(c['start_offset'],c['end_offset']) for c in exact]:
    if key not in encounter:encounter[key]=len(encounter)
   current=max(rows,key=lambda r:(r['current_rrf'],-encounter[r['range']]))
   untruncated=max(rows,key=lambda r:(r['untruncated_rrf_arithmetic_only'],-encounter[r['range']]))
   assert result[0]['section_id']==current['section_id']
   assert abs(result[0]['_rerank_score']-current['current_rrf'])<1e-14
   label='literal' if query=='1986 VAX 8800' else 'semantic'
   old=json.loads((ART/(label+'-agent-after.json')).read_text())['results'][0]
   assert result[0]['snippet']==old['content'] and result[0]['section_id']==old['section_id']
   query_audit.append({'query':query,'query_vector':stats(qvector),'keywords':_tokenize(query),
       'current_vector_limit':3,'lexical_candidates':len(lexical),'current_winner':current['section_id'],
       'untruncated_winner_arithmetic_only':untruncated['section_id'],'ranked_rows':rows})
  after=await db_snapshot(conn);assert after==before
 await conn.close()
 # Exercise response-index alignment locally using the same saved vector values.
 # This does not recreate the unrecorded historical provider response.
 offline=[]
 for batch in [chunks[:10],chunks[10:]]:
  async def handler(request):
   body=json.loads(request.content);assert len(body['input'])==len(batch)
   return httpx.Response(200,json={'data':[{'index':i,'embedding':batch[i]['vector'][:1024]} for i in reversed(range(len(batch)))]})
  client=OpenAIEmbeddingClient(api_key='offline-test',base_url='https://offline.invalid',model='text-embedding-v4',expected_dimensions=1536)
  await client._client.aclose();client._client=httpx.AsyncClient(base_url='https://offline.invalid',transport=httpx.MockTransport(handler))
  got=await client.embed_batch(['saved-input']*len(batch));await client.close()
  assert got==[c['vector'] for c in batch]
  offline.append({'response_items':len(batch),'reversed_response_array':'correctly_restored_by_index'})
 assert config_hashes()==protected_code and artifact_hashes()==protected_artifacts
 audit={'time_hong_kong':datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat(),'provider_calls':0,
  'database_access':'read-only transaction','code_and_original_artifacts_unchanged':True,
  'requested_models':sorted({r['model'] for r in records+qrecords}),
  'wire_request_from_client_code':{'fields':['input','model'],'dimensions':'omitted','text_type':'omitted','instruct':'omitted'},
  'vector_count':len(chunks),'source_sha256':sha(source.encode()),'input_checks':input_audit,
  'max_sql_vs_python_cosine_error':max_sql_error,'max_zero_padding_cosine_error':max_padding_error,
  'offline_response_alignment_test':offline,
  'historical_evidence_limit':'Raw provider response embeddings, response model/version, per-item response indices and response hashes were not saved. Correct mapping follows code inspection and local mock, not a byte-for-byte replay of the historical provider response.',
  'queries':query_audit,'database_before':before,'database_after':after}
 (OUT/'audit.json').write_text(json.dumps(audit,indent=2,default=str))
 (OUT/'pipelining-actual-embedding-input.txt').write_text(next(x['embedding_input'] for x in inputs if x['section_id']==10))
 print(json.dumps({'provider_calls':0,'models':audit['requested_models'],'vector_dimensions':[(x['title'],x['vector']) for x in input_audit],
    'padding_error':max_padding_error,'sql_error':max_sql_error,
    'queries':[{'query':q['query'],'keywords':q['keywords'],'lexical_candidates':q['lexical_candidates'],
               'current_winner':q['current_winner'],'untruncated_winner':q['untruncated_winner_arithmetic_only'],
               'rows':[{k:r[k] for k in ['section_id','title','vector_rank','cosine','lexical_rank','lexical_score','literal_matches','substring_only_matches','current_rrf','untruncated_rrf_arithmetic_only']} for r in sorted(q['ranked_rows'],key=lambda r:-r['current_rrf'])]} for q in query_audit]},indent=2))
 print(OUT)
asyncio.run(main())
