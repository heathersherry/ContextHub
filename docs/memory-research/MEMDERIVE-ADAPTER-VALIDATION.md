# MemDerive adapter validation

Historical validation of the first per-event adapter. The subsequent session-based adapter and real-model Q1 run are recorded in [the 2026-10-27 experiment](MEMDERIVE-TRIPOPS-W1-Q1-20261027.md).

Validated on 2026-10-26 (Hong Kong, UTC+8) in the isolated
`codex/memderive-adapter` worktree, starting at
`7df67566737c2da107396470f0d6ad379146da0b`.

**This is an offline protocol validation, not a benchmark effectiveness result.**
No paid model requests were made. No full MEME run or ten-world MemDerive run
was performed, so no score is added to the MEME-32k experiment index.

The implementation changes only the new MemDerive integration, its tests and
its documentation. ContextHub core algorithms, prompts and MEME paths retain
the frozen baseline behavior. The integration uses the official full backend
protocol and pins `formal-set-v1.16` data and evaluator source content hashes.

## Results

| Check | Result |
| --- | --- |
| Existing memory/MEME/public API regression suite | 416 passed |
| New adapter/source/scoring/resource tests | 5 passed |
| Official Q1, Q2 and Q3 execution tests | 3 passed |
| Total distinct tests | 424 passed |
| Core or MEME implementation changes | None |
| Paid model requests | 0 |

The combined existing suite and first four adapter tests passed in 14.73 s.
The additional real-factory/resource test passed in 0.37 s. The three official
recipe tests passed in 86.51 s. These are validation runtimes, not model latency
or benchmark cost measurements.

All recipe tests used the public development world `tripops_w1`, its real
452 events, the real shared ContextHub engine and an isolated PostgreSQL
database. Extraction and answer models were explicit deterministic fixtures.
The recipe fixture emits no assertions, which makes the resulting answer
scores unsuitable for effectiveness comparisons.

| Official recipe | Events ingested | Retrievals | Scope |
| --- | ---: | ---: | --- |
| Q1 | 452 | 1 | One end question, official answer/citation scorer |
| Q2 | 452 | 123 | Official world subset of `shape-40-v3`, including Self replay |
| Q3 | 452 | 75 | 15 trajectories across all five checkpoint boundaries |

Q3 retrieval occurred after 118, 193, 286, 366 and 452 ingested events. This
confirms that the official executor continues ingestion after each read phase
and does not use the completed final state for earlier checkpoints.

Separate nonempty-memory tests cover direct updates, dependency repair,
derived sources spanning two events, official v9 event-ID rendering,
provenance-v2 scoring, stale citations and empty witnesses. They also check
read-only semantic state, isolated namespaces, duplicate/out-of-order rejection,
failed-ingestion poisoning, past-cutoff rejection and actual factory cleanup.
Gold/query metadata sentinels never enter model prompts.

## Artifacts and reproduction

- [Machine-readable validation summary](validation-memderive-adapter-20261026/summary.json)
- [Setup, supported protocol and run commands](../../integrations/memderive/README.md)
- [Pinned upstream identities](../../integrations/memderive/upstream-lock.json)
- [Adapter tests](../../tests/memory/test_memderive_adapter.py)
- [Official recipe tests](../../tests/memory/test_memderive_recipes.py)

Complete original official outputs, SQLite Stores, source mapping logs and
native call-ledger exports are preserved locally under
`outputs/memderive-validation-20261026/` in this worktree. This ignored directory
is not part of the Git commit; retain it when archiving the worktree if the raw
fixture artifacts are needed. The tracked summary is sufficient to identify
what was tested, and tests reproduce the workflow from the pinned public data.

The first implementation deliberately supports per-event ingestion and Q3
interleaved reads. Snapshot restoration, run resumption and arbitrary historical
rollback remain unsupported. Official API totals omit ContextHub's native calls;
no combined cost claim is made. See the integration README for these boundaries.

Before assessing improvement, freeze the actual model configuration and run a
real development baseline. Before merging independently optimized core prompts,
bring the selected prompt commit into this adapter branch and rerun both
benchmark regressions. Report MEME and MemDerive metrics separately.
