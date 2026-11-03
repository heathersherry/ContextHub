# MemDerive candidate selection: paired development probe

Status: completed. 20 fixed development questions from `tripops_w1`; not a full-world or full-benchmark result.

Finished: 2026-10-30T16:58:54.790887+08:00 (UTC+8). Total elapsed: 18.40 minutes.

Started: 2026-10-30T16:40:30.929797+08:00 (UTC+8).

**Decision:** keep this as an experiment; the evidence does not yet justify
enabling an unconditional selector. The only answer/witnessed-accuracy gain was
the previously investigated hotel question q001; the other 19 questions had
unchanged correctness. The gain is one question, not a broad validation of the
approach. Answerability accuracy and citation F1 improved, while citation
precision decreased slightly. The freshly rerun control itself differed from
the archived control by one correct answer, illustrating single-run variability.

The selector added 43.67 seconds per question on average (median 30.83), including
three timed-out attempts and their retries. Its successful calls reported
55,603 input and 94,109 output tokens; 93,049 output tokens were reasoning tokens
(98.9% of output). Usage for the three timed-out attempts is unknown. The answer
stage averaged 5.32 seconds in the control and 6.20 seconds in the treatment;
selection plus answering therefore averaged 49.87 seconds in this replay.
These timings exclude original memory construction and retrieval.

Tracked evidence: [summary](validation-memderive-selection-probe-20261030/summary.json),
[frozen manifest](validation-memderive-selection-probe-20261030/manifest.json),
[validation checks](validation-memderive-selection-probe-20261030/validation.json).

## Controlled comparison

- Reused the 2026-10-27 final memory snapshot, embeddings, and logged retrieval candidates. No database connection or writes.
- Selected 20 questions at evenly spaced official positions, including q001, before inference. No outcome-based selection.
- Control: original top 15, freshly answered. Treatment: union of saved keyword/vector candidates plus the original supplemental entity/topic hits, one DeepSeek selection, freshly answered.
- Both arms return exactly 15 unmodified, source-attributed memory items. No graph expansion, query rewriting, or ingestion changes.
- Selector: DeepSeek official `deepseek-flash`, reasoning `low`, no explicit temperature. Returns existing IDs only. One selection output attempt; shared transport retries retained.
- Answer: OpenLux `gpt-5.6-sol`, reasoning `none`, temperature 0, max completion tokens 2048. Official v9 prompt and parser unchanged. Shared recorded transport sends the corresponding official wire parameters.
- Scoring: all eight official deterministic provenance-v2 metrics. Gold is used only after answer generation, never by the selector or answer prompt.
- Answer-call order alternates between arms by question. No prompt tuning during this run.

## Scores

| Metric | Archived same sample | Fresh control | Selected candidates |
| --- | ---: | ---: | ---: |
| answer_accuracy | 35.00% | 40.00% | 45.00% |
| witnessed_accuracy | 30.00% | 35.00% | 40.00% |
| answerability_accuracy | 40.00% | 45.00% | 60.00% |
| witness_precision | 57.50% | 67.50% | 67.08% |
| witness_recall | 34.46% | 38.35% | 41.71% |
| witness_f1 | 33.41% | 38.29% | 42.15% |
| stale_belief_rate | 5.00% | 5.00% | 5.00% |
| distractor_hit_rate | 0.00% | 0.00% | 0.00% |

Every aggregate has 20 question observations. Citation precision/recall/F1 are query macro means. Witnessed accuracy requires a correct answer and precision and recall each >= 0.5; it does not require all witnesses.

## Paired changes

- answer_accuracy: improved 1 (q001); regressed 0 (none); unchanged 19.
- witnessed_accuracy: improved 1 (q001); regressed 0 (none); unchanged 19.

## Question families

