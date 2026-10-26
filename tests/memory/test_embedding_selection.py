"""Embedding overrides must reach the frozen index without changing other models."""
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from integrations.memebench.memory_research.cli import argument_parser, freeze, resolve_index_config

BASE = Path(__file__).resolve().parents[2] / 'docs/memory-research/runtime-jev-config.json'

@pytest.mark.parametrize('dimensions', [1536, 1024, 768])
def test_aliyun_embedding_overrides_frozen_index_only(tmp_path, dimensions):
    config = json.loads(BASE.read_text())
    original = json.loads(BASE.read_text())
    args = argument_parser().parse_args([
        '--config', str(BASE), '--mode', 'check', '--output', str(tmp_path),
        '--memory-model', 'gpt-6-luna', '--p1-backend', 'jev',
        '--p2-direct-backend', 'jev', '--candidate-gate-backend', 'jev',
        '--embedding-provider', 'aliyun', '--embedding-model', 'qwen3.7-text-embedding',
        '--embedding-dimensions', str(dimensions),
    ])
    index = resolve_index_config(config, args)
    assert config == original
    assert index.dimensions == dimensions and index.embedding_model == 'qwen3.7-text-embedding'
    config['index'] = index.model_dump()
    manifest = freeze(tmp_path / 'frozen', config, args)
    assert manifest['configuration']['index'] == index.model_dump()
    models = manifest['model_parameters']
    assert models['embedding'] == {'provider': 'aliyun', 'model': 'qwen3.7-text-embedding', 'dimensions': dimensions}
    assert models['extractor']['model'] == models['p2']['model'] == 'gpt-6-luna'
    assert models['p1']['model'] == 'jev-1.13.0'
    assert models['answer']['model'] == 'gpt-4.1-mini'
    assert models['judge']['model'] == 'gpt-4o'


def test_embedding_defaults_use_config_and_reject_invalid_dimension():
    config = json.loads(BASE.read_text())
    parser = argument_parser()
    common = ['--config', str(BASE), '--mode', 'check', '--output', 'fixture']
    args = parser.parse_args(common)
    assert args.embedding_provider == 'openlux'
    assert resolve_index_config(config, args).model_dump() == config['index']
    for value in ['0', '-1']:
        with pytest.raises(ValidationError):
            resolve_index_config(config, parser.parse_args(common + ['--embedding-dimensions', value]))


def test_database_initializer_help_without_repository_pythonpath():
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    script = BASE.parents[2] / 'scripts/memory_research/init_db.py'
    result = subprocess.run(
        [sys.executable, str(script), '--help'], env=env,
        capture_output=True, text=True, check=True,
    )
    assert '--embedding-model' in result.stdout
    assert '--embedding-dimensions' in result.stdout
