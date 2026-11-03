# MemDerive q001: query-focus ablation with a frozen prefix

## Outcome

Both new queries admit the canceled hotel booking into the final 15: position
**12** after removing the conference description (A), and position **11** after
further changing the request to hotel-booking status (B). The previous resolved
query omitted it. The coffee-receipt fact remains above it: position **5** in
both new arms, compared with position 7 previously. Simplification helps retain
the cancellation but does not make the ranking prefer hotel evidence over
other information about the same trip.

Execution: **2026-11-02 11:19:15–11:19:41, Hong Kong time (UTC+8)**.
Total run time: 26.170 seconds. This is offline ranking only; no
answers or official benchmark scores were produced.

## Queries and controls

- **Previous resolved query:** `As of 2026-10-07 09:00, which hotel booking is currently active for Trip-Anser, the February conference trip?`
- **A: remove conference description:** `As of 2026-10-07 09:00, which hotel booking is currently active for Trip-Anser?`
- **B: hotel booking status:** `As of 2026-10-07 09:00, what is the hotel booking status for Trip-Anser?`

All queries keep the exact same `As of 2026-10-07 09:00,` prefix. Both new
queries were fixed before embedding calls and were not revised after results.

- The first three facts and their complete selection trace are fixed: Anser
  conference dates, Copperline Hotel booking, and the explicit Trip-Anser /
  Conference Anser relation. These facts still occupy three of the 15 slots.
- All arms reuse the same 141 candidates from 36 seeds and 324 eligible memories,
  threshold 0.35, 3,300 groups, and 70 precomputed bridge paths.
- Only the individual direct-relevance query cosine is replaced after the prefix.
  Neighbor admission, group membership, original query lexemes, coverage, bridge
  strengths, weights, tie breaks, and the greedy loop are unchanged.
- The previous resolved control uses its archived vector. Its entire selection
  trace is reproduced exactly; no new control embedding request is made.
- Trip-Anser remains a manually specified binding supported by the fixed facts.
  Automatic entity resolution is not evaluated.

## Cancellation and coffee comparison

Cancellation: “The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge.”

Coffee: “There is no receipt for the espresso at the Anser venue for Trip-Anser.”

| Fact | Query | Cosine | Cosine rank / 141 | Final position / 15 | Score after fixed prefix |
|---|---|---:|---:|---:|---:|
| Cancellation | Previous resolved query | 0.552459 | 18 | Not selected | 0.276229 |
| Cancellation | A: remove conference description | 0.566328 | 12 | 12 | 0.283164 |
| Cancellation | B: hotel booking status | 0.577831 | 11 | 11 | 0.288915 |
| Coffee | Previous resolved query | 0.650805 | 6 | 7 | 0.325403 |
| Coffee | A: remove conference description | 0.687111 | 3 | 5 | 0.343555 |
| Coffee | B: hotel booking status | 0.686047 | 3 | 5 | 0.343023 |

The score column is evaluated in the same fixed three-fact state. Both tracked
facts have zero coverage and bridge gains there, so it equals 0.5 × cosine.
For these facts whenever selected, it also equals their actual winning score.
The previous cancellation has no winning score because it was not selected.
Cosine ranks refer to all 141 candidates; they are not final evidence positions.

## Interpretation

Removing the conference description raises cancellation cosine only modestly
(0.552459 → 0.566328), but six previously higher-cosine candidates move below
it. These include conference receipt reminders, logistics reminders, a session
appraisal, the conference city, the registration receipt, and the Trip-Vireo
classification. Thus admission improves partly through other candidates moving
down, not through suppression of the coffee fact.

Coffee cosine actually increases from 0.650805 to 0.687111 in A. Changing the
query to status in B gives 0.686047, still above cancellation at 0.577831.
The result does not support attributing the coffee error solely to the retained
conference phrase. It is consistent with strong trip/entity matching and weak
discrimination of the particular hotel-booking relation; this is an
interpretation of rankings, not a token-level explanation of the embedding.

B improves cancellation by one final position over A on this one question.
It does not establish a general retrieval improvement: B also selects completion
status for other trips and an unrelated Trip-Corax hotel booking. No follow-up
query variants or ranking fixes were tried after observing these results.

## Complete final lists

Each cell contains the original memory text and its actual incremental selection
score. The prefix is frozen, including its original scores. Positions 2 and 3
share one two-addition group score. Every later winning addition in A and B is
a single fact with zero coverage/bridge bonus, so its score is 0.5 × cosine.

