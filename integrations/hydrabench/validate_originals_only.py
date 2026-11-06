"""One-source route ablation using live embeddings and the real search/tool path.

No production switch is added. The pure arm bypasses the first-route entry in
this process only; original retrieval, SQL, fusion, ACL, reads and tool projection
are unchanged. The control uses the same NoOp tree client as the prior sample.
Run prepare once, then compare. All artifact writes are exclusive.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
from unittest.mock import patch
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from fastapi import FastAPI

from validate_chunk_vectors import (
    URI, QUERIES, ROOT, RollbackRepository, ReplayQueries, protected_state, save, sha,
)
from contexthub.api.routers import search, documents
from contexthub.config import Settings
from contexthub.db.repository import PgRepository
from contexthub.llm.chat_client import NoOpChatClient
from contexthub.llm.factory import create_embedding_client
from contexthub.retrieval.long_doc import KeywordRetriever, LongDocRetrievalCoordinator, TreeRetriever
from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
from contexthub.retrieval.long_doc.ranges import document_ranges, embedding_input, enclosing_section
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.document_chunk_indexer import DocumentChunkIndexer
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService
import contexthub.services.retrieval_service as service_module
from contexthub_sdk import ContextHubClient
from contexthub_deepagents import create_contexthub_tools

OLD_DATABASE = 'hydra_chunks_sample_20261105'
DATABASE = 'hydra_originals_qwen37_20261105'
OUTPUT = ROOT / 'docs/hydrabench/artifacts/20261105-originals-only-qwen37'
DSN = os.environ['HYDRA_SAMPLE_DATABASE_URL']
assert DSN.rsplit('/', 1)[1] == DATABASE


def now():
    return datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat()


def vector_hash(values):
    return sha(struct.pack('<' + 'f' * len(values), *values))


def text_file(name, content):
    with (OUTPUT / name).open('x') as file:
        file.write(content)


async def snapshot(db):
    result = {}
    for table in ('contexts', 'document_sections', 'document_chunk_embeddings', 'retrieval_trace'):
        rows = await db.fetch(f'SELECT to_jsonb(t)::text AS data FROM {table} t')
        result[table] = {'count': len(rows), 'sha256': sha('\n'.join(sorted(r['data'] for r in rows)).encode())}
    return result


async def protected():
    state = await protected_state()
    conn = await asyncpg.connect(DSN.rsplit('/', 1)[0] + '/' + OLD_DATABASE,
                                server_settings={'default_transaction_read_only': 'on'})
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.account_id', 'acme', true)")
            state[OLD_DATABASE] = await snapshot(conn)
    finally:
        await conn.close()
    files = {}
    for relative in ('src', 'sdk/src', 'plugins/deepagents/src', 'tests',
                     'docs/hydrabench/artifacts/20261105-chunk-vectors',
                     'docs/hydrabench/artifacts/20261105-chunk-ranking-audit'):
        for path in (ROOT / relative).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                files[str(path.relative_to(ROOT))] = sha(path.read_bytes())
    state['existing_source_and_artifacts'] = files
    return state


def admin(database, sql):
    result = subprocess.run(['docker', 'exec', '-i', 'contexthub-hydra-smoke',
                             'psql', '-U', 'hydra_admin', '-d', database, '-v', 'ON_ERROR_STOP=1'],
                            input=sql, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr)


async def prepare():
    settings = Settings()
    assert settings.embedding_model == 'qwen3.7-text-embedding'
    assert settings.embedding_dimensions == 1536
    OUTPUT.mkdir(exist_ok=False)
    save(OUTPUT, 'protected-before.json', await protected())
    save(OUTPUT, 'configuration.json', {
        'started_at': now(), 'embedding_model': settings.embedding_model,
        'requested_dimensions': settings.embedding_dimensions,
        'database': DATABASE, 'source_database': OLD_DATABASE,
        'queries': QUERIES, 'top_k': 1, 'vector_candidate_budget': 3,
        'range_max_characters': 4000, 'rrf_constant': 60,
        'tree_control': 'NoOpChatClient; deterministic fallback, no provider call',
        'chat_calls': 0, 'answering_model_calls': 0,
    })
    admin('postgres', f'CREATE DATABASE {DATABASE} TEMPLATE {OLD_DATABASE};')
    admin(DATABASE, 'TRUNCATE document_chunk_embeddings; UPDATE contexts SET l0_embedding = NULL;')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    repo = PgRepository(pool)
    client = create_embedding_client(settings)
    calls = []

    async def capture(response):
        await response.aread()
        request = json.loads(response.request.content)
        payload = response.json()
        inputs = request['input']
        if isinstance(inputs, str):
            inputs = [inputs]
        record = {
            'request': request, 'response': payload,
            'status_code': response.status_code, 'route': response.request.url.path,
            'input_sha256': [sha(value.encode()) for value in inputs],
            'input_characters': [len(value) for value in inputs],
            'actual_dimensions': [len(item['embedding']) for item in payload.get('data', [])],
            'response_vector_float32_sha256': [vector_hash(item['embedding']) for item in payload.get('data', [])],
        }
        calls.append(record)
        save(OUTPUT, f'provider-call-{len(calls)}.json', record)

    client._client.event_hooks['response'] = [capture]
    try:
        async with repo.session('acme') as db:
            assert await db.fetchval('SELECT count(*) FROM contexts') == 1
            doc = dict(await db.fetchrow('SELECT * FROM contexts WHERE uri=$1', URI))
            local = OUTPUT / 'documents/microarchitecture'
            shutil.copytree(Path(doc['file_path']), local)
            doc['file_path'] = str(local)
            await db.execute('UPDATE contexts SET file_path=$1 WHERE id=$2', str(local), doc['id'])
            sections = [dict(r) for r in await db.fetch('SELECT * FROM document_sections WHERE context_id=$1 ORDER BY depth,section_id', doc['id'])]
            source = (local / 'extracted.txt').read_text()
            ranges = document_ranges(source, sections)
            assert len(ranges) == 16
            assert ''.join(source[a:b] for a,b in ranges) == source
            save(OUTPUT, 'fixture.json', {'document': doc, 'sections': sections, 'source_sha256': sha(source.encode())})
            save(OUTPUT, 'chunk-inputs.json', [
                {'range': [a,b], 'section_id': enclosing_section(sections,a,b)['section_id'],
                 'embedding_input': embedding_input(source,sections,a,b)} for a,b in ranges])
            # One L0 embedding is required only for a same-model mixed-route control.
            initial = await client.embed_batch([doc['l0_content'], *QUERIES.values()])
            assert len(initial) == 3 and all(value is not None for value in initial)
            save(OUTPUT, 'query-vectors.json', dict(zip(QUERIES.values(), initial[1:])))
            await db.execute('UPDATE contexts SET l0_embedding=$1::vector WHERE id=$2', json.dumps(initial[0]), doc['id'])
            assert await DocumentChunkIndexer(client).index(db, doc, source, sections) == 16
            # Verify provider response indexes map to the exact stored float32 vectors.
            pairs = {}
            for call in calls:
                for item in call['response']['data']:
                    pairs[call['request']['input'][item['index']]] = vector_hash(item['embedding'])
            rows = await db.fetch('SELECT section_id,start_offset,end_offset,embedding::text AS vector FROM document_chunk_embeddings ORDER BY start_offset')
            for row in rows:
                value = embedding_input(source,sections,row['start_offset'],row['end_offset'])
                assert vector_hash(json.loads(row['vector'])) == pairs[value]
            stored_l0 = await db.fetchval('SELECT l0_embedding::text FROM contexts WHERE id=$1',doc['id'])
            assert vector_hash(json.loads(stored_l0)) == pairs[doc['l0_content']]
            save(OUTPUT, 'index-check.json', {'chunks': len(rows), 'dimensions': 1536,
                 'all_provider_response_indexes_match_stored_vectors': True, 'zero_padding': False,
                 'source_characters': len(source), 'source_sha256': sha(source.encode()), 'completed_at': now()})
    finally:
        save(OUTPUT, 'provider-usage.json', [{
            'model': c['request']['model'], 'dimensions': c['request'].get('dimensions'),
            'response_model': c['response'].get('model'), 'input_count': len(c['input_sha256']),
            'status_code': c['status_code'], 'usage': c['response'].get('usage'),
        } for c in calls])
        await client.close()
        await pool.close()
    print(json.dumps({'database': DATABASE, 'embedding_requests': len(calls),
                      'embedding_inputs': sum(len(c['input_sha256']) for c in calls), 'chat_calls': 0}))


def compact_result(result):
    return {'section_id': result.section_id, 'source_range': result.snippet_offset,
            'score': result.relevance_score, 'strategy': result.strategy}


class RecordedStrategy:
    def __init__(self, strategy):
        self.strategy, self.calls = strategy, []

    async def retrieve(self, *args, **kwargs):
        result = await self.strategy.retrieve(*args, **kwargs)
        self.calls.append([compact_result(item) for item in result])
        return result


async def compare():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    repo = RollbackRepository(pool)
    try:
        async with repo.session('acme') as db:
            before = await snapshot(db)
            assert before['document_chunk_embeddings']['count'] == 16
            doc = dict(await db.fetchrow('SELECT * FROM contexts WHERE uri=$1', URI))
            sections = {r['section_id']: dict(r) for r in await db.fetch('SELECT * FROM document_sections')}
        source = (Path(doc['file_path']) / 'extracted.txt').read_text()
        vectors = json.loads((OUTPUT / 'query-vectors.json').read_text())
        comparison = {}
        original_l0_search = service_module.vector_search
        for mode in ('mixed', 'originals_only'):
            records = []
            embedding = ReplayQueries(vectors)
            coordinator = LongDocRetrievalCoordinator()
            tree = RecordedStrategy(TreeRetriever(NoOpChatClient()))
            keyword = RecordedStrategy(KeywordRetriever())
            vector = RecordedStrategy(ChunkVectorRetriever())
            coordinator.register_strategy('tree', tree)
            coordinator.register_strategy('keyword', keyword)
            coordinator.register_strategy('vector', vector)
            acl = ACLService()
            service = RetrievalService(
                RetrievalRouter.default() if mode == 'mixed' else RetrievalRouter(None),
                embedding, acl, masking_service=MaskingService(), long_doc_coordinator=coordinator)
            app = FastAPI(); app.include_router(search.router); app.include_router(documents.router)
            app.state.repo = repo; app.state.retrieval_service = service
            app.state.acl_service = acl; app.state.audit_service = AuditService()
            app.state.masking_service = MaskingService(); app.state.lifecycle_service = None
            client = ContextHubClient('http://sample.test', 'local', 'acme', 'query-agent')
            await client._http.aclose()
            client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://sample.test',
                headers={'X-Account-Id': 'acme', 'X-Agent-Id': 'query-agent'})
            l0_calls = []
            async def first_route(*args, **kwargs):
                if mode == 'originals_only':
                    l0_calls.append({'disabled': True})
                    return []
                result = await original_l0_search(*args, **kwargs)
                l0_calls.append({'disabled': False, 'candidates': [
                    {'id': str(row['id']), 'cosine': row.get('cosine_similarity')} for row in result]})
                return result
            try:
                tool = next(t for t in create_contexthub_tools(client) if t.name == 'contexthub_search')
                with patch.object(service_module, 'vector_search', first_route):
                    for label, query in QUERIES.items():
                        msg = await tool.ainvoke({'type': 'tool_call', 'id': mode+'-'+label,
                            'name': 'contexthub_search', 'args': {'query': query, 'context_type': ['resource'], 'top_k': 1, 'level': 'L1'}})
                        assert msg.status == 'success', msg.content
                        payload = json.loads(msg.content)
                        save(OUTPUT, f'{label}-agent-{mode}.json', payload)
                        assert len(payload['results']) == 1
                        hit = payload['results'][0]; body = hit['content']
                        assert hit['uri'] == URI and hit['version'] == doc['version']
                        assert body in source and hit['content_level'] == 'snippet'
                        assert not {'l0_content', 'l1_content', 'l2_content', 'snippet'} & hit.keys()
                        text_file(f'{label}-content-{mode}.txt', body)
                        section_read = await client.document.read_section(str(doc['id']),hit['section_id'])
                        assert section_read.content == source[section_read.start_offset:section_read.end_offset]
                        record = {'label': label, 'query': query, 'section_id': hit['section_id'],
                            'section_title': sections[hit['section_id']]['title'], 'strategy': hit['retrieval_strategy'],
                            'source_range': [source.index(body),source.index(body)+len(body)],
                            'content_characters': len(body), 'estimated_content_tokens_chars_div_4': len(body)/4,
                            'tool_message_characters': len(msg.content), 'body_sha256': sha(body.encode()),
                            'final_score': hit['score'], 'l0_route': l0_calls[-1],
                            'keyword_candidates': keyword.calls[-1], 'vector_candidates': vector.calls[-1],
                            'tree_candidates': tree.calls[-1] if tree.calls else [], 'section_read_verified': True}
                        records.append(record)
                assert embedding.calls == list(QUERIES.values())
                assert len(tree.calls) == (2 if mode == 'mixed' else 0)
                assert len(keyword.calls) == len(vector.calls) == 2
                comparison[mode] = records
            finally:
                await client.aclose()
        async with repo.session('acme') as db:
            assert await snapshot(db) == before
            ranks = {}
            for label, query in QUERIES.items():
                result = await ChunkVectorRetriever().retrieve(db, vectors[query], [doc], 16)
                ranks[label] = [dict(compact_result(r), title=sections[r.section_id]['title']) for r in result]
            save(OUTPUT, 'full-vector-ranks.json', ranks)
        async with repo.session('other-account') as db:
            assert await db.fetchval('SELECT count(*) FROM document_chunk_embeddings') == 0
            assert await db.fetchval('SELECT count(*) FROM contexts') == 0
        comparison['checks'] = {
            'completed_at': now(), 'body_equal_by_query': {
                label: comparison['mixed'][i]['body_sha256'] == comparison['originals_only'][i]['body_sha256']
                for i,label in enumerate(QUERIES)},
            'same_query_vectors_both_modes': True, 'search_provider_calls': 0, 'chat_calls': 0,
            'answering_model_calls': 0, 'sample_data_unchanged_by_search': True,
            'other_account_sees_no_contexts_or_chunks': True,
            'scope': 'One source, two queries. No live tree-selection or answering model. No corpus-wide noise conclusion.',
        }
        save(OUTPUT, 'comparison.json', comparison)
        after = await protected()
        assert after == json.loads((OUTPUT / 'protected-before.json').read_text())
        save(OUTPUT, 'protected-after.json', after)
        print(json.dumps({mode: [{k:r[k] for k in ('label','section_id','section_title','content_characters','final_score')} for r in comparison[mode]]
                          for mode in ('mixed','originals_only')}, ensure_ascii=False))
        print(json.dumps(comparison['checks']))
    finally:
        await pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare','compare'))
    asyncio.run(prepare() if parser.parse_args().phase == 'prepare' else compare())
