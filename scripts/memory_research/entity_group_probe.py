"""Compare q001 query-cosine selection against the frozen v2 selection.

No ingestion, embedding service, selector model, or default retriever modification.
Neighbor admission uses cosine thresholds; direct relevance has no similarity
deduplication. Identical prompts share one recorded answer call.
"""

import argparse
import ast
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4

import numpy as np

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.request_parameters import RequestParameters
from contexthub.llm.retry_policy import RecordedCall, RetryPolicy
from contexthub.memory.context import ContextBuilder
from contexthub.memory.contracts import material
from contexthub.models.execution import CallIdentity
from contexthub.models.knowledge import canonical_hash
from scripts.memory_research.entity_groups import (
    CONFIG, EntityGroups, eligible_facts, normalize_months, split_query_cutoff,
)
from scripts.memory_research.selection_probe import (
    FileLedger, SnapshotStore, append_json, candidate_union, read_jsonl,
    reconstruct_evidence, sha, write_json,
)


async def prepare(args):
    from memory_bench.benchmarks.derivemem.prompts import build_answer_prompt
    from memory_bench.models import Question

    root, output = args.baseline.resolve(), args.output.resolve()
    thresholds = args.neighbor_thresholds
    if len(set(thresholds)) != len(thresholds) or any(not -1 <= value <= 1 for value in thresholds):
        raise ValueError('provide distinct cosine thresholds in [-1, 1]')
    frozen = args.control_v2.resolve()
    old_selector = ast.parse((frozen / 'entity_groups.py').read_text())
    new_selector = ast.parse(Path(__file__).with_name('entity_groups.py').read_text())
    old_class = next(node for node in old_selector.body if isinstance(node, ast.ClassDef))
    new_class = next(node for node in new_selector.body if isinstance(node, ast.ClassDef))
    old_methods = {node.name: node for node in old_class.body if isinstance(node, ast.FunctionDef)}
    new_methods = {node.name: node for node in new_class.body if isinstance(node, ast.FunctionDef)}
    unchanged = sorted(set(old_methods) - {'__init__', 'groups', 'contribution'})
    for name in unchanged:
        assert ast.dump(old_methods[name]) == ast.dump(new_methods[name]), name
    assert [ast.dump(n) for n in old_methods['__init__'].body] == [
        ast.dump(n) for n in new_methods['__init__'].body[2:]]
    control_offline = json.loads((frozen / 'offline.json').read_text())
    raw_path, = root.glob('q1/runs/*/*/raw_results.jsonl')
    state_path, = [p for p in root.glob('q1/state/*.jsonl') if not p.name.endswith('.attempts.jsonl')]
    old, = [row for row in read_jsonl(raw_path) if row['question_id'] == 'q001']
    retrieval, = [row['payload'] for row in read_jsonl(root / 'memory_steps.jsonl')
                  if row['stage'] == 'retrieval' and row['payload'].get('query') == old['question']]
    audit = read_jsonl(state_path)
    events = {row['sequence']: row['events'] for row in audit if row['kind'] == 'ingest_started'}
    query_audit, = [row for row in audit if row['kind'] == 'retrieve' and row['question_id'] == 'q001']
    question = Question(id='q001', text=old['question'], index=old['question_index'],
                        category=old['category'], metadata={'query_time': query_audit['query_time']})
    world = {'world_id': old['sample_id'].removeprefix('derivemem-'), '_question': question}
    store = SnapshotStore(root)
    builder = ContextBuilder(store, None)
    union = candidate_union(retrieval)
    control = [hit['memory'] for hit in retrieval['selected'][:CONFIG['top_k']]]
    representative = store.memories[control[0]['ref']]
    scope = {key: representative[key] for key in ('tenant_id', 'namespace', 'owner_id')}
    rows = eligible_facts(store.memories.values(), scope, retrieval['through_session'])
    assert {m['ref'] for m in union} <= rows.keys()
    vector_archive = json.loads(args.query_embedding.read_text())
    assert vector_archive['request']['input'] == [question.text]
    query_vector = json.loads(vector_archive['response_text'])['data'][0]['embedding']
    query_terms = json.loads(args.query_lexemes.read_text())
    assert query_terms['query'] == question.text
    cutoff, content_query = split_query_cutoff(question.text)
    assert query_terms['content_query'] == content_query
    assert query_terms['cutoff'] == cutoff
    content_lexemes = normalize_months(query_terms['content_lexemes'], content_query)
    original_evidence = await reconstruct_evidence(builder, control, events,
        retrieval['through_session'], retrieval['purpose'])
    assert [asdict(item) for item in original_evidence] == old['retrieved']
    assert build_answer_prompt(question.text, original_evidence, world) == old['answer']['prompt']
    control_refs = control_offline['entity_groups']['selected_refs']
    assert control_offline['initial_refs'] == [memory['ref'] for memory in union]
    control_evidence = await reconstruct_evidence(builder, [material(rows[ref]) for ref in control_refs],
        events, retrieval['through_session'], retrieval['purpose'])
    control_prompt = build_answer_prompt(question.text, control_evidence, world)
    assert [asdict(e) for e in control_evidence] == control_offline['arms']['entity_groups']['retrieved']
    assert control_prompt == control_offline['arms']['entity_groups']['prompt']
    files = [Path(__file__), Path(__file__).with_name('entity_groups.py'),
             Path(__file__).with_name('selection_probe.py'), root / 'memories.jsonl',
             root / 'memory_sources.jsonl', root / 'memory_steps.jsonl', root / 'memory-config.json',
             root / 'q1.json', raw_path, state_path, args.query_embedding, args.query_lexemes,
             Path('src/contexthub/memory/context.py'), Path('src/contexthub/memory/contracts.py'),
             frozen / 'entity_groups.py', frozen / 'offline.json']
    manifest = {'prepared_at': datetime.now(timezone.utc).isoformat(), 'question_id': question.id,
        'config': CONFIG, 'neighbor_thresholds': thresholds, 'scope': scope, 'cutoff': retrieval['through_session'],
        'eligible_facts': len(rows), 'initial_candidates': len(union),
        'files': {str(path.resolve()): sha(path) for path in files},
        'selection_inputs': ['archived full-query embedding', 'content-only query lexemes with normalized months',
                             'eligible fact embeddings/lexemes/entities', 'initial candidate refs'],
        'gold_used_for_selection': False, 'selector_calls': 0, 'new_embedding_calls': 0,
        'protocol': 'frozen v2 top15 vs cosine-threshold neighbors and direct query relevance without similarity deduplication; unchanged official answer protocol',
        'unchanged_selector_methods': unchanged,
        'identical_prompts': 'one answer call per distinct prompt; shared answers are explicitly marked',
        'development_case': 'q001 was already diagnosed; this is not held-out evidence'}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'query-lexemes.json', query_terms)
    source = output / 'source'
    source.mkdir()
    for path in (Path(__file__), Path(__file__).with_name('entity_groups.py'),
                 Path(__file__).with_name('selection_probe.py'), Path('tests/test_memderive_entity_groups.py')):
        shutil.copyfile(path, source / path.name)
    seeds = [memory['ref'] for memory in union]
    arms = {'v2_control': {'evidence': control_evidence, 'prompt': control_prompt}}
    selections = {}
    for threshold in thresholds:
        name = f'threshold_{threshold:g}'
        print(json.dumps({'preparing': name, 'eligible': len(rows), 'initial': len(seeds)}), flush=True)
        started = time.monotonic()
        engine = EntityGroups(rows, query_vector, content_lexemes, neighbor_threshold=threshold)
        index_seconds = time.monotonic() - started
        vector_refs = [engine.refs[int(i)] for i in np.argsort(-engine.similarities)[:len(retrieval['vector'])]]
        assert vector_refs == [hit['memory']['ref'] for hit in retrieval['vector']]
        assert len(rows) == retrieval['candidate_counts']['vector']
        assert engine.weights == control_offline['query_lexeme_weights']
        started = time.monotonic()
        selection = engine.select(seeds)
        selection_seconds = time.monotonic() - started
        evidence = await reconstruct_evidence(builder, [material(rows[ref]) for ref in selection['selected_refs']], events,
            retrieval['through_session'], retrieval['purpose'])
        assert len(evidence) == CONFIG['top_k']
        prompt = build_answer_prompt(question.text, evidence, world)
        arms[name] = {'evidence': evidence, 'prompt': prompt}
        selections[name] = {'threshold': threshold, **selection,
            'seconds': {'index_from_loaded_rows': index_seconds, 'selection': selection_seconds}}
        write_json(output / f'{name}.json', selections[name])
        print(json.dumps({'prepared': name, 'candidate_facts': len(selection['candidate_refs']),
            'groups': selection['group_count'], 'seconds': selection_seconds}), flush=True)
    offline = {'question': question.text, 'config': CONFIG,
        'eligible_facts': len(rows), 'initial_refs': seeds,
        'query_lexeme_weights': engine.weights, 'vector_top20_exact_match': True,
        'baseline_evidence_and_prompt_exact_match': True,
        'v2_control_evidence_and_prompt_exact_match': True,
        'v2_control': control_offline['entity_groups'], 'selections': selections,
        'arms': {name: {'retrieved': [asdict(e) for e in arm['evidence']], 'prompt': arm['prompt']}
                 for name, arm in arms.items()},
        'candidates': [material(rows[ref]) for ref in sorted(set().union(
            *(selection['candidate_refs'] for selection in selections.values())))]}
    write_json(output / 'offline.json', offline)
    print(json.dumps({'output': str(output), 'prepared_arms': list(arms),
        'distinct_prompts': len({arm['prompt'] for arm in arms.values()})}), flush=True)
    return question, old, arms, manifest


