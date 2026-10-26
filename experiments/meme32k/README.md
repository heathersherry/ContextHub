# Reproduce ContextHub on MEME-32k (100 cases)

Run these commands from the repository root. This guide uses only tracked code,
your own API credentials, a fresh PostgreSQL database, and the public benchmark.
No previous `outputs/` directory or experiment database is required.

## Choose the version

- `codex/memory-research`: current implementation, including joint extraction and
  explicit forgetting. It has **not** completed a new full 100-case live evaluation.
- `meme32k-baseline-20261021`: original full-flow implementation used for 97 of the
  selected historical cases.
- `meme32k-baseline-retry-20261022`: the same implementation with the Jev
  choice/probability mismatch retry fix, used for `pl_050`, `sw_004`, `sw_025`.

The historical aggregate was **436/694**, using the case selection in
[historical-cases.json](historical-cases.json). It combined these two versions;
there was no single uniform run. To repeat that protocol, use separate checkouts,
databases and output directories for the two tags. Each historical tag defaults
to its assigned group (97 or 3 cases); the current branch defaults to all 100.
The batch entry point also supports `--case` for a smoke test. Do not add the
same case from both versions to the aggregate. The tagged implementations retain
the old extra answer guidance; the current branch uses the official answer template.
Later 100-case answer-only replays are different experiments, not this baseline.

These tags pin source and prompts, not hosted models. API aliases and generated
answers/judgments can vary; an identical score is not promised. The historical
snapshots and their SHA-256 manifests are documented in `baseline-source.json`
on each tag. The two historical core snapshots are byte-identical to the saved
execution sources, excluding the additional reproduction tooling.

## 1. Clone and install

```bash
git clone --branch codex/memory-research https://github.com/heathersherry/ContextHub.git
cd ContextHub
# Optional, before installation:
# git switch --detach meme32k-baseline-retry-20261022
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]' -e sdk/ huggingface_hub
```

Python 3.12+ and PostgreSQL 16 with pgvector are required. The project README
contains macOS and Ubuntu PostgreSQL installation instructions. Dependencies are
specified in `pyproject.toml`; this is a source/protocol pin, not a complete OS or
transitive Python dependency lock.

## 2. Obtain the exact data and official scorer

```bash
mkdir -p outputs/meme-inputs
python - <<'PY'
from huggingface_hub import hf_hub_download
hf_hub_download('meme-benchmark/MEME', 'meme_filler32k.json',
                repo_type='dataset', local_dir='outputs/meme-inputs')
PY
git clone https://github.com/SeokwonJung-Jay/MEME-public.git outputs/MEME-public
git -C outputs/MEME-public checkout 0271ad85389a963cbc4892a36391f868ba4d18d1
```

