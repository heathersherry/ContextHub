# MemDerive q001: neighbor cosine threshold sweep

## Outcome

Changing only neighbor admission to raw query-vector cosine plus a threshold
preserves the correct answer and explicit conference-to-trip-to-hotel chain at
thresholds **0.40 and 0.45**. At **0.45**, the candidate pool falls from v2's
114 facts to 52. Official answer accuracy remains 1/1 and witness recall remains
0.5: this is a smaller successful candidate pool on one development question,
not an improvement in official scores or a calibrated general threshold.

Threshold 0.35 admits the canceled booking into the candidate pool, but the
unchanged downstream ranking still omits it from the final 15. Threshold 0.50
loses the conference-to-trip bridge, and the answer reports incomplete evidence.

Execution: **2026-11-02 10:50:34–10:50:58, Hong Kong time (UTC+8)**.
Five arms answered q001 once each, including a fresh answer to the frozen v2
selection. Every arm supplied exactly **15 evidence items**. This change remains
in research scripts; the shared ContextHub default retriever is unchanged.

## Isolated change

For each initial candidate, find facts sharing at least one existing entity tag.
Keep a neighbor if and only if:

```text
cosine(archived full-question embedding, neighbor embedding) >= threshold
```

Admission uses raw cosine, without keyword weighting, link-quality weighting,
novelty discounts, or the old six-neighbor cap. A seed can have zero retained
neighbors. The existing **36 initial candidates are retained independently of
this threshold**; this is not a threshold applied to the entire candidate pool.
The four thresholds, 0.35 / 0.40 / 0.45 / 0.50, were fixed before inspecting
these selections and answers. The CLI requires explicit threshold values; this
probe does not establish a default.

All downstream v2 behavior is preserved: group construction, connected query
coverage, bridge strength, marginal relevance and redundancy, greedy selection,
and the 15-item budget. The existing eligibility filters, cutoff separation,
month normalization, query lexemes, and archived full-question vector also stay
unchanged. In particular, the archived vector still embeds the cutoff prefix;
only lexical matching uses the previously separated content query.

Source comparisons verify the unchanged helper functions and selector methods.
The group-building statements differ only in the local neighbor score and keep
predicate. See [verification](validation-memderive-neighbor-threshold-q001-20261102/verification.json).

## Question and fixed inputs

> As of 2026-10-07 09:00, which hotel booking is currently active for the February conference trip?

Reuse the memory snapshot from `outputs/memderive-tripops-w1-q1-20261027-r1`,
through session 77: 324 eligible facts, their existing vectors/entity tags/source
mappings, the original union of 36 initial candidates, and the saved query
embedding. This does not test newer extraction prompts or repeat ingestion.

The control selection is frozen in
[v2 validation](validation-memderive-entity-group-q001-v2-20261030/offline.json).
Its evidence and official answer prompt are reconstructed exactly and freshly
answered alongside the threshold arms. Original baseline evidence/prompt and
the vector top 20 are also reproduced exactly as checks, without another call
to answer the original baseline.

## Results

Candidate counts include the 36 initial candidates. Selection times measure the
programmatic selection from an already loaded index, once per threshold; they
are not repeated performance benchmarks. Frozen v2 selection time was not
remeasured. Group counts include singletons, pairs, and connected triples.

| Arm | Candidate facts | Groups | Selection time | Explicit chain in final 15 | Answer accuracy | Witness recall |
|---|---:|---:|---:|---|---:|---:|
| Frozen v2 control | 114 | 624 | Not remeasured | Yes, ranks 1–3 | 1/1 | 0.50 |
| Cosine >= 0.35 | 141 | 3,300 | 8.329 s | No; city connection replaces trip bridge | 1/1 | 0.50 |
| Cosine >= 0.40 | 92 | 1,141 | 1.212 s | Yes, ranks 1–3 | 1/1 | 0.50 |
| Cosine >= 0.45 | 52 | 342 | 0.196 s | Yes, ranks 1–3 | 1/1 | 0.50 |
| Cosine >= 0.50 | 36 | 58 | 0.012 s | No | 0/1 | 0.00 |

The first four arms answer **Copperline Hotel in Velle, Trip-Anser, 655 CR**.
All cite `w1_e042` and `w1_e056`. Official witnessed accuracy and answerability
accuracy are 1/1, witness precision is 1.0, and witness F1 is 2/3 for these arms.
The minimal witness is `{w1_e055, w1_e056}`; the canceled-booking source is
missing, so recall is only 0.5. Passing witnessed accuracy does not establish
complete witness retrieval. At 0.50 the answer is `unanswerable_incomplete`,
with no witnesses; these six metrics are zero. Stale-belief and distractor-hit
rates are zero for every arm.

### Where the important facts go

Ranks below refer to the final evidence list, not the candidate pool. Cosines
are shown rounded; admission uses their unrounded values.

| Fact | Query cosine | v2 rank | 0.35 rank | 0.40 rank | 0.45 rank | 0.50 rank |
|---|---:|---:|---:|---:|---:|---:|
| Conference Anser now runs from 2026-02-02 through 2026-02-06. (`w1_e042`) | 0.643200 | 1 | 1 | 1 | 1 | 1 |
| Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. (`w1_e056`) | 0.397794 | 2 | 2 | 2 | 2 | Omitted |
| Trip-Anser was opened for Conference Anser. (`w1_e031`) | 0.457926 | 3 | Omitted | 3 | 3 | Omitted |
| The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge. (`w1_e055`) | 0.389032 | Omitted | Omitted | Omitted | Omitted | Omitted |

