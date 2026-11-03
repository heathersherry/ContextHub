# MemDerive q001: programmatic entity-group probe

This is the preserved v1 result. Subsequent experiments cover
[v2 connected query contributions](MEMDERIVE-ENTITY-GROUP-Q001-V2-20261030.md)
and the current [neighbor-threshold sweep](MEMDERIVE-NEIGHBOR-THRESHOLD-Q001-20261102.md).
For exact v1 replay, use this report's frozen `entity_groups.py`,
`entity_group_probe.py`, and `selection_probe.py` in a separate checkout; the
invocation below records the historical v1 interface and input format.

## Outcome

The programmatic arm answered q001 correctly, while the freshly answered original
top 15 returned `unanswerable_incomplete`. Official answer accuracy and witnessed
accuracy both changed from 0/1 to 1/1. However, the program did **not** retain the
complete conference-date / conference-trip / hotel evidence chain. This is a
scoring success on one previously diagnosed development question, not evidence
that multi-hop retrieval is solved. The default ContextHub retriever is unchanged.

Execution: **2026-10-30, 17:17:05–17:17:11, UTC+8**. Only q001 was answered,
once per arm. There was no ingestion, selector-model call, new embedding call,
or LLM judge. The formula was fixed before the first offline result and was not
retuned after inspecting q001 selections or answers.

## Fixed inputs and protocol

- Reuse `outputs/memderive-tripops-w1-q1-20261027-r1`: its memory versions,
  exact source mappings, original candidates, and archived query embedding.
- Scope and session cutoff 77 match the original run. Choose the latest current
  version before filtering to fresh, accepted, non-invalidated facts with vectors.
  There are 324 eligible facts. The archived vector top 20 was reproduced exactly.
- The initial union has 36 distinct versioned refs from the original text,
  vector, entity and topic channels. The text/vector-only union has 35.
- Use the stored entity tags as an inverted index. No new entity extraction,
  aliases, typed relation model, inferred facts, or rewritten evidence.
- Tokenize the original query once with PostgreSQL `pg_catalog.english` using
  `tsvector_to_array(to_tsvector(...))`; use the stored `search_vector` lexemes
  for documents. The archived query input was checked against the exact question.
- Answer with the same OpenLux `gpt-5.6-sol` route, temperature 0,
  `reasoning_effort=none`, no seed, `max_completion_tokens=2048`. The provider
  reported `gpt-5.6-sol`. Use the unchanged official v9 prompt/parser and all
  eight deterministic provenance-v2 metrics. Each arm supplies **15** items.
- The original evidence and rendered answer prompt match the saved run exactly.
  The offline dry run and live preparation also produced identical evidence and
  prompts. This reuses the old memory snapshot; it does not test recent extractor
  or rule-candidate changes.

## First selection formula

For a group of one to three facts:

1. Compute weighted query-lexeme coverage across the union of their lexemes.
   Weights are `log((N+1)/(df+1)) + 1`; omit query terms absent from the corpus.
2. Compute cosine similarity between the saved query vector and the normalized
   sum of normalized fact vectors. This is arithmetic over existing vectors,
   not a newly embedded concatenation.
3. Combine 50% coverage and 50% cosine. For multi-fact groups, subtract
   `0.1 * (1 - mean_link_quality)` over their maximum-quality spanning tree.
   Link quality is the most specific shared entity's normalized inverse document
   frequency, divided by the geometric mean of endpoint entity counts. This
   lowers the weight of common entities and facts listing many entities.
4. Expand each initial seed once, retaining its six highest-scoring same-entity
   neighbors. Enumerate single seeds, retained pairs, and connected three-fact
   groups over those edges. One newly found bridge can connect two initial seeds.
5. Greedily select whole groups, subtracting 0.15 times the mean maximum cosine
   similarity of newly added members to already selected facts. Deduplicate refs
   and reject any group whose new members would exceed 15 slots. There is no
   subsequent individual reranking that strips out bridge members.

This produced **107 candidate facts and 651 groups**. The procedure has caps, but
enumerating all wedges can still grow quickly around a high-degree node; this
small replay does not establish production-scale cost. Entity co-occurrence is
only a candidate link and does not prove a semantic relationship.

An offline ablation used the same singleton coverage/cosine and redundancy
formula on the original 36 candidates, without expansion or multi-fact groups.
It did not retain the hotel booking. No answer-model call was made for this arm.
This separates simple singleton reweighting from the combined expansion/group
change, but does not isolate expansion and grouping from one another.

## q001 evidence and results

Question: “As of 2026-10-07 09:00, which hotel booking is currently active for the
February conference trip?”

| Evidence | Initial union | Expanded pool | Original 15 | No-expansion 15 | Program 15 |
| --- | --- | --- | --- | --- | --- |
| Conference Anser runs 2026-02-02 through 2026-02-06 | Yes | Yes | #7 | #10 | Absent |
| Trip-Anser was opened for Conference Anser | No | Yes | Absent | Absent | Absent |
| Copperline Hotel in Velle is booked for Trip-Anser at 655 CR | Yes | Yes | Absent | Absent | #8 |
| Velle Arcade Hotel reservation for Trip-Anser was canceled | No | No | Absent | Absent | Absent |

The bridge is neighbor #1 for the conference-date fact and #4 for the hotel fact,
so the expansion found it and generated the complete three-fact group. Its raw
group score was **0.42290**, ranking **125/651** before greedy redundancy penalties.
The selected first group scored **0.55334** and linked a Copperline invoice to
Conference Rukh through `Vera`. The final hotel-containing group also used that
invoice and Rukh fact, rather than the Conference Anser connection.

