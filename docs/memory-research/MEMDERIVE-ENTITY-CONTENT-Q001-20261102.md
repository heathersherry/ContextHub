# MemDerive q001: separate entity scope from content similarity

## Outcome

Within the same complete 20-fact Trip-Anser entity pool, removing Trip-Anser from
the similarity query improves the relative ordering of the hotel evidence:
the active hotel moves from cosine rank **11 to 6**, the canceled hotel from
**12 to 5**, and the coffee receipt from **3 to 9**. The final evidence position
of the cancellation changes **13 → 8**, while coffee changes **5 → 11**.
The active hotel stays at position 2 because it is explicitly fixed.

Both arms already retain both hotel facts, so this comparison improves their
relative ordering rather than demonstrating additional target recall within
the final 15. The content-only arm still ranks general travel/admin status facts
above hotel evidence. No answers or official benchmark scores were rerun.

Execution: **2026-11-02 11:28:20–11:28:31, Hong Kong time (UTC+8)**.
Total run time: 11.182 seconds.

## Queries

- **A, entity in query:** `As of 2026-10-07 09:00, what is the hotel booking status for Trip-Anser?`
- **B, content only:** `As of 2026-10-07 09:00, what is the hotel booking status?`

The exact same time prefix is retained. The only text removed between the two
queries is ` for Trip-Anser`. A reuses the archived vector from the preceding
query-focus probe. B is fixed before its single new embedding call.

## Candidate scope and unchanged ranking

- Apply the original scope, latest-version, cutoff-session 77, and fact
  eligibility filters to obtain the same 324 eligible facts.
- Use the existing entity index to collect all 20 facts tagged `trip-anser`.
  Eleven appeared in the previous 141-candidate pool; nine are newly admitted
  by this complete entity lookup. No cosine threshold excludes these 20 facts.
- Preserve the three original evidence items and their trace. Two are in the
  entity pool; the conference-date fact is outside it. There are 21 distinct
  available facts in total, with 18 unfixed candidates competing for 12 slots.
- Use the unchanged group/bridge functions with scoped facts as direct seeds,
  restricting constructed groups to the 20 facts plus fixed evidence. Keep the
  full-324-fact query weights, entity frequencies, and original-query bridge
  calculations. The existing neighbor rule affects group edges, not admission
  of the 20 direct entity candidates.
- Freeze the resulting **1,251 groups and 19 bridge paths** before both arms.
  Only the individual direct-relevance query cosine changes between arms.
  Coverage/bridge formulas, weights, greedy loop, tie breaks, exact-reference
  deduplication, and the final 15-item budget remain unchanged.
- The prefix already has coverage 1.0 and the strongest scoped bridge. Every
  later winning addition therefore has zero coverage and bridge gain. The
  unchanged selector is independently verified to choose exactly the top 12
  unfixed facts by cosine, in order.

The new entity scope is shared by A and B; its effect is not conflated with
removing the entity name. A is recomputed on these 20 candidates, so its final
positions differ from the preceding 141-candidate experiment.

## Tracked facts

| Fact | Query | Cosine rank / 20 | Query cosine | Weighted direct score | Final position |
|---|---|---:|---:|---:|---:|
| active hotel | A | 11 | 0.592014 | 0.296007 | 2 |
| active hotel | B | 6 | 0.367130 | 0.183565 | 2 |
| cancelled hotel | A | 12 | 0.577831 | 0.288915 | 13 |
| cancelled hotel | B | 5 | 0.375287 | 0.187643 | 8 |
| coffee | A | 3 | 0.686047 | 0.343023 | 5 |
| coffee | B | 9 | 0.314313 | 0.157156 | 11 |

The weighted direct score is 0.5 × query cosine. It is also the actual winning
score for cancellation and coffee. For the active hotel it is diagnostic only:
its final position and original two-fact group score **0.368799** are frozen.
Its improvement must be assessed using cosine rank, not fixed output position.

Absolute cosine values decrease in B; their scale across different query vectors
is not an answer-quality metric. The important observed change is relative
ordering within the same entity pool: both hotel facts now outrank coffee.

