"""Prepare three official DerivEval recipes without running model calls."""

import argparse
import copy
import json
from pathlib import Path
import subprocess

import yaml

from contexthub.memory.factory import MemoryConfig
from .backend import ROOT, implementation_hash, tree_hash

DEVELOPMENT_WORLDS = ['tripops_w1', 'shopops_w1', 'eduops_w1']
FORMAL_WORLDS = [*(f'tripops_w{i}' for i in range(1, 5)),
                 *(f'shopops_w{i}' for i in range(1, 4)),
                 *(f'eduops_w{i}' for i in range(1, 4))]
RECIPES = {'q1': 'q1-memory-systems.yaml', 'q2': 'q2-diagnostics.yaml',
           'q3': 'q3-maintenance.yaml'}


def prepare(*, deriveval_root, derivemem_root, memory_config, output,
            worlds=DEVELOPMENT_WORLDS, answer_route=None):
    deriveval_root, derivemem_root, output = map(
        lambda p: Path(p).resolve(), (deriveval_root, derivemem_root, output))
    lock = json.loads(Path(__file__).with_name('upstream-lock.json').read_text())
    actual = {'deriveval_src': tree_hash(deriveval_root / 'src'),
              'deriveval_presets': tree_hash(deriveval_root / 'presets/paper'),
              'derivemem_benchmark': tree_hash(derivemem_root / 'benchmark')}
    if actual != lock['sha256']:
        raise ValueError('upstream checkout differs from upstream-lock.json; review the new release explicitly')
    if not worlds or len(set(worlds)) != len(worlds) or set(worlds) - set(FORMAL_WORLDS):
        raise ValueError('select unique formal worlds; dialogue variants are outside this protocol')
    config = MemoryConfig.model_validate(memory_config).model_dump(mode='json')
    code_hash = implementation_hash()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    config['code_version'] = f'{commit}:{code_hash}'
    output.mkdir(parents=True, exist_ok=False)
    for recipe, filename in RECIPES.items():
        spec = yaml.safe_load((deriveval_root / 'presets/paper' / filename).read_text())
        spec['name'] = f'contexthub-memderive-{recipe}'
        spec['benchmark']['data'] = str(derivemem_root / 'benchmark/data')
        spec['benchmark']['subset'] = {'worlds': list(worlds)}
        spec['backends'] = [{
            'id': 'contexthub-session', 'implementation': 'integrations.memderive.backend:ContextHubBackend',
            'version': '2.0.0', 'granularity': 'per_session',
            'api_observability': 'external_context_hub_ledger',
            'parameters': {'memory_config': config, 'source_sha256': code_hash,
                           'state_root': str(output / recipe / 'state')},
            'capabilities': {'ingest': True, 'retrieve': True, 'top_k': True,
                             'read_only_retrieve': True, 'source_event_provenance': True,
                             'checkpoint': False, 'restore': False},
        }]
        # ContextHub owns its construction models. Avoid declaring unused
        # official memory model routes that would mislabel this condition.
        spec['models'] = {'llm': copy.deepcopy(answer_route or spec['models']['llm'])}
        spec['reuse'] = {'enabled': False, 'on_miss': 'execute'}
        spec['execution'] = {'api_window_default': 1, 'qa_workers': 1,
                             'timeout_seconds': 180, 'max_attempts': 1, 'retries': 0}
        spec['output'] = {'root': str(output / recipe / 'runs'), 'publish': 'never'}
        if recipe == 'q2':
            spec['experiments'][0]['parameters']['panel_path'] = str(
                derivemem_root / 'benchmark/microbench/generated/formal-set-v1.16/shape-40-v3')
        if recipe == 'q3':
            # Official API totals omit ContextHub's native clients. Do not
            # publish a misleading total-cost experiment for this adapter.
            spec['experiments'] = [spec['experiments'][0]]
            spec['experiments'][0]['lineage_id'] = 'contexthub-session-five-checkpoints-v1'
        (output / f'{recipe}.json').write_text(json.dumps(spec, indent=2) + '\n')
    (output / 'provenance.json').write_text(json.dumps({
        'baseline_commit': '7df67566737c2da107396470f0d6ad379146da0b',
        'implementation_commit': commit, 'source_sha256': code_hash,
        'upstream': lock, 'worlds': worlds, 'ingest_granularity': 'per_session',
        'usage_accounting': 'DerivEval answers plus ContextHub *.attempts.jsonl; no combined cost claim',
    }, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deriveval-root', type=Path, required=True)
    parser.add_argument('--derivemem-root', type=Path, required=True)
    parser.add_argument('--memory-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worlds', nargs='+', choices=FORMAL_WORLDS, default=DEVELOPMENT_WORLDS)
    parser.add_argument('--answer-route', type=Path, help='JSON model route; defaults to the official answer model')
    args = parser.parse_args()
    print(prepare(deriveval_root=args.deriveval_root, derivemem_root=args.derivemem_root,
                  memory_config=json.loads(args.memory_config.read_text()), output=args.output,
                  worlds=args.worlds,
                  answer_route=json.loads(args.answer_route.read_text()) if args.answer_route else None))


if __name__ == '__main__':
    main()