async def run(args):
    from memory_bench.benchmarks.derivemem.prompts import parse_answer
    from memory_bench.models import AnswerResult, GroundTruth
    from memory_bench.scorers.derivemem import DERIVEMEM_METRICS, DeriveMemMetricScorer

    question, old, arms, manifest = await prepare(args)
    if not args.execute:
        return
    output = args.output.resolve()
    route = json.loads((args.baseline / 'q1.json').read_text())['models']['llm']
    provider, = [p for p in json.loads(args.providers.read_text())['targets'] if p['label'] == route['provider']]
    assert route['model'] in provider['models']
    parameters = RequestParameters.model_validate({
        'output_limit_field': 'max_completion_tokens', 'reasoning_effort': route['reasoning_effort'],
        'response_format': None, 'temperature_supported': True,
        'seed': {'support': 'unknown', 'enabled': False},
        'disabled_unknown_seed_authorization': 'Same no-seed answer protocol as the archived baseline',
        'response_archive_directory': str(output / 'responses')})
    policy = RetryPolicy(max_attempts=1, timeout_seconds=180, backoff_seconds=(),
                         retry_status_codes=(), retry_transport_errors=())
    config = {'model': route['model'], 'base_url': provider['base_url'], 'temperature': route['temperature'],
              'request_parameters': parameters.model_dump(mode='json'), 'max_tokens': 2048,
              'retry': policy.model_dump(mode='json')}
    write_json(output / 'model-config.json', config)
    ledger = FileLedger(output / 'attempts.jsonl')
    client = OpenAIChatClient(provider['api_key'], base_url=provider['base_url'],
                             model=route['model'], temperature=route['temperature'], request_parameters=parameters)
    started = time.monotonic()
    write_json(output / 'status.json', {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat()})
    completed_prompts = {}
    try:
        for name, arm in arms.items():
            prompt = arm['prompt']
            reused_from = None
            if prompt in completed_prompts:
                reused_from, raw, audit = completed_prompts[prompt]
            else:
                call = RecordedCall(identity=CallIdentity(account_id='memderive-entity-group-probe',
                    execution_id=uuid4(), call_id=uuid4(), strategy='neighbor-threshold-sweep', stage=name,
                    operation=f'{name}:{question.id}', model_role='answer',
                    code_version=canonical_hash(manifest), config_sha256=canonical_hash(config)),
                    policy=policy, recorder=ledger, capture_raw_output=True)
                tick = time.monotonic()
                raw = await client.complete(prompt, max_tokens=2048, call=call)
                seconds = time.monotonic() - tick
                record = next(r for r in reversed(ledger.records)
                              if r.identity.call_id == call.identity.call_id and r.phase == 'finished')
                audit = {'call_id': str(call.identity.call_id), 'seconds': seconds,
                         'actual_model': record.actual_model, 'usage': record.usage.model_dump(mode='json')}
                completed_prompts[prompt] = (name, raw, audit)
            answer = AnswerResult(raw_response=raw, answer=parse_answer(raw))
            # Gold enters only after generation and cannot influence the selector.
            scored_question = replace(question, ground_truth=GroundTruth(**old['ground_truth']))
            scores = [asdict(await DeriveMemMetricScorer(metric).score(
                scored_question, answer, arms[name]['evidence'], {}, None)) for metric in DERIVEMEM_METRICS]
            result = {'question_id': question.id, 'arm': name, 'answer': asdict(answer), 'scores': scores,
                'retrieved': [asdict(e) for e in arm['evidence']], 'prompt': prompt,
                'reused_from': reused_from, **audit}
            append_json(output / 'results.jsonl', result)
            print(json.dumps({'arm': name, 'answer': answer.answer,
                'scores': {s['scorer']: s['value'] for s in scores},
                'seconds': audit['seconds'], 'reused_from': reused_from}), flush=True)
        write_json(output / 'status.json', {'status': 'complete', 'questions': 1,
            'arms': len(arms), 'answer_calls': len(completed_prompts),
            'finished_at': datetime.now(timezone.utc).isoformat(), 'seconds': time.monotonic() - started})
    except BaseException as exc:
        write_json(output / 'status.json', {'status': 'failed', 'error_type': type(exc).__name__,
            'finished_at': datetime.now(timezone.utc).isoformat(), 'seconds': time.monotonic() - started})
        raise
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--query-embedding', type=Path, required=True)
    parser.add_argument('--query-lexemes', type=Path, required=True)
    parser.add_argument('--control-v2', type=Path, required=True)
    parser.add_argument('--neighbor-thresholds', type=float, nargs='+', required=True)
    parser.add_argument('--providers', type=Path, default=Path('model_providers.local.json'))
    parser.add_argument('--execute', action='store_true')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
