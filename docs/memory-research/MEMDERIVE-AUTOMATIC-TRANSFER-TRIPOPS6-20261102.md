# MemDerive tripops_w1: six-question transfer test of automatic retrieval

Sample frozen: 2026-11-02T14:28:11.052011+08:00 (Hong Kong, UTC+8).
Answer/scoring completed: 2026-11-02T14:30:10.820151+08:00 (Hong Kong, UTC+8).

## Main result

The unchanged q001 automatic method fails during local question parsing on all
six additional questions: **0/6 retrieval completions**. No entity path or strict
15-item evidence set is produced for these cases. Its strict official answer
scores are therefore **not available**, rather than fabricated zeros.

With the user's explicit approval, each failed case falls back to the original
question's vector top 15. All six fallback cases reach official scoring:
**answer accuracy 1/6 (16.67%)**, **witnessed accuracy 1/6 (16.67%)**.
These are fallback scores, not evidence that the entity-path method transferred.
The previous q001 result is excluded from these denominators.

Retrieval uses zero generative LLM calls and zero new embedding calls. The six
original question vectors already exist in the old run and are reused exactly.
There are six answer-model calls and no LLM calls in official scoring.

## Sampling and frozen method

Before parsing any selected question, the sample was saved using only question
IDs and categories: the first non-q001 question by ID in each of the five
categories, plus the next temporal question. This supplies a second hotel-style
transfer test alongside broader question types. No saved answer, score, gold,
parser success, or retrieval result determines inclusion. Failed cases are not
replaced by easier questions.

- **q002 (temporal)**: As of 2026-10-07 09:00, which hotel is booked for Conference Falco?
- **q003 (temporal)**: As of 2026-10-07 09:00, which hotel is booked for the conference trip to Skarren?
- **q020 (semantic)**: As of 2026-10-07 09:00, how many of Vera's 2026 business trips go outside her home country?
- **q050 (closure)**: As of 2026-10-07 09:00, who approves Vera's reimbursement claims?
- **q064 (rules)**: As of 2026-10-07 09:00, which of Vera's trips need a fully itemized lodging invoice for their claims?
- **q090 (set_quant)**: As of 2026-10-07 09:00, what is the claimable total for the February conference trip, in CR?

The q001 parser and entity-path source file is copied byte-for-byte. Its SHA-256
is `2ab2ec320dc100fb616502c2f52da084272791680a740b2d2be047b7fa04f369`. spaCy remains 3.8.14 with
`en_core_web_sm` 3.8.0. No parser/ranking adjustment or per-question query rewrite
is made after sampling or observing failures.

These are further development examples from the same previously used world and
old memory snapshot. This category-covering, first-by-ID sample is neither a
random population estimate nor a held-out benchmark split.

## Strict-method failures

The existing parser requires exactly one token with tag `WDT` and dependency
`det`, then exactly one suitable prepositional noun chunk. Every sampled case
fails the first requirement. The recorded exception is `ValueError: not enough
values to unpack (expected 1, got 0)`.

| Question | Actual interrogative token / tag / dependency | Outcome |
| --- | --- | --- |
| q002 | which / WDT / dobj | Failed before entity lookup |
| q003 | which / WDT / dobj | Failed before entity lookup |
| q020 | how / WRB / advmod | Failed before entity lookup |
| q050 | who / WP / nsubj | Failed before entity lookup |
| q064 | which / WDT / nsubj | Failed before entity lookup |
| q090 | what / WP / attr | Failed before entity lookup |

For q002 and q003, spaCy analyzes `which` as a direct object in these complete
sentences. The other questions use constructions such as `how many`, `who`,
`which of`, or `what is` that the narrow q001 rule does not admit. This diagnoses
an implementation coverage problem, not evidence that non-generative retrieval
is inherently unsuitable. Entity resolution and path ranking have not been
exercised successfully on these six questions.

## Authorized fallback and official scoring

The user explicitly chose: retain strict failures, fall back to original-query
vector top 15, and report the two outcomes separately. For this sample, all six
cases take that branch; no shortage/no-path branch is needed or evaluated.

```text
fallback score(memory) = cosine(saved original-question vector, saved memory vector)
selection = highest-scoring 15 eligible facts; memory-reference tie break
```

