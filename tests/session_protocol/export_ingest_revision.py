"""Export offline HTTP replay and memory-only ingest requests; no real calls."""
import asyncio, json, socket, sys
from pathlib import Path
from uuid import uuid4
import httpx
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.session_models import RecordedSessionModels, Verdict
from contexthub.services.session_state import Node, Edge
from test_session_service import service, source, config
from test_trial_wiring import Sink, parameters


def save(root,name,value):
    (root/name).write_text(json.dumps(value,ensure_ascii=False,indent=2))

async def main(root):
    root.mkdir(parents=True,exist_ok=False)
    fixture=json.loads((Path(__file__).parent/'fixtures/trial-think-response.json').read_text())
    body=fixture['original_http_response'];sink=Sink();requests=[]
    def provider(req):
        requests.append(json.loads(req.content));return httpx.Response(200,json=body)
    params=parameters().model_copy(update={'response_archive_directory':str(root/'responses')})
    client=OpenAIChatClient('offline-placeholder',model='offline',request_parameters=params,transport=httpx.MockTransport(provider))
    cfg=config(root);port=RecordedSessionModels(cfg,{'verifier':client},None,sink,'offline',uuid4())
    try:
        verdict=await port.ask('verifier','verify',json.loads(fixture['original_request']['messages'][0]['content'])['input'],Verdict,stage='P2')
        save(root,'wrapped-response-replay.json',{'origin':fixture['origin'],'real_calls':0,
            'usage':'Copied from archived response, not new spending',
            'semantics':'Only original schema acceptance checked; not a new semantic review',
            'parsed':verdict.model_dump(mode='json'),'attempts':[r.model_dump(mode='json') for r in sink.rows],
            'raw_response':port.last_response})
    finally:await client.close()
    s=service(root)
    await s.ingest_session(source(0,'My destination is Alpha. RAW_CHAT_MARKER_DO_NOT_SEND'))
    target=s.state.nodes[0].model_copy(update={'text':'My destination is Alpha.'})
    specs=[('parent','Travel allowed.'),('read','Budget approved.'),('rule','If destination is Alpha, permit is required.'),('conflict','My destination is Beta.')]
    specs += [(f'filler{i}',f'Unrelated hobby{i}.') for i in range(20)]
    extra=tuple(Node(id=i,version=1,text=t,kind='rule' if i=='rule' else 'assertion',status='fresh',evidence=target.evidence) for i,t in specs)
    s.state=s.state.model_copy(update={'nodes':(target,*extra),'edges':(Edge(upstream='parent',downstream=target.id,status='confirmed',evidence=target.evidence),)})
    s.read_ids[target.id]=('read',);await s._index()
    new_requests=[];new_sink=Sink()
    def memory_provider(req):
        p=json.loads(req.content);new_requests.append(p);inp=json.loads(p['messages'][0]['content'])['input']
        # Explicitly synthetic judgment. This export proves wire isolation only.
        response=dict(applicable=False,prerequisites_satisfied=False,conflicts_resolved=False,kind_confirmed=True,
            independent=False,memory_ids=[inp['target']['id']],dependency_ids=[],complete=False,missing='Synthetic unresolved conflict')
        return httpx.Response(200,json={'model':'offline','choices':[{'message':{'content':json.dumps(response)}}]})
    client=OpenAIChatClient('offline-placeholder',model='offline',request_parameters=parameters(),transport=httpx.MockTransport(memory_provider))
    s.models=RecordedSessionModels(s.config,{'verifier':client},None,new_sink,'offline',uuid4())
    try:await s._verify_ingest(target)
    finally:await client.close()
    payload=json.loads(new_requests[0]['messages'][0]['content'])['input']
    text=json.dumps(payload)
    assert 'RAW_CHAT_MARKER_DO_NOT_SEND' not in text
    assert not any('"'+k+'"' in text for k in ['sources','evidence','quote','messages','source_id'])
    assert len(payload['memory'])<len(s.state.nodes)
    assert {'parent','read','rule','conflict'}<={n['id'] for n in payload['memory']}
    save(root,'memory-only-http-requests.json',new_requests)
    save(root,'memory-only-audit.json',s.audits[-2:])
    save(root,'summary.json',{'real_calls':0,'database_connections':0,'archived_response_schema':'passed',
        'original_response_preserved':True,'conversion_recorded':True,'ingest_inventory_count':len(s.state.nodes),
        'ingest_sent_memory_count':len(payload['memory']),'ingest_sent_sources':0,'ingest_sent_raw_quotes':0,
        'synthetic_judgment_only':True,'human_semantic_review':'not performed'})
    print((root/'summary.json').read_text())

if __name__=='__main__':
    def deny(*a,**k):raise RuntimeError('offline: network forbidden')
    socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny;socket.getaddrinfo=deny
    asyncio.run(main(Path(sys.argv[1]).resolve()))