## All 20 entity candidates

Ordered by B cosine rank. “—” means not selected into the final 15. The fixed
conference-date fact is outside this table because it is not tagged Trip-Anser.

| B cosine rank | A cosine rank | Fact | A cosine | B cosine | A final position | B final position |
|---:|---:|---|---:|---:|---:|---:|
| 1 | 10 | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. | 0.627414 | 0.459363 | 12 | 4 |
| 2 | 5 | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. | 0.677542 | 0.399765 | 7 | 5 |
| 3 | 1 | Trip-Anser is wrapped up and marked completed. | 0.754983 | 0.376127 | 4 | 6 |
| 4 | 7 | The refund ledger for Trip-Anser is fully synced. | 0.646684 | 0.375431 | 9 | 7 |
| 5 | 12 | The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge. | 0.577831 | 0.375287 | 13 | 8 |
| 6 | 11 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. | 0.592014 | 0.367130 | 2 | 2 |
| 7 | 4 | The pre-submission tracker for Trip-Anser is fully synced. | 0.681311 | 0.363930 | 6 | 9 |
| 8 | 9 | The receipt export for Trip-Anser finished. | 0.639584 | 0.315947 | 11 | 10 |
| 9 | 3 | There is no receipt for the espresso at the Anser venue for Trip-Anser. | 0.686047 | 0.314313 | 5 | 11 |
| 10 | 20 | The portal verified the travel receipt for the Vantia Crest flex fare (Trip-Anser); the receipt for 528 CR is on file. | 0.470170 | 0.310290 | — | 12 |
| 11 | 2 | Trip-Anser was opened for Conference Anser. | 0.720656 | 0.310063 | 3 | 3 |
| 12 | 14 | The receipt for the portable slide dock for Trip-Anser is saved. | 0.560317 | 0.282457 | 15 | 13 |
| 13 | 15 | Every submitted receipt for Trip-Anser is included in the receipt export. | 0.553019 | 0.271631 | — | 14 |
| 14 | 13 | The portable slide dock for Trip-Anser cost 39 CR. | 0.571837 | 0.259852 | 14 | 15 |
| 15 | 6 | The ride-hail from the Anser venue for Trip-Anser cost 31 CR. | 0.652903 | 0.257760 | 8 | — |
| 16 | 16 | The receipt for the airport shuttle into Velle for Trip-Anser is saved. | 0.515795 | 0.257112 | — | — |
| 17 | 8 | The espresso at the Anser venue for Trip-Anser cost 8 CR. | 0.643187 | 0.255308 | 10 | — |
| 18 | 17 | The airport shuttle into Velle for Trip-Anser cost 23 CR. | 0.514423 | 0.250340 | — | — |
| 19 | 18 | The Velle metro pass for Trip-Anser cost 27 CR. | 0.494502 | 0.217867 | — | — |
| 20 | 19 | The receipt for the Velle metro pass for Trip-Anser is saved. | 0.485889 | 0.209694 | — | — |

## Complete final evidence lists

Actual incremental selection scores are shown. The first three entries and
scores are unchanged; positions 2 and 3 share a group score.

