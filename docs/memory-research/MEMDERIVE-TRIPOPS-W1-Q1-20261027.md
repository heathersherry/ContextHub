# MemDerive tripops_w1: first real Q1 run

Status: **completed**. This is one development world, not a ten-world result.

Code: `cdeba41503d0ef299caf883800817db010dce73b` on `codex/memderive-adapter`, based on `7df675667`. Source SHA-256: `44851d0eaacd78b334802ec6f9b94615664778683a4198cae191c5929ec0eec6`.

Started: 2026-10-27T17:11:03.367685+08:00; finished: 2026-10-27T18:35:11.464318+08:00. Elapsed: 84.13 minutes (59.29 minutes through ingestion, then 24.84 minutes for retrieval, answers, scoring and finalization).

## Protocol

- `formal-set-v1.16`, Q1, all 140 final-state questions; per-session ingestion (77 sessions, 452 events). Q2/Q3 are not part of this run.
- Shared extraction adds message-source numbers; direct writes, duplicates and derived premises preserve provenance. No benchmark-specific memory reasoning policy was added.
- Memory: DeepSeek official `deepseek-flash`, reasoning `low`; TypeSafe `jev-1.13.0` for all three typed decision backends.
- Embedding: Aliyun `qwen3.7-text-embedding`, 1536 dimensions; existing MEME retrieval settings; answer retrieval top_k=15.
- Answer: OpenLux `gpt-5.6-sol`, reasoning `none`, temperature 0; official v9 prompt.
- Scoring: official deterministic provenance-v2 metrics; no model judge.
- Fresh database and namespace, no reuse; the original research worktree was not modified.
- 433 offline tests passed before the run, including shared-memory/MEME regression and official Q1/Q2/Q3 wiring.

## Completeness

```json
{
  "sessions_completed": 77,
  "sessions_failed": 0,
  "events_completed": 452,
  "event_sequence_matches_public_data": true,
  "answer_rows": 140,
  "unique_questions": 140,
  "all_answers_completed": true,
  "eight_scores_per_answer": true,
  "source_coordinates_in_range": true,
  "max_retrieved_items": 15,
  "fixture_answers": 0,
  "gold_never_ingested": "only the public chronicle is passed to the shared engine; checked by adapter tests",
  "source_unchanged_after_run": true,
  "upstream_unchanged_after_run": true,
  "all_retrievals_after_final_ingestion": true,
  "retrieval_sequences": [
    77
  ]
}
```

## Official scores

| Metric | Value | Denominator |
| --- | ---: | ---: |
| Answer accuracy | 29.29% | 140 questions |
| Witnessed accuracy | 17.86% | 140 questions |
| Answerability accuracy | 37.86% | 140 questions |
| Witness precision (query macro mean) | 61.96% | 140 questions |
| Witness recall (query macro mean) | 32.87% | 140 questions |
| Witness F1 (query macro mean) | 33.50% | 140 questions |
| Stale belief rate | 4.29% | 140 questions |
| Distractor hit rate | 2.14% | 140 questions |

Precision, recall and F1 are means of per-question values, not pooled citation counts. Witnessed accuracy requires a correct answer, witness precision >= 0.5 and witness recall >= 0.5; it does not require complete citation coverage. No scores are combined with MEME.

## Scores by question family

| Family | Questions | Answer correct | Answer + support thresholds |
| --- | ---: | ---: | ---: |
| temporal | 19 | 14 (73.68%) | 13 (68.42%) |
| closure | 18 | 7 (38.89%) | 4 (22.22%) |
| semantic | 30 | 5 (16.67%) | 1 (3.33%) |
| rules | 28 | 2 (7.14%) | 2 (7.14%) |
| set_quant | 45 | 13 (28.89%) | 5 (11.11%) |

## Usage

| Model / stage | Requests | Known input tokens | Known output tokens | Missing input / output usage |
| --- | ---: | ---: | ---: | ---: |
| deepseek-flash | 491 | 1,194,457 | 500,811 | 0 / 0 |
| qwen3.7-text-embedding | 1294 | 40,583 | Not reported | 0 / 1294 |
| jev-1.13.0 | 77 | 87,232 | 16,799 | 0 / 0 |
| gpt-5.6-sol / official answers | 140 | 1,558,921 | 27,687 | 0 / 0 |

