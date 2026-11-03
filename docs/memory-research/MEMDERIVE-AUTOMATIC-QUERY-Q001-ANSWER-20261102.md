# MemDerive q001: official answer and citation evaluation after automatic retrieval

Completed: 2026-11-02T14:23:57.466010+08:00 (Hong Kong, UTC+8).

## Result

The frozen 15-item result from the [automatic retrieval probe](MEMDERIVE-AUTOMATIC-QUERY-Q001-20261102.md)
produces a correct answer with complete, valid citations. All six positive metrics
are 1; both error rates are 0. This is one previously diagnosed development
question, not a full-world or full-benchmark result.

Generated answer:

> Copperline Hotel in Velle, booked at 655 CR for the stay

The answer explains that Copperline Hotel is the active booking and that the
Velle Arcade Hotel reservation was canceled. It cites `w1_e056` (new booking)
and `w1_e055` (old booking canceled), exactly covering the required witness set.
No coffee, general travel-summary, or administrative evidence is cited.

| Official metric | Value | Direction |
| --- | ---: | --- |
| answer_accuracy | 1 | Higher is better |
| witnessed_accuracy | 1 | Higher is better |
| answerability_accuracy | 1 | Higher is better |
| witness_precision | 1 | Higher is better |
| witness_recall | 1 | Higher is better |
| witness_f1 | 1 | Higher is better |
| stale_belief_rate | 0 | Lower is better |
| distractor_hit_rate | 0 | Lower is better |

## Unchanged inputs and protocol

- The selected 15 memory references, text, order, and reconstructed source event
  IDs come from the saved automatic retrieval. No evidence was added or reranked.
- The official v9 answer template is used unchanged, with the original question.
  The original baseline's reconstructed evidence and answer prompt exactly match
  its archived versions, checking the source mapping and renderer.
- Answer model: `gpt-5.6-sol` through the existing OpenLux configuration,
  temperature 0, reasoning effort `none`, output limit 2048, one attempt.
- Official deterministic provenance-v2 scoring is used. The cached evaluator's
  entire source-tree hash matches `integrations/memderive/upstream-lock.json`:
  `8f9c69a685740939479b4a849fc6449dd42d71ff2eec8f4c88045912d04731f9`.
  Prompt and scorer file hashes also match the previous q001 evaluation records.
- Gold is attached only after the generated answer has been saved. It is not
  supplied in the answer prompt and did not alter the frozen retrieval.
- This follow-up makes one answer-model call, zero retrieval-model calls, zero
  embedding calls, and zero scorer-LLM calls. The earlier retrieval's single
  embedding call remains accounted for in its separate report.

## Interpretation

The official scorer evaluates the generated answer and its cited event IDs; it
does not directly score the order or relevance of all 15 retrieved memories.
Consequently, irrelevant retrieved items do not themselves lower these metrics.
They can still cause wrong answers or bad citations on other questions. On this
run, both hotel facts are present and the model uses the correct pair.

No general improvement claim follows from one q001 answer. The automatic parser's
previously observed synthetic failure and remaining ranking noise are unchanged.
The shared production retrieval implementation is unchanged.

## Verification and usage

All eight official score records were reproduced locally from the saved answer.
The answer cites only event IDs present in its supplied evidence. The request
archive confirms one user message of 4,052 characters and the intended model,
temperature, and reasoning settings.

Answer plus scoring took 4.394 seconds, excluding preparation.
The provider reports 23,596 input tokens,
85 output tokens, and 23,681
total tokens, with zero reasoning tokens. The reported input count is unusually
large for the captured 4,052-character message. It is preserved as returned;
this experiment does not explain the provider's token accounting or infer a
monetary cost from it.

- [Generated answer, citations, official scores, and gold reference](validation-memderive-automatic-query-q001-answer-20261102/result.json)
- [Exact answer prompt](validation-memderive-automatic-query-q001-answer-20261102/answer-prompt.txt)
- [Frozen evidence and input manifest](validation-memderive-automatic-query-q001-answer-20261102/prepared.json)
- [Execution script](validation-memderive-automatic-query-q001-answer-20261102/run.py)
- [Request configuration](validation-memderive-automatic-query-q001-answer-20261102/model-config.json)
- [Attempt ledger and provider usage](validation-memderive-automatic-query-q001-answer-20261102/attempts.jsonl)
- [Verification](validation-memderive-automatic-query-q001-answer-20261102/verification.json)
- [Artifact hashes](validation-memderive-automatic-query-q001-answer-20261102/artifact-sha256.json)

The archive also preserves the credential-redacted raw response. No credential
file or upstream distribution is copied into the documentation archive.
