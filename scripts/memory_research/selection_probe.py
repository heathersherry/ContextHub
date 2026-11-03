"""Paired, read-only replay of saved MemDerive retrievals with one evidence selector.

No engine defaults, ingestion, embeddings, query rewriting or graph expansion change.
The official answer prompt, parser and deterministic scorers are imported unchanged.
Run without --execute to freeze inputs and verify reconstruction without API calls.
"""

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.request_parameters import RequestParameters
from contexthub.llm.retry_policy import RecordedCall, RetryPolicy
from contexthub.memory.context import ContextBuilder, ContextSelection
from contexthub.memory.contracts import ref_of
from contexthub.models.execution import CallIdentity
from contexthub.models.knowledge import canonical_hash
from contexthub.services.execution_ledger import validate_append


PROMPT_PATH = Path(__file__).with_name('selection_prompt.txt')
TOP_K = 15


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def append_json(path, value):
    with path.open('a') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_union(retrieval):
    # Include the original supplemental channels, so the control remains a subset.
    memories = {}
    for channel in ('text', 'vector', 'entity', 'topic'):
        for hit in retrieval[channel]:
            memory = hit['memory']
            if memory['ref'] in memories and memories[memory['ref']] != memory:
                raise ValueError('inconsistent candidate versions')
            memories[memory['ref']] = memory
    return [memories[ref] for ref in sorted(memories)]


def selector_prompt(question, memories):
    labels = {f'M{i}': memory for i, memory in enumerate(memories, 1)}
    payload = {'question': question, 'count': min(TOP_K, len(memories)), 'memories': [
        {'ref': label, **{key: memory[key] for key in ('text', 'entities', 'timestamp')}}
        for label, memory in labels.items()
    ]}
    return PROMPT_PATH.read_text() + '\n' + json.dumps(payload, ensure_ascii=False), labels


def parse_selection(raw, labels):
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {'selected_refs'}:
        raise ValueError('expected selected_refs only')
    refs = data['selected_refs']
    if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
            or len(refs) != min(TOP_K, len(labels)) or len(set(refs)) != len(refs)
            or any(ref not in labels for ref in refs)):
        raise ValueError('selection must contain the required number of distinct supplied refs')
    return [labels[ref] for ref in refs]


class SnapshotStore:
    def __init__(self, root):
        self.memories = {ref_of(row): row for row in read_jsonl(root / 'memories.jsonl')}
        self.provenance = {ref_of(row): row for row in read_jsonl(root / 'memory_sources.jsonl')}

    async def read_ref(self, scope, ref):
        return self.memories.get(ref)

    async def sources(self, scope, memory_id, version):
        return self.provenance.get(f'{memory_id}:{version}')


async def reconstruct_evidence(builder, memories, events, cutoff, purpose):
    from memory_bench.models import RetrievedItem

    result = await builder.result(ContextSelection(purpose, memories, '', False),
        SimpleNamespace(trace=SimpleNamespace(namespace='snapshot-replay'), session_seq=cutoff))
    evidence = []
    for item in result.items:
        source_events = [events[source.session_seq][source.message_no - 1]
                         for source in item.source_messages]
        ids = [event['id'] for event in source_events]
        evidence.append(RetrievedItem(
            text=item.text, rank=len(evidence) + 1,
            source_id=f"event_ids=[{', '.join(ids) if ids else 'none'}]",
            timestamp=source_events[-1]['timestamp'] if source_events else None,
            provenance=f'contexthub {item.type}/{item.origin}/{item.status}/{item.temporal_scope}',
            metadata={'event_ids': ids, **item.model_dump(exclude={'text'})},
        ))
    return evidence


class FileLedger:
    def __init__(self, path):
        self.path, self.records = path, []

    async def append(self, record):
        previous = [r for r in self.records if r.identity.call_id == record.identity.call_id]
        if validate_append(previous, record):
            append_json(self.path, record.model_dump(mode='json'))
            self.records.append(record)


async def recorded_completion(client, ledger, policy, config, question_id, stage, prompt,
                              code_version, *, max_tokens, validate=None):
    call = RecordedCall(
        identity=CallIdentity(account_id='memderive-selection-probe', execution_id=uuid4(),
            call_id=uuid4(), strategy='candidate-union-selector', stage=stage,
            operation=f'{stage}:{question_id}', model_role=stage, code_version=code_version,
            config_sha256=canonical_hash(config)),
        policy=policy, recorder=ledger, capture_raw_output=True, validate_response=validate,
    )
    tick = time.monotonic()
    raw = await client.complete(prompt, max_tokens=max_tokens, call=call)
    finished = next(r for r in reversed(ledger.records) if r.identity.call_id == call.identity.call_id
                    and r.phase == 'finished')
    return raw, {'call_id': str(call.identity.call_id), 'seconds': time.monotonic() - tick,
                 'usage': finished.usage.model_dump(mode='json'), 'actual_model': finished.actual_model}


