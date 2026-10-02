"""Offline synthetic request measurements; intentionally imports only test doubles."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path


def deny(*args, **kwargs):
    raise RuntimeError('offline: network forbidden')


socket.socket.connect = socket.socket.connect_ex = deny
socket.create_connection = socket.getaddrinfo = deny

from test_session_service import Models, service, source
from test_session_revision import BasisModels, RawModels
from contexthub.services.session_knowledge_service import SessionKnowledgeService


def summarize(audits):
    rows = [a for a in audits if a['operation'] == 'request']
    return dict(request_count=len(rows), input_characters=sum(a['input_characters'] for a in rows),
        sent_source_count=sum(len(a['raw_sources']) for a in rows),
        sent_message_count=sum(a['raw_message_count'] for a in rows),
        sent_memory_count=sum(a['memory_count'] for a in rows), requests=rows,
        measured_model_tokens=None, estimated_tokens=None, billed_amount=None)


async def main(output):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        m = Models()
        m.reject_repair = True
        s = service(root, m)
        await s.ingest_session(source(0, 'My hobby is Chess.'))
        await s.ingest_session(source(1, 'My lead is Alice.', 'My recipient is Alice.',
            'If my lead changes, my recipient will be Bob.'))
        stopped = summarize(s.audits)
        s = SessionKnowledgeService(s.config, s.store, m)
        unrelated = []
        for ordinal in (2, 3):
            await s.ingest_session(source(ordinal, 'ordinary chat'))
            unrelated.append(summarize(s.audits))
        b = service(root, BasisModels(), profile='full/strong-only')
        await b.ingest_session(source(0, 'My hotel is Alpha.', 'My shuttle is available.'))
        hotel, shuttle = b.state.nodes
        b.audits = []
        b.attempts[hotel.id]["trigger"] = "synthetic-relevant-basis-change"
        b._apply(invalidated=(hotel.id,))
        await b._repair(hotel.id)
        basis = summarize(b.audits)
        basis.update(content_version=b._nodes()[hotel.id].version,
            basis_revision=b._nodes()[hotel.id].basis_revision,
            dependency_confirmed=any(e.upstream == shuttle.id for e in b.state.edges))
        r = service(root, RawModels())
        await r.ingest_session(source(0, 'My hotel is Alpha.'))
        r.checkpoint = r.checkpoint.model_copy(update={'sources': (*r.checkpoint.sources,
            source(1, 'The office is elsewhere only on Fridays.', 'It is closed otherwise.'))})
        r.audits = []
        await r._dependencies(r.state.nodes[0].id)
        examples = dict(memory_first=next(c[2] for c in r.models.calls if c[1] == 'dependency'),
            demanded_raw=[c[2] for c in r.models.calls if c[1] == 'dependency'][-1],
            stages=[dict(role=c[0], payload=c[2]) for c in m.calls if c[1] == 'generate'])
        result = dict(protocol='session-evidence-v2', synthetic_only=True,
            measurement='Constructed model input JSON characters including instruction and schema; no real tokens or fees.',
            stopped_initial=stopped, unrelated_sessions=unrelated, unchanged_new_basis=basis,
            raw_lookup=summarize(r.audits), examples=examples)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({k: {name: val for name, val in v.items() if name != 'requests'}
                         for k, v in result.items() if isinstance(v, dict) and k != 'examples'}, ensure_ascii=False))


if __name__ == '__main__':
    import sys
    asyncio.run(main(Path(sys.argv[1])))