The query is not rewritten; no entity is injected, no prefix fact is reserved,
and there is no 0.5 multiplier or semantic redundancy penalty. The same 324
eligible facts and observation session 77 are used. Independently recomputing
these ranks exactly reproduces each original run's vector-channel top 15.

Answering uses the unmodified official v9 template and the original question:
`gpt-5.6-sol`, temperature 0, reasoning effort `none`, maximum output 2048 tokens,
one attempt per question. Source-event labels are reconstructed from the saved
message provenance. All six original baseline prompts and evidence lists are
reproduced exactly as a renderer/provenance check.

The pinned official provenance-v2 deterministic scorer is unchanged. Its source
tree matches the upstream lock:
`8f9c69a685740939479b4a849fc6449dd42d71ff2eec8f4c88045912d04731f9`.
Gold is introduced to scoring only after each generated answer is saved.

| Question | Category | Gold answer | Generated fallback answer | Answer | Witnessed | Citation F1 | Stale citation |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| q002 | temporal | Ilxbury Summit Hotel | Ilxbury Summit Hotel | 1 | 1 | 1.0000 | 0 |
| q003 | temporal | Kordva Landing Hotel | Abstained: incomplete evidence | 0 | 0 | 0.0000 | 0 |
| q020 | semantic | 9 | Abstained: incomplete evidence | 0 | 0 | 0.1000 | 0 |
| q050 | closure | Liss | Abstained: incomplete evidence | 0 | 0 | 0.0000 | 0 |
| q064 | rules | ["Trip-Anser", "Trip-Corax", "Trip-Falco", "Trip-Milvus", "Trip-Rukh", "Trip-Tanit"] | Abstained: incomplete evidence | 0 | 0 | 0.0000 | 0 |
| q090 | set_quant | 1601 | 360 | 0 | 0 | 0.1905 | 1 |

All six fallback evidence lists contain exactly 15 unique memories. A zero stale
or distractor rate on an abstention is not a successful answer.

Aggregate official metrics (per-question means for citation P/R/F1):

| Metric | Fallback value |
| --- | ---: |
| answer_accuracy | 0.166667 |
| witnessed_accuracy | 0.166667 |
| answerability_accuracy | 0.333333 |
| witness_precision | 0.444444 |
| witness_recall | 0.193957 |
| witness_f1 | 0.215079 |
| stale_belief_rate | 0.166667 |
| distractor_hit_rate | 0.000000 |

## Post-scoring diagnosis

The following gold-aware checks occur after selection and answer generation and
do not feed either stage.

- **q002 succeeds.** The correct Ilxbury Summit Hotel booking is already at
  vector rank 8 (0.646389), and the answer cites its source `w1_e422`.
- **q003 exposes a retrieval miss.** The correct Kordva Landing Hotel booking
  exists in the eligible pool at rank 56 (0.431875), outside the 15 slots. The
  answer abstains. This is another useful entity-linking case, but the unchanged
  parser never reaches that part of the method.
- **q020 has both evidence gaps and omissions.** Its required witness contains
  38 events. Only 25 have source-attributed representation in the eligible pool,
  and two are represented in the selected 15. The answer knows the trip list
  and Vera's base city but cannot establish the home-country/cross-border count.
  The missing-source audit finds no directly attributed rows in any saved
  version for the 13 absent witness events, including the early geographic facts.
- **q050 lacks the approval chain in the returned evidence.** Three of its four
  witness events are represented somewhere in the eligible pool, but only the
  team-membership source is returned. The Liss finance-partner fact is rank 145.
  The policy source `w1_e022` has no directly attributed row in the saved snapshot.
- **q064 retrieves invoice examples without the required policy and trip set.**
  None of its 14 witness events appears in the returned 15. The policy source
  `w1_e023` has no directly attributed row in the saved snapshot. The answer
  abstains and cites related invoices that official provenance does not accept
  as support for this question.
- **q090 answers a partial amount as a total.** It returns 360 instead of 1601.
  Only one of nine witness events is represented in the selected evidence. The
  selected current registration-fee memory carries both `w1_e057` and `w1_e066`
  as sources; the model cites both, and the scorer flags `w1_e057` as stale.
  This is a provenance issue in addition to incomplete retrieval. Two absent
  eligible witness sources exist only on older stale memory versions; another
  source has no directly attributed row in the saved snapshot.