async def prepare(root, output, count):
    from memory_bench.benchmarks.derivemem.prompts import build_answer_prompt
    from memory_bench.models import Question

    raw_path, = root.glob('q1/runs/*/*/raw_results.jsonl')
    state_path, = [p for p in root.glob('q1/state/*.jsonl') if not p.name.endswith('.attempts.jsonl')]
    old_rows = read_jsonl(raw_path)
    if not 2 <= count <= len(old_rows):
        raise ValueError('sample size must be between 2 and the number of questions')
    indices = [round(i * (len(old_rows) - 1) / (count - 1)) for i in range(count)]
    selected_rows = [old_rows[i] for i in indices]
    queries = {row['question'] for row in selected_rows}
    retrievals = {}
    for row in read_jsonl(root / 'memory_steps.jsonl'):
        if row['stage'] == 'retrieval' and row['payload'].get('query') in queries:
            query = row['payload']['query']
            if query in retrievals:
                raise ValueError('ambiguous saved query')
            retrievals[query] = row['payload']
    audit = read_jsonl(state_path)
    events = {row['sequence']: row['events'] for row in audit if row['kind'] == 'ingest_started'}
    query_times = {row['question_id']: row['query_time'] for row in audit if row['kind'] == 'retrieve'}
    builder = ContextBuilder(SnapshotStore(root), None)
    prepared = []
    for row in selected_rows:
        retrieval = retrievals[row['question']]
        union = candidate_union(retrieval)
        baseline = [hit['memory'] for hit in retrieval['selected'][:TOP_K]]
        if not {m['ref'] for m in baseline} <= {m['ref'] for m in union}:
            raise ValueError('candidate union omitted control evidence')
        evidence = await reconstruct_evidence(builder, baseline, events,
            retrieval['through_session'], retrieval['purpose'])
        if [asdict(item) for item in evidence] != row['retrieved']:
            raise ValueError('reconstructed baseline evidence differs from archived evidence')
        question = Question(id=row['question_id'], text=row['question'], index=row['question_index'],
                            category=row['category'], metadata={'query_time': query_times[row['question_id']]})
        prompt = build_answer_prompt(question.text, evidence,
            {'world_id': row['sample_id'].removeprefix('derivemem-'), '_question': question})
        if prompt != row['answer']['prompt']:
            raise ValueError('official baseline answer prompt differs from archived prompt')
        selection_prompt, labels = selector_prompt(question.text, union)
        prepared.append(dict(question=question, old=row, retrieval=retrieval, union=union,
            baseline=evidence, baseline_prompt=prompt, selection_prompt=selection_prompt, labels=labels))
    files = [raw_path, state_path, root / 'memory_steps.jsonl', root / 'memories.jsonl',
             root / 'memory_sources.jsonl', root / 'memory-config.json', root / 'q1.json',
             Path(__file__), PROMPT_PATH]
    manifest = {'prepared_at': datetime.now(timezone.utc).isoformat(), 'top_k': TOP_K,
        'sample_rule': 'round(i*(N-1)/(n-1)), i=0..n-1, official result order; chosen before calls',
        'question_ids': [row['question_id'] for row in selected_rows],
        'category_counts': dict(Counter(row['category'] for row in selected_rows)),
        'candidate_counts': [len(row['union']) for row in prepared],
        'files': {str(path.resolve()): sha(path) for path in files},
        'control': 'saved original top15, freshly answered',
        'treatment': 'saved text/vector/entity/topic candidate union, one DeepSeek selection, freshly answered',
        'changes_excluded': ['ingestion', 'embedding', 'query rewrite', 'graph expansion', 'answer prompt', 'top_k'],
        'baseline_evidence_and_prompts_exact_match': True,
        'transport': 'shared recorded chat client; official answer wire parameters; official prompt/parser/scorers',
        'pricing': 'token usage recorded; no monetary total without verified prices'}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'manifest.json', manifest)
    for row in prepared:
        append_json(output / 'prepared.jsonl', {'question_id': row['question'].id,
            'question': row['question'].text, 'union': row['union'],
            'baseline_refs': [item.metadata['ref'] for item in row['baseline']],
            'selection_prompt': row['selection_prompt'], 'baseline_prompt': row['baseline_prompt']})
    return prepared, builder, events, manifest


