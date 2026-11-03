# MemDerive tripops_w1: parserless entity expansion on seven questions

Completed: 2026-11-02T15:05:39.720024+08:00 (Hong Kong, UTC+8).

## Result

All seven requested questions complete retrieval with exactly 15 unique memories.
There is no syntactic gate and no fallback branch. The program matches entities
and expands the candidate pool to 209–219 facts per question. However, **all final
lists equal their original full-question vector top 15**.

Seven fresh answers were generated and scored with the pinned official evaluator:
**answer accuracy 1/7 (14.29%)**, **witnessed accuracy 1/7 (14.29%)**. Only q002
passes. The six transfer cases remain at 1/6, while q001 changes from the earlier
successful resolved-content-query result to an incorrect abstention.

The top-15 invariance was identified and recorded before answer calls. This is a
minimal candidate-generation experiment with unchanged relevance scoring, not a
successful graph-aware reranker. It removes sentence-form failure without showing
an effectiveness improvement.

## Frozen procedure

The sample is q001 plus exactly the same six prior cases: q002, q003, q020, q050,
q064, and q090. No new model is introduced. The old 324-fact memory snapshot,
original-question embeddings, and observation session 77 are reused.

1. Match existing normalized entity labels directly against the original question
   body. Normalization is the existing NFKC/casefold/whitespace routine; entity
   patterns are the shared literal boundary matcher. The explicit `As of ...`
   prefix is excluded only from literal entity matching. The full original
   question, including its time prefix, remains the embedding query.
2. Initial candidates are the union of the full-question vector top 15 and every
   eligible fact indexed under a matched entity. No entity name, target ref,
   answer text, fixed prefix, or manually rewritten query is supplied.
3. Expand through the entity index for two breadth-first memory-to-memory steps.
   Every shared-entity neighbor is considered, with exact-reference deduplication.
   There is no top-six neighbor cap or entity-type/sentence-form filter. The two
   expansion steps are an explicit experimental depth budget, not a claim that
   two steps suffice for arbitrary questions.
4. Rank the resulting union by raw cosine against the same original-question
   vector, with memory-reference tie-breaking, and return the top 15. No entity
   bonus, path score, 0.5 multiplier, content-query rewrite, or similarity penalty
   is applied. Expansion paths are recorded but do not influence the score.

The implementation does not call a parser or require `which`, `who`, `how many`,
or a unique noun/prepositional phrase. Shared entities are candidate connections,
not proof of the relation needed by the question.

| Question | Literal entity matches | Initial facts | Added at hop 1 | Added at hop 2 | Expanded facts | Final facts |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| q001 | conference trip, february | 16 | 73 | 127 | 216 | 15 |
| q002 | conference falco | 15 | 77 | 121 | 213 | 15 |
| q003 | conference trip, skarren | 16 | 96 | 97 | 209 | 15 |
| q020 | 2026, vera | 41 | 155 | 18 | 214 | 15 |
| q050 | vera | 40 | 99 | 72 | 211 | 15 |
| q064 | invoice, vera | 43 | 113 | 55 | 211 | 15 |
| q090 | conference trip, cr, february | 20 | 84 | 115 | 219 | 15 |

The index contains generic labels such as `conference trip`, `february`, `invoice`,
and `cr`, as well as named entities and numeric labels. Literal matches are not
all equally discriminating. This experiment preserves those matches as observed;
it does not retrospectively remove noisy labels to improve this sample.

## Why expansion cannot change this ranking

Let `S` be the global top 15 under the original-question cosine, and `C` the
expanded candidates. Initial candidate construction guarantees `S` is a subset
of `C`. The final score and tie-breaker are identical. Consequently:

```text
top15(C, original_query_cosine) = S
```

Every added fact is below the global top 15 under the unchanged score. Enlarging
the candidate pool cannot make it outrank those facts. The invariant is checked
for all seven questions and was stated to the user before evaluation. No hidden
ranking change is introduced to force an improvement.

## Official answer results

The official v9 prompt, original question, GPT-5.6-sol model route, temperature 0,
reasoning effort `none`, output limit 2048, and one-attempt policy are retained.
All seven answers are fresh calls; none is reused from a prior answer file.
Gold is introduced only after each answer is generated and saved.

| Question | Previous answer score | Current answer score | Current witnessed score | Citation F1 | Stale citation | Current answer |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| q001 | 1 | 0 | 0 | 0.0000 | 0 | Abstained |
| q002 | 1 | 1 | 1 | 1.0000 | 0 | Ilxbury Summit Hotel |
| q003 | 0 | 0 | 0 | 0.0000 | 0 | Abstained |
| q020 | 0 | 0 | 0 | 0.1000 | 0 | Abstained |
| q050 | 0 | 0 | 0 | 0.0000 | 0 | Abstained |
| q064 | 0 | 0 | 0 | 0.0000 | 0 | Abstained |
| q090 | 0 | 0 | 0 | 0.1905 | 1 | 360 |

The previous q001 comparator used automatically resolved Trip-Anser scope and a
content query with the locator phrase removed. The previous six comparators used
the approved original-query vector fallback. This historical comparison is not a
uniform single-variable ablation. In particular, q001's regression must not be
attributed solely to removing the syntactic gate: this run also omits its former
entity-scope restriction and content-query ranking.