| Position | A: entity in query | Score | B: content-only query | Score |
|---:|---|---:|---|---:|
| 1 | Conference Anser now runs from 2026-02-02 through 2026-02-06. | 0.546449 | Conference Anser now runs from 2026-02-02 through 2026-02-06. | 0.546449 |
| 2 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. | 0.368799 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. | 0.368799 |
| 3 | Trip-Anser was opened for Conference Anser. | 0.368799 | Trip-Anser was opened for Conference Anser. | 0.368799 |
| 4 | Trip-Anser is wrapped up and marked completed. | 0.377492 | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. | 0.229681 |
| 5 | There is no receipt for the espresso at the Anser venue for Trip-Anser. | 0.343023 | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. | 0.199882 |
| 6 | The pre-submission tracker for Trip-Anser is fully synced. | 0.340656 | Trip-Anser is wrapped up and marked completed. | 0.188064 |
| 7 | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. | 0.338771 | The refund ledger for Trip-Anser is fully synced. | 0.187715 |
| 8 | The ride-hail from the Anser venue for Trip-Anser cost 31 CR. | 0.326451 | The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge. | 0.187643 |
| 9 | The refund ledger for Trip-Anser is fully synced. | 0.323342 | The pre-submission tracker for Trip-Anser is fully synced. | 0.181965 |
| 10 | The espresso at the Anser venue for Trip-Anser cost 8 CR. | 0.321593 | The receipt export for Trip-Anser finished. | 0.157973 |
| 11 | The receipt export for Trip-Anser finished. | 0.319792 | There is no receipt for the espresso at the Anser venue for Trip-Anser. | 0.157156 |
| 12 | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. | 0.313707 | The portal verified the travel receipt for the Vantia Crest flex fare (Trip-Anser); the receipt for 528 CR is on file. | 0.155145 |
| 13 | The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge. | 0.288915 | The receipt for the portable slide dock for Trip-Anser is saved. | 0.141228 |
| 14 | The portable slide dock for Trip-Anser cost 39 CR. | 0.285919 | Every submitted receipt for Trip-Anser is included in the receipt export. | 0.135816 |
| 15 | The receipt for the portable slide dock for Trip-Anser is saved. | 0.280159 | The portable slide dock for Trip-Anser cost 39 CR. | 0.129926 |

## Interpretation and limits

The result supports separating entity scope from content similarity on this
known question. It does not establish a complete relevance solution: B still
puts the annual travel summary, PS-1 closure, trip completion, and refund-ledger
sync ahead of cancellation. The latter scores 0.375287, just below the
refund-ledger sync at 0.375431. The active hotel is only sixth by cosine.
Coffee remains in the final 15 rather than being excluded.

The entity binding is manually specified from existing supporting facts; this
does not test automatic entity resolution or missing entity tags. Both arms
use the same entity pool and frozen prefix. The prefix, query, and candidate
pool were not tuned after inspecting the new vector. No answer or citation
score improvement is claimed.

## Validation and usage

- The complete previous 141-candidate trace is reproduced exactly before the
  new entity scope is evaluated. The same frozen selector and AST-verified
  continuation loop are used without editing implementation files.
- All 20 entity candidates have direct singleton entries. The prefix is identical
  across both arms; every later selected fact belongs to the entity scope; both
  final lists have exactly 15 unique references.
- Frozen groups, paths, candidates, and input/source hashes are unchanged across
  arms. The 12-item continuation matches an independent cosine sort exactly.
- One new successful Aliyun `qwen3.7-text-embedding` request, 1,536 dimensions,
  no retries. A reuses its previous vector; no memory vectors are regenerated.
- The new request took 0.434 seconds. The provider reports 43 input and
  43 total tokens, with output tokens omitted; the ledger records partial usage.
  No price is inferred. No selector LLM, answer, or scorer calls were made.
- Existing core and research-selector implementation files are unchanged.

## Reproduction and artifacts

Replay using the saved vector, without network calls:

```bash
PYTHONPATH=src:sdk/src:. .venv/bin/python outputs/memderive-entity-content-q001-20261102-r1/replay.py
```

The original execution used `--embed` once. Frozen manifests record required
prior output paths and hashes. The validation copy preserves this execution
even if the raw directory is later replayed.

- [Results, rankings, and all selection traces](validation-memderive-entity-content-q001-20261102/results.json)
- [Candidate scope and input manifest](validation-memderive-entity-content-q001-20261102/manifest.json)
- [Frozen groups and paths](validation-memderive-entity-content-q001-20261102/frozen-search-space.json)
- [Recorded embedding configuration](validation-memderive-entity-content-q001-20261102/embedding-config.json)
- [Attempt ledger](validation-memderive-entity-content-q001-20261102/attempts.jsonl)
- [Artifact hashes](validation-memderive-entity-content-q001-20261102/artifact-hashes.json)
- [Previous query-focus probe](MEMDERIVE-QUERY-FOCUS-Q001-20261102.md)
- [Raw run](../../outputs/memderive-entity-content-q001-20261102-r1)