All 2,002 benchmark requests succeeded; there was no transport retry. The separate routing probe adds one request (4,390 input / 5 output tokens). Provider prices were not supplied; known usage is reported without inventing a bill. Route preflight is separate from benchmark usage. Native clients are not included in official API-window totals.

A separate one-call answer-route preflight is recorded in `answer-route-preflight.json`. Cached and reasoning token details, attempts and failure counts are retained in `analysis.json` and the original ledgers. Reasoning tokens are a subset of output tokens, not an additional total.

Although the request specifies reasoning_effort=none and temperature=0, 87 answer requests report nonzero reasoning usage (14704 tokens). An offline HTTP serialization check confirms that the pinned official client sends the configured parameters; see `answer-request-serialization-check.json`. Provider-side disabling of reasoning is not independently verified.

## Verified initial diagnostics

The final snapshot contains 338 fresh facts and 70 stale fact versions, with zero rules. These observations identify bottlenecks; they do not attribute all wrong answers or quantify the gain from a future fix.

1. **Rule intake is too narrow for this source.** The candidate gate in `src/contexthub/memory/jev_candidate_gate.py:10` requires a whole-word `if`. None of the 452 public events contains that word, so no rule candidates reach extraction. The 77 Jev calls all executed `p2_intent`; the configured rule gate and P1 did not make calls. Rule-family answer accuracy was 2/28.
2. **The extractor drops fictional world knowledge.** In session 1, only messages 16 and 17 produced extracted items. Messages 1–15, which establish geography and country membership, were omitted. The instruction in `src/contexthub/memory/prompts/extractor.txt:17` to omit general knowledge unless explicitly retained is consistent with this behavior. Here these facts are necessary supplied world knowledge, not information the answerer may assume from outside the log.
3. **Stored evidence can miss retrieval.** For `q001`, the fresh booking fact `2d39ca73-6eb7-4ea3-b358-69ec7b0be039:1`, sourced from `w1_e056`, states that Copperline Hotel is booked for Trip-Anser. It was absent from the retrieved top 15. The answerer saw the invoice and conference dates and returned `unanswerable_incomplete` instead of the hotel.

The next useful investigation is extraction coverage and retrieval coverage on this development world. Keep this baseline frozen; any change to shared prompts or rule intake should be evaluated on both MEME and MemDerive before merging. Q2/Q3 model experiments and a combined-branch real MEME run remain unperformed.

## Interpretation

This run measures the current ContextHub configuration, not an exact reproduction of the paper's construction-model/embedding condition. Any semantic extraction or retrieval limitation remains part of this baseline; no prompts were tuned during the run. One development world cannot establish general benchmark performance.

## Saved configuration and artifacts

- [Tracked result and diagnostics](validation-memderive-tripops-w1-q1-20261027/summary.json)
- [Frozen official run configuration](validation-memderive-tripops-w1-q1-20261027/q1.json)
- [Source and upstream identities](validation-memderive-tripops-w1-q1-20261027/provenance.json)
- [Offline answer-request serialization check](validation-memderive-tripops-w1-q1-20261027/answer-request-serialization-check.json)
- [Local raw run report](../../outputs/memderive-tripops-w1-q1-20261027-r1/report.md)
- [Setup and protocol](../../integrations/memderive/README.md)

Complete raw outputs, all per-question scores, native/API ledgers, memory snapshots and process traces remain in `outputs/memderive-tripops-w1-q1-20261027-r1/` in the adapter worktree. That directory is ignored by Git. The retained experiment database is `contexthub_memderive_tripops_w1_20261027_r1`. Preserve these artifacts for follow-up; the tracked summary does not contain the full raw run.

This one-world development run is not entered in the MEME-32k 100-case index. No merge into `codex/memory-research` was performed. The run used the frozen baseline plus the adapter and message-provenance extension, not the original worktree's ongoing prompt edits.
