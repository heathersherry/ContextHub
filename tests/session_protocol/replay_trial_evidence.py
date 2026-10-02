"""Export strictly offline replay evidence. Synthetic later ports are labeled."""
import asyncio
import copy
import json
from pathlib import Path
import socket
import sys

import httpx
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.session_memory import SessionInput
from contexthub.services.session_models import RecordedSessionModels
from test_session_service import service
from test_trial_wiring import Sink, parameters


def deny(*args, **kwargs):
    raise RuntimeError('offline: network forbidden')


async def replay(output, mode):
    fixture = json.loads((Path(__file__).parent / 'fixtures/trial-extraction.json').read_text())
    source = SessionInput.model_validate(fixture['source'])
    body = copy.deepcopy(fixture['original_http_response'])
    if mode != 'original':
        adapted = copy.deepcopy(fixture['adapted_response'])
        # Explicit offline fixture adaptation, never a production response conversion.
        for item in adapted['items']:
            item['read_memory_ids'] = item.pop('read_ids', [])
        if mode == 'invalid_last_id':
            adapted['items'][-1]['evidence_ids'] = ['E999999']
        body['choices'][0]['message']['content'] = json.dumps(adapted, ensure_ascii=False)
    requests = []
    def provider(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=body)
    s = service(output)
    params = parameters().model_copy(update={'response_archive_directory': str(output / 'responses')})
    s.config.models['extraction'] = s.config.models['extraction'].model_copy(update={'request_parameters': params})
    client = OpenAIChatClient('offline-placeholder', model='offline', temperature=0,
        request_parameters=params, transport=httpx.MockTransport(provider))
    sink = Sink()
    port = RecordedSessionModels(s.config, {'extraction': client}, None, sink, s.store.account_id, s.store.stream_id)
    synthetic = s.models.ask
    async def ask(role, operation, payload, schema, **kwargs):
        s.models.last_response = None
        if operation != 'extract':
            return await synthetic(role, operation, payload, schema, **kwargs)
        try:
            return await port.ask(role, operation, payload, schema, **kwargs)
        finally:
            s.models.call_ids = list(port.call_ids)
            s.models.last_response = port.last_response
    s.models.ask = ask
    error = None
    try:
        await s.ingest_session(source)
    except Exception as exc:
        error = dict(type=type(exc).__name__, details=s.failure)
    finally:
        await client.close()
    checkpoint = await s.store.load()
    result = dict(mode=mode, real_calls=0, database_connections=0,
        provenance=fixture['origin'], adaptation=fixture['adaptation'] if mode != 'original' else None,
        semantics='Later verifier/dependency/embedding are synthetic; fresh is NOT a semantic score.',
        usage='Mock response copies original usage; not new token spending.',
        error=error, checkpoint=checkpoint.model_dump(mode='json'),
        requests=requests, attempts=[r.model_dump(mode='json') for r in sink.rows])
    (output / 'replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return dict(mode=mode, status=checkpoint.status, nodes=len(checkpoint.state.nodes),
                read_ids=len(checkpoint.read_ids), error=error['type'] if error else None)


async def main(directory):
    directory.mkdir(parents=True, exist_ok=False)
    summaries = []
    for mode in ('original', 'ids', 'invalid_last_id'):
        output = directory / mode
        output.mkdir()
        summaries.append(await replay(output, mode))
    (directory / 'summary.json').write_text(json.dumps(dict(real_calls=0,
        database_connections=0, replays=summaries), ensure_ascii=False, indent=2))
    print(json.dumps(summaries))


if __name__ == '__main__':
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    asyncio.run(main(Path(sys.argv[1]).resolve()))