| Position | Previous resolved query | A: remove conference | B: booking status |
|---:|---|---|---|
| 1 | Conference Anser now runs from 2026-02-02 through 2026-02-06. **(0.546449)** | Conference Anser now runs from 2026-02-02 through 2026-02-06. **(0.546449)** | Conference Anser now runs from 2026-02-02 through 2026-02-06. **(0.546449)** |
| 2 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. **(0.368799)** | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. **(0.368799)** | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. **(0.368799)** |
| 3 | Trip-Anser was opened for Conference Anser. **(0.368799)** | Trip-Anser was opened for Conference Anser. **(0.368799)** | Trip-Anser was opened for Conference Anser. **(0.368799)** |
| 4 | Registration for Conference Anser is confirmed. **(0.364954)** | Trip-Anser is wrapped up and marked completed. **(0.367829)** | Trip-Anser is wrapped up and marked completed. **(0.377492)** |
| 5 | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. **(0.343403)** | There is no receipt for the espresso at the Anser venue for Trip-Anser. **(0.343555)** | There is no receipt for the espresso at the Anser venue for Trip-Anser. **(0.343023)** |
| 6 | Trip-Anser is wrapped up and marked completed. **(0.337333)** | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. **(0.341020)** | The pre-submission tracker for Trip-Anser is fully synced. **(0.340656)** |
| 7 | There is no receipt for the espresso at the Anser venue for Trip-Anser. **(0.325403)** | The pre-submission tracker for Trip-Anser is fully synced. **(0.326674)** | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. **(0.338771)** |
| 8 | The Conference Anser registration fee is 360 CR. **(0.321957)** | Registration for Conference Anser is confirmed. **(0.322274)** | The refund ledger for Trip-Anser is fully synced. **(0.323342)** |
| 9 | A reminder about Conference Anser logistics checklists went out. **(0.315383)** | The refund ledger for Trip-Anser is fully synced. **(0.317930)** | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. **(0.313707)** |
| 10 | A receipt for the Conference Anser registration is on file and remains valid. **(0.313845)** | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. **(0.310684)** | Registration for Conference Anser is confirmed. **(0.313105)** |
| 11 | Vera said the Conference Anser sessions were worth the travel. **(0.312018)** | The Conference Anser registration fee is 360 CR. **(0.284348)** | The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge. **(0.288915)** |
| 12 | Conference Anser takes place in Velle. **(0.306713)** | The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge. **(0.283164)** | Trip-Tanit is wrapped up and marked completed. **(0.281728)** |
| 13 | The pre-submission tracker for Trip-Anser is fully synced. **(0.305875)** | A reminder about Conference Anser logistics checklists went out. **(0.278500)** | Trip-Rukh wrapped up on 2026-09-10 at 09:20. **(0.278192)** |
| 14 | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. **(0.302342)** | A receipt for the Conference Anser registration is on file and remains valid. **(0.276416)** | A reminder about Conference Anser logistics checklists went out. **(0.273771)** |
| 15 | The refund ledger for Trip-Anser is fully synced. **(0.294455)** | Trip-Rukh wrapped up on 2026-09-10 at 09:20. **(0.275502)** | Ostin Terrace Hotel in Ostin is booked for Trip-Corax at 605 CR for the stay. **(0.268706)** |

## Validation and usage

- The previous resolved-query selection trace is reproduced exactly.
- The frozen greedy continuation loop is reused from the preceding probe; its
  AST matches the frozen selector loop. No selector implementation was changed.
- All three arms retain the identical first three facts and exactly 15 unique
  candidate references. Frozen search-space and input/source file hashes match.
- Every score is independently checked against the stored coverage, direct
  relevance, bridge, and added-slot components.
- Exactly two successful embedding calls, no retries: existing Aliyun
  `qwen3.7-text-embedding`, 1,536 dimensions. Each reports 47 input tokens and
  47 total tokens: **94 input/total tokens** across the two calls. Output tokens
  are omitted by the provider, so recorded usage is partial. No price is inferred.
- Request times: A 0.505 s; B 0.210 s. No selector LLM, answer, scorer, ingestion,
  or memory-embedding calls. Existing core/research selector files are unchanged.
- This is one previously diagnosed development question, with one vector per
  query variant and no held-out or answer-score claim.

## Reproduction and artifacts

Replay with the saved vectors and no network calls:

```bash
PYTHONPATH=src:sdk/src:. .venv/bin/python outputs/memderive-query-focus-q001-20261102-r1/replay.py
```

The original run used `--embed` once to obtain the two vectors. Input paths and
hashes are frozen in the manifest; archived prior runs are required. The report
and validation copy preserve this execution even if the raw directory is replayed.

- [All candidate scores, ranks, target diagnostics, and selection traces](validation-memderive-query-focus-q001-20261102/results.json)
- [Input manifest and exact queries](validation-memderive-query-focus-q001-20261102/manifest.json)
- [Embedding configuration](validation-memderive-query-focus-q001-20261102/embedding-config.json)
- [Attempt ledger](validation-memderive-query-focus-q001-20261102/attempts.jsonl)
- [Artifact hashes](validation-memderive-query-focus-q001-20261102/artifact-hashes.json)
- [Previous resolved-query experiment](MEMDERIVE-RESOLVED-QUERY-Q001-20261102.md)
- [Raw execution](../../outputs/memderive-query-focus-q001-20261102-r1)
