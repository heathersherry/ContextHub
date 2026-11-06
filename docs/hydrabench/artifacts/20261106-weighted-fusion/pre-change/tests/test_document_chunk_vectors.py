"""Original ranges, atomic indexing, semantic discovery and evidence fusion."""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from contexthub.errors import ServiceUnavailableError
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.retrieval.long_doc.coordinator import LongDocRetrievalCoordinator
from contexthub.retrieval.long_doc.ranges import document_ranges, embedding_input
from contexthub.retrieval.long_doc.result import LongDocRetrievalResult
from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
from contexthub.services.document_chunk_indexer import DocumentChunkIndexer


def sections_for(text):
    left = text.index('## First')
    right = text.index('## Second')
    return [dict(section_id=1, parent_id=None, node_id='root', title='Document', depth=0,
                 start_offset=0, end_offset=len(text)),
            dict(section_id=2, parent_id=1, node_id='heading-0001', title='Guide', depth=1,
                 start_offset=0, end_offset=len(text)),
            dict(section_id=3, parent_id=2, node_id='heading-0002', title='First', depth=2,
                 start_offset=left, end_offset=right),
            dict(section_id=4, parent_id=2, node_id='heading-0003', title='Second', depth=2,
                 start_offset=right, end_offset=len(text))]


def test_ranges_cover_source_once_and_keep_intro_and_chapter_boundaries():
    text = '# Guide\nIntroduction.\n\n## First\n' + 'A complete sentence. ' * 60 + '\n\n## Second\nFinal evidence.'
    sections = sections_for(text)
    ranges = document_ranges(text, sections, 100)
    assert ''.join(text[a:b] for a,b in ranges) == text
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(a < b and b-a <= 100 for a,b in ranges)
    assert all(a[1] == b[0] for a,b in zip(ranges,ranges[1:]))
    for section in sections[2:]:
        assert all(not (a < section['start_offset'] < b) for a,b in ranges)
    start, end = ranges[-1]
    value = embedding_input(text, sections, start, end)
    assert value.startswith('Guide > Second\n\n')
    assert value.endswith(text[start:end])


@pytest.mark.asyncio
async def test_indexer_batches_originals_and_writes_no_bodies_or_summaries():
    text = '# Guide\nIntro.\n\n## First\nEvidence.\n\n## Second\nMore evidence.'
    inputs, writes = [], []
    class Embedding:
        async def embed_batch(self, texts):
            inputs.extend(texts)
            return [[1., 0.] for _ in texts]
    class DB:
        async def execute(self, sql, *args):
            assert 'l0_content' not in sql and 'summary' not in sql
            writes.append(args)
    document = {'id': uuid4(), 'version': 7}
    count = await DocumentChunkIndexer(Embedding()).index(DB(), document, text, sections_for(text))
    assert count == len(inputs) == len(writes) == 3
    assert all(args[0:2] == (document['id'], 7) for args in writes)
    assert all(args[5] == sha256(text.encode()).hexdigest() for args in writes)
    assert all(args[6] == '[1.0,0.0]' for args in writes)


@pytest.mark.asyncio
async def test_incomplete_embedding_batch_saves_nothing_and_noop_is_explicit():
    class Embedding:
        async def embed_batch(self, texts):
            return [[1.], None, [1.]]
    text = '# Guide\nIntro.\n\n## First\nOne.\n\n## Second\nTwo.'
    with pytest.raises(ServiceUnavailableError):
        await DocumentChunkIndexer(Embedding()).index(SimpleNamespace(), {'id': uuid4(), 'version': 1}, text, sections_for(text))
    assert await DocumentChunkIndexer(NoOpEmbeddingClient()).index(SimpleNamespace(), {}, text, []) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['none', 'version', 'source'])
async def test_vector_reads_exact_evidence_and_rejects_changed_source_or_version(tmp_path, changed):
    text = 'The original evidence.'
    (tmp_path/'extracted.txt').write_text(text if changed != 'source' else 'Replacement content.')
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 4, 'file_path': str(tmp_path)}
    class DB:
        async def fetch(self, sql, *args):
            assert 'e.context_version = c.version' in sql
            assert "e.account_id = current_setting('app.account_id')" in sql
            assert args[1] == [doc['id']]
            return [dict(context_id=doc['id'], context_version=3 if changed=='version' else 4,
                         section_id=9,start_offset=4,end_offset=len(text),
                         source_sha256=sha256(text.encode()).hexdigest(),similarity=0.75)]
    results = await ChunkVectorRetriever().retrieve(DB(), [1.], [doc], 3)
    if changed != 'none':
        assert results == []
    else:
        assert results[0].snippet == text[4:]
        assert results[0].snippet_offset == (4,len(text))
        assert results[0].strategy == 'vector'


@pytest.mark.asyncio
async def test_fuse_same_range_without_overwriting_different_range_by_route_order():
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 1}
    good = LongDocRetrievalResult(doc['id'], doc['uri'], 'keyword', 7, 'first evidence', (0,14), 8)
    other = replace(good, strategy='vector', section_id=8, snippet='different evidence', snippet_offset=(15,33), relevance_score=.9)
    class Keyword:
        async def retrieve(self, *args): return [good]
    class Vector:
        async def retrieve(self, *args): return [other, replace(good,strategy='vector',relevance_score=.8)]
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None,'query',[doc],query_embedding=[1.])
    assert [hit['snippet'] for hit in hits] == ['first evidence', 'different evidence']
    assert hits[0]['retrieval_strategy'] == 'hybrid'
    assert hits[0]['_rerank_score'] == pytest.approx(1/61+1/62)
    assert hits[1]['_rerank_score'] == pytest.approx(1/61)
    assert len({hit['snippet_offset'] for hit in hits}) == 2


@pytest.mark.asyncio
async def test_semantic_original_route_does_not_require_keyword_or_l0_match():
    doc = {'id': uuid4(), 'uri': 'ctx://resources/example', 'version': 1}
    class Keyword:
        async def retrieve(self, *args): return []
    class Vector:
        async def retrieve(self, db, vector, documents, top_k):
            assert vector == [1.] and documents == [doc]
            return [LongDocRetrievalResult(doc['id'],doc['uri'],'vector',2,'original',(0,8),.8)]
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword', Keyword())
    coordinator.register_strategy('vector', Vector())
    hits = await coordinator.search_originals(None,'paraphrase',[doc],query_embedding=[1.])
    assert hits[0]['snippet'] == 'original'
