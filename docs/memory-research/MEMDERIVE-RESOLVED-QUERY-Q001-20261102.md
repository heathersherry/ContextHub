# MemDerive q001: one resolved-query continuation with a fixed prefix

## Outcome

The canceled hotel booking still does not enter the final 15. Its query cosine
increases from **0.389032 to 0.552459**, and its cosine rank among the same 141
candidates improves from **100 to 18**. Both final lists have exactly 15 unique
items and the first three items are identical. All 12 later positions change.

The new tail is dominated by Anser-related facts, including registration, coffee
receipts, and administrative status. Entity conditioning changes retrieval focus
but does not sufficiently distinguish hotel-booking evidence on this question.
This is an offline ranking result, not a new answer or official-score result.

Execution: **2026-11-02 11:12:46–11:13:03, Hong Kong time (UTC+8)**.
Total run time: 17.256 seconds.

## Controlled intervention

Original query: `As of 2026-10-07 09:00, which hotel booking is currently active for the February conference trip?`

Resolved query: `As of 2026-10-07 09:00, which hotel booking is currently active for Trip-Anser, the February conference trip?`

The query was specified before the call and was not revised after inspecting the
result. Trip-Anser is a manually specified diagnostic binding supported by the
previously selected conference-date fact and the explicit conference-to-trip
fact. Neither a hotel name nor a cancellation event is inserted into the query.
This experiment does not test automatic entity resolution.

- Freeze the first three items and their original selection trace.
- Reuse 324 eligible fact vectors, the same 36 initial candidates, neighbor
  threshold 0.35, the same 141 candidate facts, 3,300 groups, and 70 bridge paths.
- Compute groups and bridge strengths using the original query before changing
  anything. Reuse them for both arms without recalculation.
- Replace only the individual query cosine read by the direct-relevance term
  after the frozen prefix. Keep original lexical coverage, score weights,
  bridge logic, greedy iteration, tie breaks, and exact-reference deduplication.
- Keep the final budget at 15. No ingestion, memory re-embedding, selector LLM,
  answer model, or official scorer is run.

The actual incremental score remains:

```text
score = (0.5 * coverage_gain + 0.5 * sum(query_cosine for new facts)
         + bridge_gain) / number_of_new_facts
```

## Cancellation result

Fact: “The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge.”

| Measure | Original query | Resolved query |
|---|---:|---:|
| Query cosine | 0.389032 | 0.552459 |
| Cosine rank among all 141 candidates | 100 | 18 |
| Final evidence rank | Not selected | Not selected |
| Last-round incremental score | 0.194516 | 0.276229 |
| Last-round rank among distinct legal single-fact additions | 57 | 4 |

The last-round ranks use each arm’s own 14 already selected facts; they are not
a comparison in an identical selection state. In the resolved arm, the last
slot goes to “The refund ledger for Trip-Anser is fully synced.” at 0.294455.
Its cosine is 0.588911. The cancellation has zero added coverage and bridge
gain and therefore scores 0.5 × 0.552459 = 0.276229.

The old final item, weekly digest #26 with seasonal booking tips, drops from
cosine 0.498468 to 0.468672, ranks 49 by resolved cosine, and leaves the final 15.

## Before and after: full final evidence lists

Scores here are actual incremental selection scores, not raw query cosines.
The original prefix scores are frozen. Items 2 and 3 share one two-addition
group score; they were not independently assigned that score.

| Position | Original evidence | Score | Resolved-query evidence | Score |
|---:|---|---:|---|---:|
| 1 | Conference Anser now runs from 2026-02-02 through 2026-02-06. | 0.546449 | Conference Anser now runs from 2026-02-02 through 2026-02-06. | 0.546449 |
| 2 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. | 0.368799 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. | 0.368799 |
| 3 | Trip-Anser was opened for Conference Anser. | 0.368799 | Trip-Anser was opened for Conference Anser. | 0.368799 |
| 4 | Desk booking for February opens on Friday. | 0.307123 | Registration for Conference Anser is confirmed. | 0.364954 |
| 5 | Conference Falco is on the calendar for 2026-10-26 through 2026-10-30. | 0.285191 | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. | 0.343403 |
| 6 | On 2026-03-02, Trip-Vireo was classified as a conference trip. | 0.279441 | Trip-Anser is wrapped up and marked completed. | 0.337333 |
| 7 | Registration for Conference Falco is confirmed. | 0.268392 | There is no receipt for the espresso at the Anser venue for Trip-Anser. | 0.325403 |
| 8 | Conference Tanit now runs 2026-05-04 through 2026-05-08. | 0.263141 | The Conference Anser registration fee is 360 CR. | 0.321957 |
| 9 | An old thread still shows Conference Lyra starting on February 8. | 0.263081 | A reminder about Conference Anser logistics checklists went out. | 0.315383 |
| 10 | At 2026-04-30 09:00, registration for Conference Corax was confirmed. | 0.261372 | A receipt for the Conference Anser registration is on file and remains valid. | 0.313845 |
| 11 | Tam griped about hotel prices in conference season. | 0.255062 | Vera said the Conference Anser sessions were worth the travel. | 0.312018 |
| 12 | On 2026-03-02, Travel desk weekly digest #8 went out with seasonal booking tips. | 0.253236 | Conference Anser takes place in Velle. | 0.306713 |
| 13 | Trip-Falco was opened for Conference Falco. | 0.253028 | The pre-submission tracker for Trip-Anser is fully synced. | 0.305875 |
| 14 | Trip-Falco is classified as a conference trip. | 0.250738 | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. | 0.302342 |
| 15 | Travel desk weekly digest #26 went out with seasonal booking tips. | 0.249234 | The refund ledger for Trip-Anser is fully synced. | 0.294455 |

