# MemDerive tripops_w1 Q1 checkpoint — 2026-11-03

## Scope and frozen versions

This checkpoint preserves the current `codex/memory-research` working copy,
including the expanded rule entry, revised extraction/maintenance prompts,
ordered item processing, and ordinary-answer rule eligibility fix. Existing
research scripts are retained as separate experiments, not enabled retrieval
policies. No new optimization or paid experiment was performed for this commit.

The full build processed all 77 sessions and 452 public events from the locked
`formal-set-v1.16` development world. It produced 475 memory versions (453 facts,
22 rules), with complete vectors, statuses and source mappings. The subsequent
replay restored this complete snapshot into an independent database and ran all
140 Q1 questions with at most 15 memories each. It made no ingestion calls and
left all memory/source/session/topic/config rows unchanged.

| Artifact | SHA-256 |
| --- | --- |
| Full-build implementation | `00ccf2dbe84a51d0a7970f083e4370b1a39b8e19eebd30edb4b08eb802c22d51` |
| Rule-eligible replay implementation | `c300a6af580731e22bb30e97dbb53bedf0476811965c04e839922773c63a4e16` |
| Complete database snapshot | `5b473b27c764e06b9b4e51b5b4d69c46d48b516bbcfd90ebeb7eefe285b44a98` |

These hashes identify actual frozen working-copy contents, not HEAD alone.
All extraction prompts are byte-identical between the build and replay.

## Result

| Official metric | Full-build Q1 | Rule-eligible replay |
| --- | ---: | ---: |
| Answer accuracy | 54/140 (38.57%) | 58/140 (41.43%) |
| Witnessed accuracy | 35/140 (25.00%) | 41/140 (29.29%) |
| Rules-family correct | 3/28 | 6/28 |
| Stale-evidence hits | 0/140 | 6/140 |
| Distractor hits | 13/140 | 2/140 |

Seven answers improved and three regressed. Rules reached 68 questions, with
137 rule items across the 2,100 final context items. The old answer entry filtered
out rules; the fixed shared entry admits both facts and rules within the same
budget. No ranking algorithm, graph expansion or model selector was added.

The requested extraction coverage improved, including background facts, the
traveler-department approval rule, reimbursement requirements, and e127's
itemized-invoice requirement alongside the draft withdrawal. Remaining issues
include top-15 evidence gaps, old/new policies both remaining current, an e258
amount/duplicate error and loss of some source attribution on repaired PS-1.
Five stale-evidence hits reason from old meal caps; q087 selects the current
threshold but cites the obsolete one too. These are not six wrong numeric answers.

The answer path uses text/vector RRF (20 candidates per channel, c=60, hybrid
limit 20), followed by entity supplements and a final first-15 slice. All 140
queries had 20 hybrid candidates, so entity-only supplements never reached the
final 15. Answer queries supplied no topic labels and performed no dependency
traversal. These observations are not additional changes in this checkpoint.

## Models, usage and interpretation

Memory construction used DeepSeek `deepseek-flash`, TypeSafe `jev-1.13.0` and
Aliyun `qwen3.7-text-embedding` (1536 dimensions). Answers used OpenLux
`gpt-5.6-sol`, temperature 0, requested reasoning none, and the pinned official
v9 answer prompt/parser and provenance-v2 local scorers. Gold and official
support labels were confined to local scoring and post-run audits.

Construction took 75.19 minutes and used 2,638,229 known non-embedding tokens;
one failed request lacks usage. The replay took 13.43 minutes: 140 query
embedding calls (7,155 input tokens) and 140 answer calls, with no retries.
Answer usage reported 3,003,319 input and 30,262 output tokens, including
2,724,858 cached input tokens. Reported reasoning tokens remain part of output.

Provider usage needs reconciliation: similarly sized visible prompts (mean
4,603 characters) split between roughly 1.3K and 23.7K reported input tokens.
Do not interpret the token ratio as equivalent algorithmic context growth or
billing cost. Public-price estimates and their account-tier/cache assumptions
are stored separately; they do not replace an actual account bill.

This single-world result is diagnostic, not an overall benchmark score or a
same-workload comparison with the paper's ten-world results.

## Local artifacts and validation

Raw artifacts and databases remain local; this commit includes source, tests
and reports, not provider credentials, database dumps or historical raw logs.
Paths below are repository-relative and resolve under
`/Users/sherrylin/Documents/PythonProjects/ContextHub` on the experiment machine.

- Full build: `outputs/memderive-tripops-w1-q1-20261103-r1/report.txt`.
- Complete reusable snapshot: `outputs/memderive-tripops-w1-q1-20261103-r1/snapshot/`.
- Replay report: `outputs/memderive-tripops-w1-q1-rules-20261103-r1/report.txt`.
- Per-question comparison: replay `analysis/question-comparison.json`.
- Hashes and row integrity: replay `freeze-manifest.json`, `memory-integrity.json`,
  `delivery-sha256.json`.
- Usage audit: replay `analysis/paper-comparison-usage-audit.json`.
- Price estimate: replay `analysis/public-price-estimate.json`.
- Commit-time verification: replay `checkpoint-tests.log`.

The replay's targeted suite passed 52 tests. A previous broad run passed 434,
skipped two and failed two MEME judge-fixture checks: the required pinned judge
file was unavailable. Commit-time verification explicitly deselects those two
fixture-dependent tests; it does not bypass or modify judge integrity checks.

Commit-time result: **466 passed, 3 skipped, 2 deselected in 13.99s**, using
the dedicated research test database and pinned DerivEval imports. Models were
simulated; no paid benchmark or model requests ran during verification.