| Family | Questions | Control answer | Selected answer | Control witnessed | Selected witnessed |
| --- | ---: | ---: | ---: | ---: | ---: |
| temporal | 3 | 66.67% | 100.00% | 66.67% | 100.00% |
| semantic | 4 | 0.00% | 0.00% | 0.00% | 0.00% |
| closure | 3 | 100.00% | 100.00% | 100.00% | 100.00% |
| rules | 4 | 25.00% | 25.00% | 25.00% | 25.00% |
| set_quant | 6 | 33.33% | 33.33% | 16.67% | 16.67% |

## Usage and latency

| Stage | Requests | Input tokens | Output tokens | Mean seconds | Median seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| selection | 23 | 55,603 | 94,109 | 43.67 | 30.83 |
| baseline | 20 | 24,877 | 1,821 | 5.32 | 4.97 |
| selected | 20 | 24,396 | 1,845 | 6.20 | 6.06 |

No dollar estimate is claimed without verified provider prices. Selection adds a sequential model call. Provider-reported reasoning tokens are included within output tokens.

## Interpretation limits

- This tests the combined candidate-retention-plus-selection change, not the independent contribution of either component.
- The snapshot predates the expanded rule gate and factual-retention prompt. Missing rules and omitted source facts cannot be restored by selection.
- One response per question and arm, one development world, and only 20 questions: a directional probe, not a general quality claim or evidence for changing the default.
- Entity linking and multi-hop supplementation remain unimplemented. Selection can only choose existing candidates.
- Reasoning is requested off for answers; provider-reported reasoning usage is retained in summary.json and is not assumed to be zero.

## Validation and artifacts

- Seven focused tests passed for union preservation, gold exclusion, original-text selection, budget, duplicate IDs and unknown IDs.
- An additional existing official-judge prompt-loading test passed with the correct local MEME source path; its database-dependent companion was skipped because no validation database was configured.
- All 20 baseline evidence lists and answer prompts matched their archived counterparts exactly before API calls. Final checks verify frozen files, 15-item budgets, candidate membership and unmodified memory text.
- Re-scoring the archived responses reproduced all 160 official metric values exactly (20 questions x 8 metrics).
- Raw artifacts: `/Users/sherrylin/Documents/PythonProjects/ContextHub/outputs/memderive-selection-probe-20261030-r1` (`manifest.json`, `prepared.jsonl`, `model-config.json`, `attempts.jsonl`, `responses/`, `selections.jsonl`, `results.jsonl`, `summary.json`).
- This MemDerive small-sample probe is not entered in the MEME-32k 100-case index.

## Additional diagnostics

Mean and median times above are per-question stage durations including transport retries and backoff. Token totals include only reported usage; failed attempts with missing usage are not assumed free.

- selection: 3 failed attempts; 3 attempts with missing/inconsistent usage; 20 calls reported reasoning tokens (93,049 tokens).
- baseline: 0 failed attempts; 0 attempts with missing/inconsistent usage; 0 calls reported reasoning tokens (0 tokens).
- selected: 0 failed attempts; 0 attempts with missing/inconsistent usage; 0 calls reported reasoning tokens (0 tokens).

q001 answers and citations:

- baseline: answer=null; witness IDs=[]; witness precision=0.000; recall=0.000.
- selected: answer="Copperline Hotel in Velle, booked for Trip-Anser at 655 CR for the stay."; witness IDs=['w1_e042', 'w1_e056']; witness precision=1.000; recall=0.500.

## Reproduction

Use the pinned DerivEval source on `PYTHONPATH`, the existing baseline directory, and a new output directory. Provider credentials are read from the local provider configuration and are never copied into the result files.

```bash
PYTHONPATH=src:sdk/src:.:/private/tmp/contexthub-deriveval-reference/src .venv/bin/python -m scripts.memory_research.selection_probe \
  --baseline outputs/memderive-tripops-w1-q1-20261027-r1 \
  --output outputs/memderive-selection-probe-new-run --count 20 --execute
```

Omit `--execute` for an offline preparation/reconstruction check. Each invocation requires a new output directory. The probe is experimental and is not wired into the shared engine or either benchmark adapter.
