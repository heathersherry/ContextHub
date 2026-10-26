"""Translate the official backend protocol into the shared MemoryEngine API.

One public event is one ContextHub session. No gold, typed actions, diagnostic
nodes, or query metadata enter the engine. Runs start fresh; there is no resume
or checkpoint restoration contract in this version.
"""

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from memory_bench.models import BackendCapabilities, RetrievedItem, parse_timestamp_instant

from contexthub.memory.factory import MemoryConfig, create_memory_engine
from contexthub.memory.schemas import MemoryScope
from contexthub.memory.storage import SCOPE

ROOT = Path(__file__).resolve().parents[2]


def tree_hash(root, *, suffixes=None):
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if (path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'
                and (suffixes is None or path.suffix in suffixes)):
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()


def implementation_hash():
    # Include prompts and SQL, not just the Git commit of a possibly dirty tree.
    return hashlib.sha256((
        tree_hash(ROOT / 'src/contexthub')
        + tree_hash(ROOT / 'integrations/memderive', suffixes={'.py', '.json'})
    ).encode()).hexdigest()


def _write(path, value, *, mode='a'):
    with path.open(mode, encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')


@dataclass
class _Namespace:
    scope: MemoryScope
    audit_path: Path
    events: list[dict] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    failed: bool = False


class ContextHubBackend:
    name = 'contexthub'
    version = '1.0.0'
    memory_family = 'structural_topological'
    ingest_mode = 'ordered_incremental'
    capabilities = BackendCapabilities(
        supports_concurrent_retrieve=False,
        supports_concurrent_ingest=False,
        requires_ordered_ingest=True,
        requires_finalize=False,
        requires_wait_until_ready=False,
    )

    def __init__(self, *, memory_config, state_root, source_sha256,
                 session_timeout_seconds=1800):
        self.config = MemoryConfig.model_validate(memory_config)
        self.state_root = Path(state_root)
        self.source_sha256 = source_sha256
        self.session_timeout_seconds = session_timeout_seconds
        self.engine = None
        self.namespaces = {}
        self.run_id = uuid4().hex

    @property
    def metadata(self):
        return {
            'adapter_version': self.version,
            'source_sha256': self.source_sha256,
            'memory_config': self.config.model_dump(mode='json'),
            'ingest_granularity': 'per_event',
            'source_event_provenance': True,
            'read_only_retrieve': True,
            'checkpoint': False,
            'restore': False,
            'resume': False,
            'usage_accounting': 'ContextHub execution_attempts; excluded from DerivEval API totals',
        }

    async def initialize(self, context):
        if implementation_hash() != self.source_sha256:
            raise ValueError('ContextHub source changed; prepare a new experiment configuration')
        if self.engine is not None:
            raise ValueError('backend is already initialized')
        self.state_root.mkdir(parents=True, exist_ok=True)
        # Response archives belong to this run, even if a template used /tmp.
        config = self.config.model_copy(update={
            'embedding': self.config.embedding.model_copy(update={
                'response_archive_directory': str(self.state_root / self.run_id / 'responses'),
            }),
        })
        self.engine = await create_memory_engine(
            os.environ['MEMDERIVE_DATABASE_URL'], config,
            chat_key=os.environ['MEMDERIVE_CHAT_API_KEY'],
            embedding_key=os.environ['MEMDERIVE_EMBEDDING_API_KEY'],
            jev_key=os.environ.get('MEMDERIVE_JEV_API_KEY'),
        )

    async def create_namespace(self, namespace, *, fresh):
        if not fresh:
            raise ValueError('resume/restore is unsupported; start a fresh experiment')
        if self.engine is None:
            raise ValueError('backend is not initialized')
        if namespace in self.namespaces:
            raise ValueError('namespace already opened')
        scope = MemoryScope(tenant_id='memderive', namespace=uuid4().hex, owner_id=self.run_id)
        path = self.state_root / f'{scope.namespace}.jsonl'
        _write(path, {'kind': 'namespace', 'logical_namespace': namespace,
                      'scope': scope.model_dump(), 'backend': self.metadata}, mode='x')
        self.namespaces[namespace] = _Namespace(scope, path)

    def _state(self, namespace):
        state = self.namespaces[namespace]
        if state.failed:
            raise ValueError('ingestion failed; start a fresh experiment')
        return state

    async def ingest(self, events, namespace, metadata=None):
        if len(events) != 1 or (metadata or {}).get('ingest_granularity', 'per_event') != 'per_event':
            raise ValueError('ContextHub event provenance requires granularity: per_event')
        event = events[0]
        state = self._state(namespace)
        async with state.lock:
            self._state(namespace)
            if event.id in state.event_ids:
                raise ValueError('duplicate event ID; ingestion is not idempotent')
            if not event.id or not event.timestamp:
                raise ValueError('event ID and timestamp are required')
            instant = parse_timestamp_instant(event.timestamp)
            if state.events and instant < parse_timestamp_instant(state.events[-1]['timestamp']):
                raise ValueError('events must be in chronological order')
            # Explicit whitelist: the official adapter's public content includes
            # event ID, time and source. Do not forward event/query metadata.
            record = dict(id=event.id, role=event.role, content=event.content,
                          timestamp=event.timestamp, session_id=event.session_id,
                          speaker_id=event.speaker_id)
            sequence = len(state.events) + 1
            _write(state.audit_path, {'kind': 'ingest_started', 'sequence': sequence, 'event': record})
            try:
                result = await self.engine.ingest(
                    state.scope, sequence,
                    [dict(role=event.role, content=event.content, timestamp=event.timestamp)],
                    timestamp=event.timestamp, timeout_seconds=self.session_timeout_seconds,
                )
                _write(state.audit_path, {'kind': 'ingest_complete', 'sequence': sequence,
                                         'event_id': event.id, 'memory_refs': result.memory_refs})
            except BaseException:
                state.failed = True
                raise
            state.events.append(record)
            state.event_ids.add(event.id)
            return {'sequence': sequence, 'event_ids': [event.id], 'memory_refs': result.memory_refs}

    async def finalize(self, namespace):
        self._state(namespace)  # Engine.ingest completes synchronously.

    async def wait_until_ready(self, namespace):
        self._state(namespace)

    async def retrieve(self, query, namespace, top_k):
        if top_k < 1:
            raise ValueError('top_k must be positive')
        state = self._state(namespace)
        async with state.lock:
            self._state(namespace)
            if query.query_time and state.events:
                if parse_timestamp_instant(query.query_time) < parse_timestamp_instant(state.events[-1]['timestamp']):
                    raise ValueError('query predates ingested state; historical rollback is unsupported')
            result = await self.engine.context(state.scope, query.text)
            evidence = []
            for item in result.items[:top_k]:
                source_events = [state.events[seq - 1] for seq in item.source_sessions]
                ids = [event['id'] for event in source_events]
                # The standard v9 renderer now produces exactly the official
                # event-attributed line, without modifying the answer prompt.
                evidence.append(RetrievedItem(
                    text=item.text, rank=len(evidence) + 1,
                    source_id=f"event_ids=[{', '.join(ids) if ids else 'none'}]",
                    timestamp=source_events[-1]['timestamp'] if source_events else None,
                    provenance=f'contexthub {item.type}/{item.origin}/{item.status}/{item.temporal_scope}',
                    metadata={'event_ids': ids, **item.model_dump(exclude={'text'})},
                ))
            _write(state.audit_path, {'kind': 'retrieve', 'question_id': query.question_id,
                                     'query_time': query.query_time, 'sequence': result.sequence,
                                     'refs': [e.metadata['ref'] for e in evidence]})
            return evidence

    async def close(self):
        if self.engine is None:
            return
        try:
            # Preserve the original shared ledger, including retries and failed
            # calls. Official API-window totals do not include these calls.
            async with self.engine.store.pool.acquire() as conn:
                for state in self.namespaces.values():
                    rows = await conn.fetch(
                        f'SELECT * FROM execution_attempts WHERE {SCOPE} ORDER BY recorded_at,call_id,attempt_no,phase',
                        *state.scope.args,
                    )
                    path = state.audit_path.with_suffix('.attempts.jsonl')
                    with path.open('w', encoding='utf-8') as stream:
                        for row in rows:
                            stream.write(json.dumps(dict(row), default=str, ensure_ascii=False) + '\n')
        finally:
            await self.engine.close()
            self.engine = None
