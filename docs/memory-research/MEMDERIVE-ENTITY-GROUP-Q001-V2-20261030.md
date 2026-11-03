# MemDerive q001: query-relevant connected contributions

This is the preserved v2 result. The current research scripts implement the
[neighbor-threshold follow-up](MEMDERIVE-NEIGHBOR-THRESHOLD-Q001-20261102.md),
which changes neighbor admission only. For exact v2 replay, use this report's
frozen source files in a separate checkout; the invocation below records the
historical v2 interface and input format.

## Outcome

The revised program keeps the complete conference-date / conference-trip / hotel
chain in the **first three of 15 evidence items**. The original freshly answered
control returns incomplete evidence; the revised arm answers Copperline Hotel in
Velle, Trip-Anser, 655 CR. Official answer accuracy and witnessed accuracy are
1/1 for the revised arm, matching v1's scores. The improvement over v1 is explicit
chain retention, not an additional scoring gain.

Execution: **2026-10-30 18:09:26–18:09:58, UTC+8**. Only q001 was answered, once
per arm. There were no selector, embedding, or scoring-model calls. The change
remains in independent research scripts; the shared default retriever is unchanged.

## What changed

The user required that an expanded fact be evaluated against the question, not
accepted merely because it shares an entity. The revised selector distinguishes:

- **Direct contribution:** a new fact's query-vector similarity, reduced by its
  similarity to already selected facts.
- **Connected coverage:** new query terms count toward a complete answer only
  within a single connected group of at most three facts. The objective uses the
  best fully present group's coverage, rather than pooling terms across every
  selected fact regardless of their relationship.
- **Bridge contribution:** a middle fact can be useful even with weak direct
  similarity if it completes a path between two relevant, complementary initial
  candidates. The endpoints must not already share an entity. Its contribution
  depends on the weaker edge, the lower endpoint query similarity, and the extra
  query coverage obtained by combining the endpoints.

The explicit `As of <ISO date/time>,` prefix is separated from lexical content
matching. The original session cutoff, eligibility filters, question sent to the
answerer, and archived query vector are preserved. Only the lexical features
remove the cutoff; the existing query vector still represents the full question.
English stemmed month names and numeric ISO dates receive a shared `month:MM`
feature. The effective q001 terms are `book`, `confer`, `hotel`, `month:02`, `trip`.
No trip, hotel, source ID, question ID, or expected answer is hardcoded in selection.

All original memory eligibility constraints remain: scope, session 77 cutoff,
latest current version chosen before filtering, fresh/accepted/non-invalidated
facts with vectors. There are 324 eligible facts and 36 initial candidates. Each
seed retains at most six same-entity neighbors. The final expansion has **114
facts, 624 groups, and 22 query-relevant candidate bridge paths**.

## Formula and its limits

Let `S` be selected facts, `G` a candidate group, and `new = G - S`.

```text
C(S) = highest weighted query coverage among complete connected groups in S
B(S) = strongest complete query-relevant bridge path in S
R(f) = max(0, cosine(saved query vector, fact vector))
D(f,S) = max(0, maximum cosine(f, any fact already in S))

priority(G | S) = (
    0.5 * (C(S union G) - C(S))
  + 0.5 * sum(R(f) * max(0, 1 - D(f,S)) for f in new)
  + (B(S union G) - B(S))
) / len(new)
```

Query coverage uses corpus inverse-document-frequency weights. For a bridge
`left - center - right`, strength is:

```text
min(left_edge_quality, right_edge_quality)
* min(R(left), R(right))
* (coverage(left union right) - max(coverage(left), coverage(right)))
```

Edge quality uses the most specific shared entity and discounts endpoints listing
many entities, as in v1. Neighbor admission now multiplies edge quality by the
neighbor's direct query similarity and additional query-term coverage. Whole
groups are retained under the 15-item budget; repeated facts do not consume new
slots. Previously earned coverage and bridge strength are not counted again.

The combined-vector cosine is still logged as a diagnostic, but **is no longer
the main selection score**. Per-fact query similarity now contributes through
incremental relevance. This is an explicit change from v1, not a claim that the
original four-signal formula was preserved unchanged.

These are relevance heuristics. Shared-entity connectivity does not establish
semantic entailment, normalized months do not identify the role of every date,
and the best-group/best-bridge objectives do not model all possible multi-answer
or longer-chain questions. Remaining slots can still contain irrelevant facts.

## Development trace

The preliminary v2 offline variant pooled query coverage over all selected facts.
It selected the correct February conference and a different trip's hotel, then
mistakenly treated all query terms as covered. The correct hotel and bridge were
absent from its final 15. No answer-model requests were made for that variant.

The final revision changes that pooling to coverage within a connected group.
After selecting the Conference Anser date, it adds the hotel and connecting fact
together. That step adds 0.5503 connected coverage, 0.0346 bridge strength, and
their nonduplicated direct relevance, for priority 0.2414 per new slot. The full
chain is then available without relying on an assumed name correspondence.

Both offline variants and their source are preserved. The final implementation
was frozen before `dry2`; the same selection was then used for the live calls.
This is development on an already diagnosed q001, including one structural
revision after an offline failure. It is not a held-out or pre-registered test.