- **0.35:** Both the explicit trip bridge and cancellation enter the pool but
  lose in final ranking. Rank 3 instead says “Conference Anser takes place in
  Velle.” The unchanged second-step scoring favors that city-based combination
  (0.245674) over the explicit trip chain (0.241361). Entity co-occurrence does
  not itself prove the intended relationship. A larger pool does not guarantee
  selection of the stronger evidence chain.
- **0.40 / 0.45:** The explicit trip bridge passes the threshold. The hotel fact
  has a lower cosine but survives because it is an initial candidate. Both
  facts reach ranks 2–3 through the unchanged group scoring. The cancellation
  fails neighbor admission and is not an initial candidate.
- **0.50:** The bridge fails admission. The hotel remains in the initial pool
  but is then omitted by downstream selection. No new facts are added beyond
  the 36 seeds; some links between seeds remain, so the graph is not empty.

The retained neighbor count for the active hotel seed is respectively
6 / 12 / 5 / 3 / 0 for v2 / 0.35 / 0.40 / 0.45 / 0.50. These are observed counts,
not configured quotas. The cancellation at 0.35 isolates a remaining downstream
selection issue; this experiment deliberately does not change that logic.

## Models, cost, and validation

- Answer model: OpenLux `gpt-5.6-sol`, reasoning `none`, temperature 0, maximum
  completion 2,048 tokens, no seed; same official prompt/parser as the old run.
- Five distinct prompts, five successful fresh answer calls, no reused answers,
  and no retries. The five-arm answer/scoring loop took 14.883 seconds.
- Reported usage: **5,932 input + 526 output = 6,458 tokens**. Request durations
  ranged from 2.631 to 3.551 seconds. No price estimate is inferred.
- No ingestion, new embedding, selector-model, or scoring-model calls. Scoring
  uses the official deterministic program; gold is introduced after generation.
- **18 targeted tests passed with warnings treated as errors.** Tests include
  inclusive threshold behavior, raw negative cosine, empty neighbor sets,
  preserved seeds, and retaining more than six qualifying neighbors.
- All 40 official metric outputs reproduce exactly. Dry and live evidence and
  prompts match; every arm has 15 items; frozen input hashes match.

The temporary official evaluator source directory was missing required files.
It was restored from the pinned official archive into
`outputs/memderive-upstream-recovery-20261102/deriveval`. Although the transfer
lacked a ZIP central directory, libarchive recovered the source and presets,
and their complete tree hashes exactly match
[`upstream-lock.json`](../../integrations/memderive/upstream-lock.json).
No evaluator code was adapted. The
[recovery record](validation-memderive-neighbor-threshold-q001-20261102/upstream-recovery-verification.json)
preserves the transport limitation and verified hashes.

## Interpretation and limits

Among the tested successful arms retaining the explicit chain, 0.45 uses the
smallest pool and least observed selection time. It is a useful setting for
this q001 comparison, not evidence for a universal threshold. The same fixed
15-item answer budget keeps answer token usage broadly similar; the main
observed saving is in programmatic candidate/group processing.

This is one previously diagnosed development question, with one answer sample
per arm. It establishes neither generalization nor a benchmark-wide score gain.
The unchanged downstream ranking still loses the canceled booking even when it
is admitted, and a lower threshold can favor a weaker connection. No shared
retrieval policy is enabled by this experiment.

## Reproduction and artifacts

Run from the repository root with the archived baseline and verified official
source available. Use a new output directory; omit `--execute` for offline
selection only. For exact replay after further edits, use this validation
directory's frozen source files in a separate checkout.

```bash
PYTHONPATH=src:sdk/src:.:outputs/memderive-upstream-recovery-20261102/deriveval/src \
.venv/bin/python -m scripts.memory_research.entity_group_probe \
  --baseline outputs/memderive-tripops-w1-q1-20261027-r1 \
  --output outputs/memderive-neighbor-threshold-q001-replay \
  --query-embedding outputs/memderive-tripops-w1-q1-20261027-r1/q1/state/16b9691342b1428fb73073d2fcbf3d4c/responses/b885e2ef-df11-4b8c-8dcd-fd7bfe2fdea3-1.json \
  --query-lexemes outputs/memderive-entity-group-q001-v2-20261030-r1/query-lexemes.json \
  --control-v2 docs/memory-research/validation-memderive-entity-group-q001-v2-20261030 \
  --neighbor-thresholds 0.35 0.40 0.45 0.50 \
  --execute

PYTHONPATH=src:sdk/src:. .venv/bin/python -m pytest tests/test_memderive_entity_groups.py -q -W error
```

- [Summary and per-arm usage](validation-memderive-neighbor-threshold-q001-20261102/summary.json)
- [Full candidate lists, selection traces, and evidence](validation-memderive-neighbor-threshold-q001-20261102/offline.json)
- [Answers and official scores](validation-memderive-neighbor-threshold-q001-20261102/results.jsonl)
- [Frozen input manifest](validation-memderive-neighbor-threshold-q001-20261102/manifest.json)
- [Artifact and evaluator hashes](validation-memderive-neighbor-threshold-q001-20261102/artifact-hashes.json)
- [Raw run and response archives](../../outputs/memderive-neighbor-threshold-q001-20261102-r1)