async def run(args):
    from memory_bench.benchmarks.derivemem.prompts import build_answer_prompt, parse_answer
    from memory_bench.models import AnswerResult, GroundTruth
    from memory_bench.scorers.derivemem import DERIVEMEM_METRICS, DeriveMemMetricScorer

    root, output = args.baseline.resolve(), args.output.resolve()
    prepared, builder, events, manifest = await prepare(root, output, args.count)
    print(json.dumps({'prepared': len(prepared), 'output': str(output), 'execute': args.execute}), flush=True)
    if not args.execute:
        return
    providers = json.loads(args.providers.read_text())['targets']
    memory_config = json.loads((root / 'memory-config.json').read_text())
    route = json.loads((root / 'q1.json').read_text())['models']['llm']
    deepseek, = [p for p in providers if p['label'] == 'deepseek']
    answer_provider, = [p for p in providers if p['label'] == route['provider']]
    assert deepseek['base_url'] == memory_config['chat_base_url']
    assert route['model'] in answer_provider['models']
    archive = str(output / 'responses')
    selector_parameters = RequestParameters.model_validate({**memory_config['request_parameters'],
        'response_archive_directory': archive})
    answer_parameters = RequestParameters.model_validate({
        'output_limit_field': 'max_completion_tokens', 'reasoning_effort': route['reasoning_effort'],
        'response_format': None, 'temperature_supported': True,
        'seed': {'support': 'unknown', 'enabled': False},
        'disabled_unknown_seed_authorization': 'Same no-seed answer protocol as the archived baseline',
        'response_archive_directory': archive})
    selector_policy = RetryPolicy.model_validate(memory_config['retry'])
    answer_policy = RetryPolicy(max_attempts=1, timeout_seconds=180, backoff_seconds=(),
                               retry_status_codes=(), retry_transport_errors=())
    config = {'selector': {'model': memory_config['chat_model'], 'base_url': deepseek['base_url'],
                          'request_parameters': selector_parameters.model_dump(mode='json')},
              'answer': {'model': route['model'], 'base_url': answer_provider['base_url'],
                         'request_parameters': answer_parameters.model_dump(mode='json'),
                         'temperature': route['temperature'], 'max_tokens': 2048},
              'selector_retry': selector_policy.model_dump(mode='json'),
              'answer_retry': answer_policy.model_dump(mode='json')}
    write_json(output / 'model-config.json', config)
    version = canonical_hash({'manifest': manifest, 'config': config})
    ledger = FileLedger(output / 'attempts.jsonl')
    selector = OpenAIChatClient(deepseek['api_key'], base_url=deepseek['base_url'],
        model=memory_config['chat_model'], request_parameters=selector_parameters)
    answerer = OpenAIChatClient(answer_provider['api_key'], base_url=answer_provider['base_url'],
        model=route['model'], temperature=route['temperature'], request_parameters=answer_parameters)
    started = time.monotonic()
    write_json(output / 'status.json', {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat()})
    try:
        for number, item in enumerate(prepared):
            question = item['question']
            raw, selection_audit = await recorded_completion(selector, ledger, selector_policy,
                config, question.id, 'selection', item['selection_prompt'], version, max_tokens=None,
                validate=lambda text: parse_selection(text, item['labels']))
            selected = parse_selection(raw, item['labels'])
            evidence = await reconstruct_evidence(builder, selected, events,
                item['retrieval']['through_session'], item['retrieval']['purpose'])
            selection = {'question_id': question.id, 'selected_refs': [m['ref'] for m in selected],
                         'candidate_refs': [m['ref'] for m in item['union']], 'raw': raw, **selection_audit}
            append_json(output / 'selections.jsonl', selection)
            arms = [('baseline', item['baseline']), ('selected', evidence)]
            if number % 2:
                arms.reverse()  # Balance answer-call order without changing question selection.
            for arm, arm_evidence in arms:
                prompt = build_answer_prompt(question.text, arm_evidence,
                    {'world_id': item['old']['sample_id'].removeprefix('derivemem-'), '_question': question})
                raw_answer, audit = await recorded_completion(answerer, ledger, answer_policy,
                    config, question.id, arm, prompt, version, max_tokens=2048)
                answer = AnswerResult(raw_response=raw_answer, answer=parse_answer(raw_answer))
                # Gold is attached only here, after both retrieval and generation.
                scored_question = replace(question, ground_truth=GroundTruth(**item['old']['ground_truth']))
                scores = [asdict(await DeriveMemMetricScorer(metric).score(
                    scored_question, answer, arm_evidence, {}, None)) for metric in DERIVEMEM_METRICS]
                result = {'question_id': question.id, 'arm': arm, 'category': question.category,
                    'question': question.text, 'retrieved': [asdict(e) for e in arm_evidence],
                    'prompt': prompt, 'answer': asdict(answer), 'scores': scores, **audit}
                append_json(output / 'results.jsonl', result)
                print(json.dumps({'question': question.id, 'arm': arm,
                    'answer': scores[0]['value'], 'witnessed': scores[1]['value'],
                    'seconds': round(audit['seconds'], 2)}), flush=True)
        write_json(output / 'status.json', {'status': 'complete', 'questions': len(prepared),
            'finished_at': datetime.now(timezone.utc).isoformat(), 'seconds': time.monotonic() - started})
    except BaseException as exc:
        write_json(output / 'status.json', {'status': 'failed', 'error_type': type(exc).__name__,
            'finished_at': datetime.now(timezone.utc).isoformat(), 'seconds': time.monotonic() - started})
        raise
    finally:
        await selector.close()
        await answerer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--providers', type=Path, default=Path('model_providers.local.json'))
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--execute', action='store_true')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