## Evidence and official scoring

| Evidence | Original 15 | v1 program 15 | Final v2 program 15 |
| --- | --- | --- | --- |
| Conference Anser runs 2026-02-02 through 2026-02-06 (`w1_e042`) | #7 | Absent | **#1** |
| Copperline Hotel booked for Trip-Anser, 655 CR (`w1_e056`) | Absent | #8 | **#2** |
| Trip-Anser opened for Conference Anser (`w1_e031`) | Absent | Absent | **#3** |
| Velle Arcade Hotel reservation canceled (`w1_e055`) | Absent | Absent | Absent |

The same-scoring, no-expansion offline ablation retains the conference date at
#1 but still omits the target hotel. That ablation was not answered live. It
removes expansion and multi-fact groups together, so it does not isolate their
individual effects.

| Official metric | Original, fresh answer | Final v2, fresh answer |
| --- | ---: | ---: |
| Answer accuracy | 0/1 | 1/1 |
| Witnessed accuracy | 0/1 | 1/1 |
| Answerability accuracy | 0/1 | 1/1 |
| Witness precision | 0 | 1 |
| Witness recall | 0 | 0.5 |
| Witness F1 | 0 | 0.6667 |
| Stale belief rate | 0 | 0 |
| Distractor hit rate | 0 | 0 |

The answerer cites only `w1_e056`, despite the additional chain facts being in
its input. The minimal witness remains `{w1_e055, w1_e056}`; the cancellation fact
does not survive neighbor admission. Official witnessed accuracy passes at
precision and recall of at least 0.5 plus a correct answer. Full witness recovery
is therefore still unresolved. Zero distractor-hit rate concerns cited annotated
distractors and does not imply that the entire 15-item set is relevant.

## Cost and validation

- In-memory index/vector preparation: 0.0689 s. Expansion/group selection:
  **0.8137 s**, excluding file reads, database/tokenization, and network costs.
  The v1 selection observation was 0.1999 s; this prototype scans more group
  combinations when evaluating marginal contributions.
- Two successful OpenLux `gpt-5.6-sol` answer calls, temperature 0, reasoning
  disabled, no seed, 2,048-token output limit. Returned model ID matches the route.
- Control: 1,295 input + 106 output tokens; 9.2449 s.
- Final v2: 1,198 input + 88 output tokens; 21.8002 s.
- Total live usage: **2,493 input + 194 output tokens**, 31.0490 s for the answer
  phase. These single-call timings do not establish latency differences between
  retrieval methods. No monetary price estimate is inferred.
- **15 focused tests passed**, with warnings treated as errors. Coverage includes
  scope/version/cutoff/state filters, budget/deduplication, date normalization,
  low-similarity useful bridges, irrelevant shared-entity neighbors, no repeated
  credit, and prohibition on pooling coverage across disconnected facts.
- All 16 official metric outputs were reproduced. The original vector top 20,
  baseline evidence/prompt, dry/live evidence/prompt, frozen input hashes, and
  15-item budgets were verified. The official prompt, parser and scorer are
  unchanged; no gold data is supplied to selection or generation.

## Artifacts and reproduction

- [Summary, retention and cost](validation-memderive-entity-group-q001-v2-20261030/summary.json)
- [Candidates, marginal contributions and exact prompts](validation-memderive-entity-group-q001-v2-20261030/offline.json)
- [Answers and official scores](validation-memderive-entity-group-q001-v2-20261030/results.jsonl)
- [Manifest](validation-memderive-entity-group-q001-v2-20261030/manifest.json) and
  [verification](validation-memderive-entity-group-q001-v2-20261030/verification.json)
- [Failed preliminary offline variant](validation-memderive-entity-group-q001-v2-20261030/preliminary-offline/offline.json)
- [v1 report and frozen source](MEMDERIVE-ENTITY-GROUP-Q001-20261030.md)

Current experimental code is `scripts/memory_research/entity_groups.py` and
`entity_group_probe.py`. The validation directory preserves source snapshots.
The raw run and redacted request/response archives remain under
`outputs/memderive-entity-group-q001-v2-20261030-r1`.

With the project environment, NumPy, and pinned external DerivEval checkout,
choose a fresh output directory. This invocation makes no model calls:

```bash
PYTHONPATH=src:sdk/src:.:/private/tmp/contexthub-deriveval-reference/src \
.venv/bin/python -m scripts.memory_research.entity_group_probe \
  --baseline outputs/memderive-tripops-w1-q1-20261027-r1 \
  --output outputs/memderive-entity-group-q001-v2-replay \
  --query-embedding outputs/memderive-tripops-w1-q1-20261027-r1/q1/state/16b9691342b1428fb73073d2fcbf3d4c/responses/b885e2ef-df11-4b8c-8dcd-fd7bfe2fdea3-1.json \
  --query-lexemes docs/memory-research/validation-memderive-entity-group-q001-v2-20261030/query-lexemes.json
```

Adding `--execute` makes two real answer requests using the local provider
configuration. This small MemDerive probe is not entered in the MEME-32k index.
