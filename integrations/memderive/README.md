# MemDerive adapter

This integration connects ContextHub's shared `MemoryEngine` to the official
[DerivEval evaluator](https://anonymous.4open.science/r/DerivEval-6799/) and
[DeriveMem data](https://anonymous.4open.science/r/DeriveMem-67C6/) for
[MemDerive](https://arxiv.org/html/2610.25373v1). The pinned release is
`formal-set-v1.16`. The ContextHub starting commit is `7df675667`.

The adapter owns event conversion, source mapping and run isolation. The
upstream evaluator owns question selection, answer prompts, answer parsing,
provenance-v2 scoring, diagnostic replay and checkpoint scheduling. No gold
answers, typed actions, diagnostic graphs or query metadata are passed to the
memory engine. The shared extraction contract now includes source message
numbers; its maintenance and retrieval decision policies remain unchanged.

## Supported protocol

| Recipe | Implementation | Initial scope |
| --- | --- | --- |
| Q1 answer and citation quality | Official v9 answer prompt and eight deterministic metrics | Development worlds; formal worlds can be selected explicitly |
| Q2 diagnostics | Official `shape-40-v3`, targeted/inherited probes and bottom-up Self replay | No Gold intervention |
| Q3 continuous maintenance | Official `interleaved`, p20/p40/p60/p80/p100 | Same namespace, ordered writes, read-only memory queries |

The ten formal worlds contain 4,692 events and 1,503 end questions. The full Q3
panel has 132 trajectories and 660 checkpoint question cells. These are full
release counts, not the size of the default development subset.

**Ingestion is explicitly `per_session`.** Each complete official session becomes
one ContextHub session. Each public event becomes a numbered message retaining
its event ID/time/source header. Shared extraction outputs `source_message_nos`
for every item; the parser requires nonempty, unique, in-range message numbers.
Direct writes save those coordinates; duplicate no-ops retain the existing
sources, and derived memories follow their versioned premises. The public API and SDK expose
`source_messages` as `{session_seq, message_no}` pairs alongside session sources.
The adapter maps those coordinates back to official event IDs. These are
model-attributed sources, not a semantic proof that every cited event entails
the memory. Unrelated events in the same session are not added automatically.

This shared provenance extension requires a fresh database with the current
schema and the current extraction output contract. It does not migrate existing
MEME/application databases. The earlier `0aa7aaac5` validation record describes
the superseded per-event condition; it is not a session-mode effectiveness run.

Derived memories use the shared engine's recursive `basis_refs` provenance.
Missing sources stay empty; the adapter never fills them from gold, all events
in a session, or strings guessed from generated memory text. Returned metadata
contains the original event IDs and ContextHub versioned refs. The source label
renders `event_ids=[...]`, so the unmodified official v9 answer renderer emits
the same lines as its event-attributed renderer. Unit tests check this equality
and use the official scorer, including empty-witness semantics.

Retrieval uses the shared `auto` selection and takes its first `top_k` items.
It adds no benchmark-specific policy retrieval or reasoning. Queries can append
embedding-call audit records, but do not modify memories, sources, dependencies
or ingest state. Q2 answers and reconstructed answers are never written back.

The adapter implements the full official `MemoryBackend` protocol. The current
upstream `SimpleMemoryBackend` wrapper cannot ingest again after becoming ready,
which is incompatible with interleaved maintenance. No upstream patch is needed.

**No snapshot/restore, resume, historical rollback, or idempotent ingestion is
claimed.** The official custom-backend resolver disables automatic build reuse;
the generated configs also disable reuse. A failure requires a fresh run.
Retrieval rejects a query cutoff earlier than the latest ingested event. Q3
answers each checkpoint before ingesting the next delta. A fresh run uses new
physical namespaces and preserves any previous database evidence.

## Prepare a run

Download and extract the two official source archives linked above. Keep them
outside this repository. `upstream-lock.json` records content hashes rather
than ZIP hashes, since archive packaging can change without source changes.
Preparation rejects a different checkout; review a new release before updating
the lock. The evaluator has its own code license and the synthetic dataset is
CC BY 4.0; retain their notices. Neither archive is vendored here.

In this worktree, use a separate virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]' -e ./sdk pyyaml 'openai>=1.90,<3'
export DERIVEVAL_ROOT=/absolute/path/to/DerivEval
export DERIVEMEM_ROOT=/absolute/path/to/DeriveMem
export PYTHONPATH="$PWD/src:$PWD/sdk/src:$PWD:$DERIVEVAL_ROOT/src"
```

Use a **dedicated PostgreSQL database**, separate from MEME and application
experiments. The explicit MemDerive environment names prevent fallback to an
existing application database or provider settings:

```bash
createdb -h 127.0.0.1 contexthub_memderive
export MEMDERIVE_DATABASE_URL=postgresql://127.0.0.1/contexthub_memderive
export MEMDERIVE_CHAT_API_KEY=...       # ContextHub extraction/maintenance
export MEMDERIVE_EMBEDDING_API_KEY=... # ContextHub retrieval/indexing
# export MEMDERIVE_JEV_API_KEY=...     # only when the selected core config uses Jev
export LLM_API_KEY=...                # official answer model
export LLM_BASE_URL=...               # official answer endpoint
```

Copy `examples/memory-config.json` to a local file under `outputs/`, then set
the actual construction models, endpoints, index dimensions, retry policy and
Jev choices. The example is an application template, **not a claim to reproduce
the latest MEME model configuration**. The same core configuration must be
recorded explicitly for a controlled comparison. `initialize: true` initializes
the memory schema in the dedicated database. Changing embedding/index settings
requires a separate compatible database.

```bash
.venv/bin/python -m integrations.memderive.prepare \
  --deriveval-root "$DERIVEVAL_ROOT" \
  --derivemem-root "$DERIVEMEM_ROOT" \
  --memory-config outputs/memory-config.local.json \
  --output outputs/memderive-dev-v1
```

This writes `q1.json`, `q2.json`, `q3.json` and `provenance.json`, without model
calls. The default worlds are `tripops_w1`, `shopops_w1`, `eduops_w1`; the other
seven formal worlds remain held out. `--worlds` explicitly selects a different
set. An optional `--answer-route FILE.json` supplies an official DerivEval model
route object. Otherwise, the answer model and settings come from the pinned
paper preset. ContextHub construction models come only from the core config.

The configuration freezes a hash of actual ContextHub implementation files and
prompts, including uncommitted edits. Initialization rejects changed source.
Prepare a new output directory after changing code or prompts. Do not edit this
worktree during a run. A run on another branch/worktree is independent as long
as its database, namespaces and output directories are also separate. API
credentials may still share provider rate limits and billing.

## Run official evaluation

First check the selected recipe without any model calls:

```bash
.venv/bin/python -m deriveval.cli plan outputs/memderive-dev-v1/q1.json --no-persist --json
```

Then explicitly launch it; this makes real construction and answer-model calls:

```bash
.venv/bin/python -m deriveval.cli run outputs/memderive-dev-v1/q1.json \
  --store outputs/memderive-dev-v1/q1.sqlite --foreground --json
```

Use `q2.json` or `q3.json` and a corresponding Store path for the other recipes.
Each recipe performs its own fresh construction. Three development worlds still
contain many events: plan and review the configuration before a paid run. Do
not treat `--offline-fixture` alone as an offline ContextHub run: it replaces
the answerer, while this backend still uses real construction clients. The
regression tests below explicitly replace those clients as well.

Keep the official run manifests and per-question artifacts. The adapter also
writes namespace/event mapping JSONL and a `*.attempts.jsonl` export of the
shared ContextHub execution ledger on close. On process termination the
underlying database remains the authoritative ledger. The export includes
retries, failures and query embeddings.

**DerivEval API-window totals exclude ContextHub's native model clients.** They
are not a complete token, latency or dollar total. The generated Q3 config
therefore omits the optional `cost_efficiency` recipe. No combined cost metric
is claimed until the two ledgers have been explicitly reconciled with verified
prices. The adapter metadata and preparation manifest record this limitation.
ContextHub's own retry/concurrency policy is independent of DerivEval's API
windows; generated recipes serialize QA.

Report each official metric and denominator separately: answer accuracy,
witnessed accuracy, answerability accuracy, witness precision/recall/F1, stale
belief rate and distractor hit rate. Keep Q2 replay outcomes and Q3 maintenance
metrics separate. Do not average these scores with MEME.

## Verify without paid model calls

Create a new validation database and use the deterministic configuration:

```bash
createdb -h 127.0.0.1 contexthub_memderive_test
export MEMORY_DATABASE_URL=postgresql://127.0.0.1/contexthub_memderive_test
.venv/bin/python -m scripts.memory_research.init_db \
  --config docs/memory-research/validation-config.json
.venv/bin/python -m pytest tests/memory/test_memderive_adapter.py \
  tests/memory/test_memderive_recipes.py -q
```

The adapter tests exercise actual shared-engine writes, update/repair,
derived-event provenance, read-only queries, namespace isolation, failures and
official answer/citation scoring. Recipe tests run the official supervisor on
all public events in `tripops_w1`: Q1 has one end question, Q2 uses its official
diagnostic panel, and Q3 executes all five checkpoints. Model responses are
explicit fixtures; these tests cannot establish benchmark performance. Tests
skip when the optional upstream package/data or dedicated database is absent.

See [the initial validation record](../../docs/memory-research/MEMDERIVE-ADAPTER-VALIDATION.md).

## Saved-candidate selection experiment

The [2026-10-30 paired probe](../../docs/memory-research/MEMDERIVE-SELECTION-PROBE-20261030.md)
replays saved retrieval candidates for 20 fixed questions and compares the
original top 15 with a single DeepSeek selection from the candidate union.
It uses the official answer prompt, parser and scorers, preserves source
attribution, and performs no ingestion or embedding calls. This is a separate
research script, not a backend resume feature or an enabled engine policy.

The subsequent [q001 entity-group probe](../../docs/memory-research/MEMDERIVE-ENTITY-GROUP-Q001-20261030.md)
uses only existing vectors and entity tags for expansion and group selection,
with no selector model. The hotel answer passes official scoring, but the full
conference-to-trip chain is not retained in the 15 items. It remains a separate
experiment and does not enable a new backend retrieval policy.

The [v2 follow-up](../../docs/memory-research/MEMDERIVE-ENTITY-GROUP-Q001-V2-20261030.md)
ranks new facts by direct query relevance and contribution within connected
evidence groups. The complete conference-to-trip-to-hotel chain reaches the
first three items on q001. Official scores remain the same as v1; the canceled
booking is still absent. This remains a development probe, not a default policy.

The [2026-11-02 neighbor-threshold sweep](../../docs/memory-research/MEMDERIVE-NEIGHBOR-THRESHOLD-Q001-20261102.md)
replaces only neighbor admission with raw query cosine >= an explicit threshold,
preserving v2 downstream ranking and the 15-item budget. Thresholds 0.40 and 0.45
retain the explicit chain and correct answer; 0.50 fails. At 0.35 the canceled
booking enters the pool but still loses in final selection. The research CLI now
requires `--control-v2` and `--neighbor-thresholds`; see the report for frozen
inputs and the verified official runtime. No universal threshold or shared
retrieval default is selected from this one-question probe.
