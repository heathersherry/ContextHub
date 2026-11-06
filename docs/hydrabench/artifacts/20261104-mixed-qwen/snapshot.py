import asyncio
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from contexthub.config import Settings
from contexthub.db.pool import create_pool
from contexthub.db.repository import PgRepository

async def main():
    settings = Settings()
    expected = json.loads(Path('data/hydrabench-expanded/index/index-manifest.json').read_text())
    assert settings.embedding_model == expected['embedding_model']
    assert settings.embedding_dimensions == expected['embedding_dimensions']
    assert settings.rerank_strategy == 'by_type'
    pool = await create_pool(settings)
    try:
        async with PgRepository(pool).session('acme') as db:
            rows = await db.fetch("""
                SELECT context_type, count(*) AS count,
                       count(l0_embedding) AS embedded,
                       count(file_path) AS long_document_paths,
                       sum(adopted_count) AS adopted_count,
                       sum(ignored_count) AS ignored_count,
                       md5(string_agg(
                         uri || ':' || version::text || ':' || status || ':' || validity_status || ':' ||
                         scope || ':' || coalesce(owner_space,'') || ':' ||
                         md5(coalesce(l0_content,'')) || ':' || md5(coalesce(l1_content,'')) || ':' ||
                         md5(coalesce(l2_content,'')) || ':' || md5(l0_embedding::text),
                         E'\\n' ORDER BY uri)) AS content_and_vector_fingerprint
                  FROM contexts GROUP BY context_type ORDER BY context_type
            """)
    finally:
        await pool.close()
    counts = [dict(r) for r in rows]
    assert sum(r['count'] for r in counts) == 18340
    assert all(r['count']==r['embedded'] and r['long_document_paths']==0 and r['adopted_count']==0 and r['ignored_count']==0 for r in counts)
    tracked = ['integrations/hydrabench/run.py', 'integrations/hydrabench/retrieval_tools.py', 'integrations/hydrabench/sqlite_tool.py', 'integrations/hydrabench/probe.py', 'data/hydrabench-expanded/selected-private.json',
               'data/hydrabench-expanded/text-support-labels.json',
               'src/contexthub/config.py', 'src/contexthub/main.py',
               'src/contexthub/retrieval/router.py', 'src/contexthub/retrieval/rerank.py',
               'src/contexthub/services/retrieval_service.py']
    output = {'counts':counts, 'settings':{'rerank_strategy':settings.rerank_strategy,
              'embedding_model':settings.embedding_model, 'embedding_dimensions':settings.embedding_dimensions,
              'search_over_retrieve_factor':settings.search_over_retrieve_factor,
              'embedding_endpoint_host':urlsplit(settings.openai_base_url).hostname,
              'propagation_enabled':settings.propagation_enabled,'lifecycle_enabled':settings.lifecycle_enabled,
              'chat_model':settings.chat_model,'chat_enable_thinking':settings.chat_enable_thinking,'chat_model_used':True},
              'sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in tracked},
              'packages':{p:importlib.metadata.version(p) for p in ['asyncpg','httpx','openai','pydantic','uvicorn']}}
    Path(sys.argv[1]).write_text(json.dumps(output,indent=2,default=str))
    print(json.dumps({'settings':output['settings'],'counts':counts},indent=2,default=str))

asyncio.run(main())
