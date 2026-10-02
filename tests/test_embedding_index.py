import json
from uuid import UUID
import httpx
import pytest
from contexthub.discovery.embedding_index import build_fact_index
from contexthub.discovery.search import SearchCandidate
from contexthub.llm.recorded_embedding import RecordedEmbeddingClient,EmbeddingConfig
from knowledge_helpers import identity,policy,MemorySink


def candidate(i,text):
    return SearchCandidate(id=UUID(int=i),kind='batch_proposition',proposition_id=UUID(int=i),
        sources=(),source_versions=(),text=text,evidence=(),explicit_provenance=())


@pytest.mark.asyncio
async def test_incremental_index_records_each_physical_call_and_reuses_exact_facts(tmp_path):
    requests=[]
    def handle(req):
        body=json.loads(req.content);requests.append(body['input'])
        return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.,float(i)]} for i in range(len(body['input']))],
            'usage':{'prompt_tokens':9,'total_tokens':9}})
    cfg=EmbeddingConfig(model='fixture',base_url='https://fixture.invalid',dimensions=2,send_dimensions=True,
        max_batch=2,response_archive_directory=str(tmp_path),retry=policy())
    client=RecordedEmbeddingClient(cfg,api_key='fixture',transport=httpx.MockTransport(handle));sink=MemorySink()
    try:
        first=await build_fact_index((candidate(1,'a'),candidate(2,'b'),candidate(3,'c')),
            history_sha256='a'*64,embedding_client=client,identity=identity(),recorder=sink)
        assert requests==[['a','b'],['c']]
        second=await build_fact_index((candidate(1,'a'),candidate(2,'changed'),candidate(4,'d')),
            history_sha256='b'*64,embedding_client=client,identity=identity(),recorder=sink,previous=first)
        assert requests[-1]==['changed','d'] and len(second.facts)==3
        assert second.facts[0].vector==first.facts[0].vector
        assert len([r for r in sink.rows if r.phase=='finished'])==3
        with pytest.raises(ValueError,match='cache_identity'):
            await build_fact_index((candidate(1,'a'),),history_sha256='c'*64,embedding_client=client,
                identity=identity().model_copy(update={'account_id':'other'}),recorder=sink,previous=second)
        assert len(requests)==3
    finally: await client.close()