| Official metric | Original 15, fresh answer | Program 15, fresh answer |
| --- | ---: | ---: |
| Answer accuracy | 0/1 | 1/1 |
| Witnessed accuracy | 0/1 | 1/1 |
| Answerability accuracy | 0/1 | 1/1 |
| Witness precision | 0 | 1 |
| Witness recall | 0 | 0.5 |
| Witness F1 | 0 | 0.6667 |
| Stale belief rate | 0 | 0 |
| Distractor hit rate | 0 | 0 |

The baseline returned null. The program answer was:

```json
{"cost": 655, "hotel": "Copperline Hotel", "location": "Velle", "trip": "Trip-Anser"}
```

It cited only `w1_e056`. The official minimal witness is `{w1_e055, w1_e056}`.
The unchanged scorer accepts witnessed accuracy when the answer is correct and
both witness precision and recall reach **0.5**, explaining the pass with half
the minimal witness. This is event-ID scoring, not a semantic audit of the
reasoning chain. The answer's statement identifying Trip-Anser as the February
conference trip was not backed by the explicit connecting facts in its 15 items.
The zero distractor-hit metric also does not mean the retrieved set was clean:
it checks cited annotated distractors, not every irrelevant retrieved item.

## Remaining problems

The arithmetic is cheap enough for this snapshot, but this first ranking formula
does not reliably prefer useful connections:

- The query's cutoff date/time contributes rare numeric lexemes. Unrelated
  `09:00` and September/October records earn coverage, while `February` does not
  lexically match the correct fact's numeric `2026-02` dates.
- `Vera` appears in 40 eligible facts. Downweighting it did not prevent unrelated
  groups from earning high coverage by joining different trips through the person.
- The greedy score repeatedly credits a group's existing coverage even when only
  one weak new member is added. The similarity penalty does not measure the new
  member's incremental contribution to answering the question.
- Six neighbors per seed also exclude the canceled booking. A cap can still lose
  a necessary temporal-state fact, even when the surviving hotel fact is correct.

Before considering a default change, the next generic revision should separate
the query cutoff from content matching while retaining temporal eligibility,
normalize written and numeric dates, and score the additional query constraints
resolved by a connection rather than merely pooled words. Common-person-only
connections should receive much less influence. Those changes were **not** made
or tested in this run; a correct q001 answer is insufficient reason to tune to its
specific hotel, trip name, or gold witness IDs.

## Cost and validation

| Work | Observed cost |
| --- | ---: |
| Build in-memory index and normalized vector matrix from loaded rows | 0.0675 s |
| No-expansion singleton ablation | 0.0077 s |
| Entity expansion, grouping and selection | 0.1999 s |
| Original answer request | 3.5755 s; 1,295 input + 101 output tokens |
| Program answer request | 2.2451 s; 1,239 input + 91 output tokens |
| Total live usage | 2 successful requests; 2,534 input + 192 output tokens |
| Selector / new embedding / judge-model requests | 0 / 0 / 0 |

CPU times exclude snapshot file reads, the one read-only SQL tokenization, and
production retrieval/database costs. Request timings are single observations,
not evidence of a general speedup. No monetary total is inferred without verified
provider prices. Raw response archives and attempt records remain under the run
directory, with credentials redacted by the shared client.

Eight focused unit tests passed: latest-version/state/scope/cutoff filtering,
stored lexeme parsing, a synthetic two-entity bridge, whole-group budget and
deduplication, neighbor caps, the offline ablation, and entity-frequency penalties.
All 16 persisted official metric outputs were reproduced. Input hashes, baseline
evidence/prompt identity, dry/live selection identity, and all 15-item budgets were
checked. Ruff was unavailable in the virtual environment and was not run.

## Artifacts and reproduction

- [Frozen summary and diagnostics](validation-memderive-entity-group-q001-20261030/summary.json)
- [Candidate refs, selected groups, evidence and prompts](validation-memderive-entity-group-q001-20261030/offline.json)
- [Both answers and official scores](validation-memderive-entity-group-q001-20261030/results.jsonl)
- [Input manifest](validation-memderive-entity-group-q001-20261030/manifest.json)
- [Verification and source snapshot hashes](validation-memderive-entity-group-q001-20261030/verification.json)
- Implementation: `scripts/memory_research/entity_groups.py` and `entity_group_probe.py`.
- Raw run: `outputs/memderive-entity-group-q001-20261030-r1`.

The probe needs NumPy (already present in this experiment environment), the
project dependencies and the pinned external DerivEval checkout. From the repo
root, choose a new output directory; omitting `--execute` makes no model calls:

```bash
PYTHONPATH=src:sdk/src:.:/private/tmp/contexthub-deriveval-reference/src \
.venv/bin/python -m scripts.memory_research.entity_group_probe \
  --baseline outputs/memderive-tripops-w1-q1-20261027-r1 \
  --output outputs/memderive-entity-group-q001-replay \
  --query-embedding outputs/memderive-tripops-w1-q1-20261027-r1/q1/state/16b9691342b1428fb73073d2fcbf3d4c/responses/b885e2ef-df11-4b8c-8dcd-fd7bfe2fdea3-1.json \
  --query-lexemes docs/memory-research/validation-memderive-entity-group-q001-20261030/query-lexemes.json
```

Adding `--execute` makes exactly two answer requests through the local provider
configuration. This probe is not a complete MemDerive or MEME experiment and is
not entered in the MEME-32k 100-case index.