The new item at position 12, “Conference Anser takes place in Velle,” gets
0.5 × 0.589055 + 0.012186 = 0.306713. Its bridge bonus is produced by the
unchanged frozen bridge logic, so its position is above two facts with slightly
higher raw cosines. All other new items after the prefix have zero coverage and
bridge gain in their winning steps.

## Query cosine comparison for new final items

New-query cosines for fixed items are diagnostic only and do not affect the
frozen prefix. “—” denotes absence from the original final 15.

| New position | Old position | Fact | Original cosine | Resolved cosine |
|---:|---:|---|---:|---:|
| 1 | 1 | Conference Anser now runs from 2026-02-02 through 2026-02-06. | 0.643200 | 0.803390 |
| 2 | 2 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. | 0.397794 | 0.579621 |
| 3 | 3 | Trip-Anser was opened for Conference Anser. | 0.457926 | 0.762472 |
| 4 | — | Registration for Conference Anser is confirmed. | 0.477521 | 0.729908 |
| 5 | — | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. | 0.469017 | 0.686805 |
| 6 | — | Trip-Anser is wrapped up and marked completed. | 0.367866 | 0.674666 |
| 7 | — | There is no receipt for the espresso at the Anser venue for Trip-Anser. | 0.351676 | 0.650805 |
| 8 | — | The Conference Anser registration fee is 360 CR. | 0.443319 | 0.643915 |
| 9 | — | A reminder about Conference Anser logistics checklists went out. | 0.434308 | 0.630767 |
| 10 | — | A receipt for the Conference Anser registration is on file and remains valid. | 0.462348 | 0.627690 |
| 11 | — | Vera said the Conference Anser sessions were worth the travel. | 0.402802 | 0.624035 |
| 12 | — | Conference Anser takes place in Velle. | 0.394944 | 0.589055 |
| 13 | — | The pre-submission tracker for Trip-Anser is fully synced. | 0.361621 | 0.611750 |
| 14 | — | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. | 0.468251 | 0.604684 |
| 15 | — | The refund ledger for Trip-Anser is fully synced. | 0.379857 | 0.588911 |

## Validation, cost, and scope

- Continuing with the archived original query reproduces the entire previous
  selection trace exactly, including the fixed prefix.
- The continuation loop is AST-identical to the frozen selector’s greedy loop.
- Both arms use the same frozen groups, neighborhoods, and bridge strengths.
  Prefix equality, candidate membership, 15 unique items, and input file hashes
  are checked. All archived source and current implementation files are unchanged.
- Exactly one successful request to the existing Aliyun
  `qwen3.7-text-embedding` endpoint, using 1,536 dimensions; no retries.
- The embedding request took 0.606 seconds. The provider reports
  52 input tokens and 52 total tokens; output tokens are omitted, so the ledger
  correctly marks usage as partial. No price is inferred.
- No answer generation or scoring was repeated, so answer accuracy and witness
  recall are not newly measured.
- This is a previously diagnosed q001 case with a manually specified binding.
  It establishes neither a general query-rewrite policy nor automatic binding
  correctness. The default retriever and research selector source were not changed.

## Reproduction and artifacts

The raw directory contains the replay script, query vector, provider response,
ledger, frozen search space, and complete per-arm traces. Reuse the saved vector
without a model call:

```bash
PYTHONPATH=src:sdk/src:. .venv/bin/python outputs/memderive-resolved-query-q001-20261102-r1/replay.py
```

The original execution used `--embed` once. That flag refuses to overwrite an
existing query vector. The report records the original execution time; a later
offline replay may refresh the raw manifest/status but makes no embedding call.

- [Full results and all 141 candidate comparisons](validation-memderive-resolved-query-q001-20261102/results.json)
- [Frozen input manifest](validation-memderive-resolved-query-q001-20261102/manifest.json)
- [Recorded embedding configuration](validation-memderive-resolved-query-q001-20261102/embedding-config.json)
- [Attempt ledger](validation-memderive-resolved-query-q001-20261102/attempts.jsonl)
- [Artifact hashes](validation-memderive-resolved-query-q001-20261102/artifact-hashes.json)
- [Raw run](../../outputs/memderive-resolved-query-q001-20261102-r1)