The wrapper checks the dataset SHA-256
`a88d28374a002b3e5b1683fb7201d06a1ce739d2ebf94c971c37bb65cf6ebdd3`
and official `judge.py` SHA-256
`4d19e4a8acc4f3143ace3b5933aeeb92a91e019d57cd0bba42c3bcba7c91a218`.
A newer file with a different hash is rejected. Obtain the matching original
release if the upstream dataset changes; do not disable the check. Dataset
attribution/license remains with [MEME](https://huggingface.co/datasets/meme-benchmark/MEME).
The scorer's Python CLI dependencies are not needed: our adapter loads only the
pinned official scoring definitions.

## 3. Configure your own accounts

```bash
cp experiments/meme32k/providers.example.json model_providers.local.json
# Edit api_key values in model_providers.local.json.
export TYPESAFE_API_KEY='your-typesafe-key'
```

`model_providers.local.json` is ignored by Git. Never commit it. Use the exact
provider routing below for comparison with the historical experiment:

| Role | Provider | Model / settings |
| --- | --- | --- |
| Typed decisions | TypeSafe | `jev-1.13.0`, P1 threshold >= 0.5 |
| Extraction and maintenance | DeepSeek official | `deepseek-flash`, reasoning effort `low` |
| Embeddings | Aliyun international | `qwen3.7-text-embedding`, 1536 dimensions |
| Answers | OpenLux | `gpt-4.1-mini`, temperature 0, max tokens 500 |
| Model judgments | OpenLux | `gpt-4o`, temperature 0 |

All three Jev backends are enabled. Topic similarity uses strict > 0.8.
The historical DeepSeek request used a rolling alias; no immutable model snapshot
was returned. Current availability of these provider/model combinations must be
checked with your accounts. The evaluator currently requires the OpenLux endpoint;
an OpenAI account alone is insufficient. Price fields are empty deliberately:
unknown costs stay unknown; tokens are still recorded. ER uses deterministic
matching rather than a model judgment.

## 4. Prepare a fresh database

Create an empty database with a PostgreSQL account allowed to install pgvector:

```bash
createdb contexthub_memory_meme32k
export MEMORY_DATABASE_URL='postgresql://localhost/contexthub_memory_meme32k'
python -m scripts.memory_research.init_db --config experiments/meme32k/config.json
```

Adjust the DSN for your PostgreSQL user/password. Do not reuse an application or
previous experiment database. The initializer installs the memory schema and
checks embedding configuration. Use the same DSN when running the batch.

## 5. Validate inputs, smoke-test, and run all 100

Offline preflight makes **no API calls and no database connection**, and does not
require keys. It verifies data, scorer, case counts, clean tracked source and CLI
arguments:

```bash
python -m integrations.memebench.memory_research.batch \
  --data outputs/meme-inputs/meme_filler32k.json \
  --official-judge outputs/MEME-public/code/eval/judge.py \
  --output outputs/meme32k-full --preflight
```

A single-case smoke run **does call paid APIs**:

```bash
python -m integrations.memebench.memory_research.batch \
  --data outputs/meme-inputs/meme_filler32k.json \
  --official-judge outputs/MEME-public/code/eval/judge.py \
  --output outputs/meme32k-smoke --case pl_001
```

Then run the selected version serially (all 100 on the current branch; 97 or 3
on the historical tags). This can take many hours and incurs API costs:

```bash
python -m integrations.memebench.memory_research.batch \
  --data outputs/meme-inputs/meme_filler32k.json \
  --official-judge outputs/MEME-public/code/eval/judge.py \
  --output outputs/meme32k-full
```

The smoke output is separate and is not counted in the full run. The wrapper uses
the same tracked single-case engine as `cli --mode formal`, but launches one
process/run per case so completed cases can be retained after an interruption.
It does not patch model code or use the historical experiment scripts.

## 6. Resume and inspect results

Repeat the full-run command with `--resume` to skip successful cases and start
cases not yet attempted. Failed or interrupted attempts are preserved and skipped.
Add `--resume --retry-failed` to explicitly rerun those cases **from session 1**,
with fresh run IDs. This repeats paid calls; there is no mid-session restoration
or judge-only recovery in this wrapper. Successful cases are never rerun by it.
Use one process per output directory. Resuming requires the same source commit,
config, dataset, scorer, selected cases and provider endpoints. A code change
requires a new output directory.

- `summary.json`: selected successful runs, six task counts, total passed and
  missing cases. It is a completed-case subtotal until `complete=true`.
- `<case>.log`: subprocess output and failures.
- `<case>/memory-check-*/`: exact source/prompt snapshots, manifest, episode
  answers, scored answers, call records, memory exports and per-case summary.
- `batch.json`: source commit and non-secret protocol identifiers.

For a valid full result, require 100 completed cases, zero missing cases and
`total_scored=694`. Cas/Abs/Del require paired before-and-after success; ER/Agg/Tr
use after-test success. Report failures separately, never call a partial subtotal
the 100-case score. Retry selection is operational recovery, not repeated sampling
for a better score. Keep all previous attempts.

Raw outputs remain local/ignored. To publish evidence, review and archive results
separately; source tags do not distribute historical model responses or databases.

## Offline regression checks

To combine the two completed historical groups, save their `summary.json` files
as `outputs/baseline97.json` and `outputs/baseline3.json` in one checkout, then:

```bash
python - <<'PY_SUM'
import json
from pathlib import Path
mapping = json.loads(Path('experiments/meme32k/historical-cases.json').read_text())['cases']
parts = [json.loads(Path(p).read_text()) for p in ('outputs/baseline97.json', 'outputs/baseline3.json')]
seen = set()
tasks = {}
for tag, part in zip(('meme32k-baseline-20261021', 'meme32k-baseline-retry-20261022'), parts):
    expected = {r['case'] for r in mapping if r['source_tag'] == tag}
    assert part['complete'] and set(part['selected_runs']) == expected
    assert not seen.intersection(expected)
    seen.update(expected)
    for task, row in part['tasks'].items():
        dest = tasks.setdefault(task, {'passed': 0, 'total': 0})
        for key in dest:
            dest[key] += row[key]
assert len(seen) == 100 and sum(t['total'] for t in tasks.values()) == 694
print(json.dumps({'tasks': tasks, 'passed': sum(t['passed'] for t in tasks.values()), 'total': 694}, indent=2))
PY_SUM
```

Use another dedicated database with the test-only 3-dimensional configuration:

```bash
createdb contexthub_memory_meme_tests
export MEMORY_DATABASE_URL='postgresql://localhost/contexthub_memory_meme_tests'
python -m scripts.memory_research.init_db --config docs/memory-research/validation-config.json
export MEME_OFFICIAL_JUDGE="$PWD/outputs/MEME-public/code/eval/judge.py"
python -m pytest tests/memory -q
```

Model responses in these tests are fixtures. Passing them verifies implementation
behavior, not live benchmark accuracy. See [VALIDATION.txt](VALIDATION.txt) for
what was actually verified when publishing this reproduction entry point.