| Question | Required witness events | Represented in all eligible memories | Represented in selected 15 |
| --- | ---: | ---: | ---: |
| q002 | 1 | 1 | 1 |
| q003 | 1 | 1 | 0 |
| q020 | 38 | 25 | 2 |
| q050 | 4 | 3 | 1 |
| q064 | 14 | 13 | 0 |
| q090 | 9 | 6 | 1 |

Source-ID overlap is an audit of traceability, not proof that memory text fully
preserves an event's meaning. The old snapshot has 408 fact versions and no rule
versions. Later extraction/prompt fixes have not been reapplied by this test;
these outcomes must not be presented as results of freshly rebuilt current-code
memory. The 15-item budget also should not be equated with 15 possible source
citations, because one memory may carry multiple event IDs.

## Implications

The q001 success has not transferred to this sample. The first demonstrated
bottleneck is the parser's narrow input contract. Its broader entity-path ranking
quality cannot be measured here because every strict case stops before lookup.
The approved vector fallback allows complete answer scoring but does not solve
those parsing failures or missing information in the old memory snapshot.

The next controlled change would be broader local question parsing, tested on
these already disclosed failures and separately on fresh questions, while keeping
15 evidence items and no generative retrieval calls. Missing source facts and
stale provenance need separate checks against a rebuilt memory snapshot. No such
fix or new ingestion is included in this experiment.

## Verification, usage, and artifacts

All 48 official metric records reproduce from the saved answers. All cited event
IDs are present in their respective supplied evidence. The six wire requests
match the intended model settings. Retrieval and selection produce no model API
calls; all six recorded calls are answer calls.

Answer generation plus scoring took 26.886 seconds, excluding
preparation and investigation. The provider reports 119,129
input tokens, 1,196 output tokens, and
120,325 total tokens. Captured answer prompts are only
3,754–4,377 characters each, so the reported input usage is unusually high; it is
preserved without inventing a correction or a monetary cost.

- [Frozen sample](validation-memderive-automatic-transfer-tripops6-20261102/sample.json)
- [Unchanged-parser failures and dependency traces](validation-memderive-automatic-transfer-tripops6-20261102/parser-audit.json)
- [Frozen fallback candidates, all scores, top 15, and answer prompts](validation-memderive-automatic-transfer-tripops6-20261102/prepared-fallback.json)
- [Official aggregate scores and usage](validation-memderive-automatic-transfer-tripops6-20261102/summary.json)
- [Per-question results: q002](validation-memderive-automatic-transfer-tripops6-20261102/q002-result.json), [q003](validation-memderive-automatic-transfer-tripops6-20261102/q003-result.json), [q020](validation-memderive-automatic-transfer-tripops6-20261102/q020-result.json), [q050](validation-memderive-automatic-transfer-tripops6-20261102/q050-result.json), [q064](validation-memderive-automatic-transfer-tripops6-20261102/q064-result.json), [q090](validation-memderive-automatic-transfer-tripops6-20261102/q090-result.json)
- [Post-scoring witness and ranking diagnostics](validation-memderive-automatic-transfer-tripops6-20261102/posthoc-diagnostics.json)
- [Missing-source version audit](validation-memderive-automatic-transfer-tripops6-20261102/missing-source-audit.json)
- [Verification](validation-memderive-automatic-transfer-tripops6-20261102/verification.json)
- [Recorded answer attempts](validation-memderive-automatic-transfer-tripops6-20261102/attempts.jsonl)
- [Source preparation](validation-memderive-automatic-transfer-tripops6-20261102/prepare.py) and [fallback/answer execution](validation-memderive-automatic-transfer-tripops6-20261102/run_fallback.py)
- [Artifact hashes](validation-memderive-automatic-transfer-tripops6-20261102/artifact-sha256.json)

The archive preserves credential-redacted raw responses and reused query-vector
responses. No credential file is copied. This is a MemDerive small-sample test;
it does not belong in the MEME-32k 100-case experiment index. Shared production
retrieval code and the original q001 experiment records remain unchanged.