For the six non-q001 cases, the complete evidence and answer prompt are byte-for-
byte identical to the previous run. Fresh generations may still differ. q003's
citation precision changes from 0 to 0.5 because the new answer cites events,
while its answer and witnessed accuracy remain 0. That difference is answer
variation under unchanged inputs, not a retrieval gain.

Aggregate official metrics across all seven questions:

| Metric | Value |
| --- | ---: |
| answer_accuracy | 0.142857 |
| witnessed_accuracy | 0.142857 |
| answerability_accuracy | 0.285714 |
| witness_precision | 0.452381 |
| witness_recall | 0.166249 |
| witness_f1 | 0.184354 |
| stale_belief_rate | 0.142857 |
| distractor_hit_rate | 0.000000 |

q090 again reports 360 rather than the gold total 1601, and again cites the stale
source `w1_e057`. q020, q050, and q064 abstain. The earlier old-snapshot information
and provenance gaps remain unchanged; no new ingestion is performed.

## Expansion succeeds in finding facts that final ranking discards

Post-scoring target inspection shows that the key hotel records are in the
expanded candidate pools:

| Question | Memory | Original-query cosine | Global rank among 324 | Rank within expanded pool | Selected |
| --- | --- | ---: | ---: | ---: | --- |
| q001 | active_hotel | 0.397794 | 107 | 83 | No |
| q001 | cancelled_hotel | 0.389032 | 119 | 90 | No |
| q001 | coffee_missing_receipt | 0.351676 | 168 | 129 | No |
| q003 | correct_hotel | 0.431875 | 56 | 45 | No |

Thus q001's new hotel and cancellation are not missing from candidate generation.
They rank 83 and 90 within its 216 candidates and are discarded at the final cut.
q003's correct hotel is present too, at expanded-pool rank 45. This distinguishes
candidate reachability from final evidence selection.

The archived routes are deterministic example shortest co-occurrence paths, not
semantic resolutions. For example, an expansion may connect through a common
city or a multi-trip summary. The report does not claim that the system has
proved which trip the question refers to merely because a hotel is reachable.

## Implication and scope

Removing the hard parser gate restores coverage. With this particular scoring
rule, entity expansion cannot improve the final evidence set. To evaluate an
actual ranking benefit, a subsequent experiment must let entity/path evidence
change candidate constraints or ranking, and then test that change separately.
Increasing expansion depth alone would not break the demonstrated invariant.
No such additional change is included here.

This seven-question set is development data already inspected in earlier turns.
It is not a held-out evaluation or a full-world result. Current extraction-prompt
changes are not represented because the old frozen memory snapshot is reused.
The shared production retriever is unchanged; this remains an isolated probe.

## Verification, time, and usage

Offline replay reproduces every selection and path trace. All 56 official metric
records reproduce from saved answers. All seven original baseline evidence lists
and answer prompts reconstruct exactly. The current selected references match
the archived vector-channel top 15 in every case. Every generated citation refers
to an event present in its supplied evidence, although some are not valid support
under official scoring.

The evaluator's source-tree hash matches the pinned upstream lock:
`8f9c69a685740939479b4a849fc6449dd42d71ff2eec8f4c88045912d04731f9`.

Calls: **7 fresh answer calls, 0 retrieval-generation calls, 0 new embedding
calls, 0 scorer-LLM calls**. Answering plus scoring took 32.999
seconds, excluding preparation and investigation.

Provider-reported usage is 165,078 input tokens,
1,298 output tokens, and 166,376
total tokens. The captured prompts contain 3,754–4,377 characters each, so the
reported input usage is unusually large. It is retained as returned without an
invented correction or inferred monetary cost.

- [Frozen sample and rules](validation-memderive-parserless-tripops7-20261102/sample.json)
- [Complete entity matches, expansion edges/routes, scores, top 15, and prompts](validation-memderive-parserless-tripops7-20261102/prepared.json)
- [Selector source](validation-memderive-parserless-tripops7-20261102/selector.py)
- [Experiment runner](validation-memderive-parserless-tripops7-20261102/run.py)
- [Official summary](validation-memderive-parserless-tripops7-20261102/summary.json)
- [Previous/current answer and metric comparison](validation-memderive-parserless-tripops7-20261102/comparison.json)
- [Hotel and coffee ranking diagnostics](validation-memderive-parserless-tripops7-20261102/posthoc-diagnostics.json)
- [Verification](validation-memderive-parserless-tripops7-20261102/verification.json)
- [Recorded attempts](validation-memderive-parserless-tripops7-20261102/attempts.jsonl)
- [Artifact hashes](validation-memderive-parserless-tripops7-20261102/artifact-sha256.json)

Per-question official results: [q001](validation-memderive-parserless-tripops7-20261102/q001-result.json),
[q002](validation-memderive-parserless-tripops7-20261102/q002-result.json),
[q003](validation-memderive-parserless-tripops7-20261102/q003-result.json),
[q020](validation-memderive-parserless-tripops7-20261102/q020-result.json),
[q050](validation-memderive-parserless-tripops7-20261102/q050-result.json),
[q064](validation-memderive-parserless-tripops7-20261102/q064-result.json),
and [q090](validation-memderive-parserless-tripops7-20261102/q090-result.json).

The archive includes credential-redacted raw responses and the seven reused
query-vector responses. No credential file is copied. This small MemDerive run
does not belong in the MEME-32k 100-case index.
