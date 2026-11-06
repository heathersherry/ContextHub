# HyDRA-Bench: full 20-case run with per-type retrieval and the new model

Follow-up: the [original-meaning semantic review](2026-11-04-SEMANTIC-REVIEW.md)
qualifies both mismatches and some matching answers. The 13/20 below remains
the historical core reference-agreement count, not unconditional correctness.

On local review against the released references, **13/20 answers match the core
reference (65%)**: 6/10 A2S and 7/10 S2A. One additional answer is partial, five
final answers differ from the reference, and one episode produces no final answer
after exhausting its per-call generation budget. All 20 episodes were executed.

Every episode eventually retrieved all tables used by its reference SQL: 42/42
question-table occurrences across 20/20 cases. All 56 SQL calls executed without
SQL errors. The five reference mismatches are primarily SQL interpretation or
filter-scope differences, not failure to retrieve the necessary table schemas.
Several reference questions do not specify their intended aggregation clearly.

This is a coding-assistant review of the core answers and actual tool evidence,
with all reference SQL replayed read-only. It is not the paper's o4-mini judge,
an independent human evaluation, or a full HyDRA-Bench accuracy claim. Partial
and absent answers are excluded from the 13 matches; extra prose in matching
answers has not been exhaustively fact-checked.

## Runtime and the required configuration correction

- Model: **qwen3.8-2.4t-a95b**, as newly configured by the user.
- Retrieval: table-schema-only requests use pure vector scores; resource requests
  retain BM25 keyword reranking. `RERANK_STRATEGY=by_type` implements this policy.
  Mixed-type/unfiltered requests retain one common keyword ranking scale; the
  agent wrapper always requests one explicit type.
- ContextHub API: localhost port 18085, reusing isolated database
  `hydra_retrieval_20261104`. Original keyword and vector-only services remain
  separate. No benchmark rows were moved out of SQLite.
- Unchanged inputs: same 20 cases, exact system prompt, original question-only
  agent input, 75 schemas, 18,265 text passages, stored vectors, embedding model
  `text-embedding-v4`, 1,536 dimensions, top three results, nine initial candidates,
  ten searches per type, read-only bound SQLite, and search-gated table access.
- Limits: temperature 0, 4,096 output tokens per model call, 120-second model-call
  timeout, 600-second episode deadline, graph recursion limit 80, concurrency two.

The initial invocation retained the previous model's `enable_thinking=False`.
The new provider rejected all 20 initial requests with HTTP 400, stating that
this model requires `enable_thinking=True`. None made a tool call or returned an
answer. Those original traces are preserved. Thinking was then enabled for all
20 cases, with a corrected policy saved before any successful model execution.
No prompt, question, retrieval parameter, source, or output-token limit changed.

The thinking-enabled run is the evaluated run. No completed wrong or partial
answer was rerun. The no-answer case was not selectively given a larger budget.
Because the model, its mandatory thinking mode, and retrieval policy differ from
earlier agent runs, final-answer differences cannot be attributed solely to the
retrieval change.

## Results by question

| Case | Type | Outcome | Actual core answer / observation |
| --- | --- | --- | --- |
| dev_43 | A2S | Match | 1999 Japanese Grand Prix date and JDDJ reconciliation significance |
| dev_62 | A2S | Reference mismatch | Ferrari by total race points; reference Mercedes by highest single standing |
| dev_5 | S2A | Match | Silverstone: 52.0786, −1.01694 |
| dev_13 | S2A | Reference mismatch | Eddie Irvine: 0.25 race points on average; reference 2.27 standing points |
| dev_35 | A2S | No final answer | Correct schemas and Cosmic Entity SQL result obtained; reasoning consumes final call budget |
| dev_72 | A2S | Match | DC Comics; Brightest Day follows Blackest Night and its aftermath |
| dev_10 | S2A | Match, SQL-only path | 351 kg; no text search occurred |
| dev_68 | S2A | Match | Helen Parr; all seven reference powers |
| dev_26 | A2S | Match | Correct post; greater statistical power while controlling family-wise error |
| dev_31 | A2S | Match | Newcastle, UK; parliamentary democracy under constitutional monarchy |
| dev_6 | S2A | Reference mismatch | 45 India-related users; reference 18 exact-location users; horse evidence also missing |
| dev_20 | S2A | Reference mismatch | 295 New York substring matches; reference 15 exact-location users |
| dev_32 | A2S | Partial | Junior card and spending-management explanation; junior/prepaid mapping unproven |
| dev_46 | A2S | Reference mismatch | Brno-City's largest individual active loan and city history; reference regional loan sum and South Moravia history |
| dev_42 | S2A | Match | Sokolov; 8 female customers |
| dev_60 | S2A | Match | Prague; earliest account ID 1539 |
| dev_55 | A2S | Match | Amy Firth is Peter Firth's daughter |
| dev_66 | A2S | Match | New Jersey; Newark, supported by an alternative passage |
| dev_0 | S2A | Match | Laugh Out Loud |
| dev_11 | S2A | Match | Vice President role → Communication Studies, within the historical benchmark reference |

## Attribution of the seven non-matching outcomes

### Five SQL/reference mismatches

**dev_62 — aggregation interpretation.** The agent retrieved both relevant
schemas, computed the sum of `constructorResults.points` (Ferrari, 7,081.5), and
also computed maximum `constructorStandings.points` (Mercedes, 765). It chose
the former and searched Ferrari's name origin. The reference chooses the latter.
Unlike the earlier model's sum of repeated standing snapshots, this run sums
race-result points, a defensible interpretation of total constructor points.
The question does not specify total versus highest individual record. This is
a reference mismatch with ambiguity, not a table-retrieval failure. The later
Ferrari text search follows that SQL decision.

**dev_13 — different points measure.** The text correctly identifies Eddie
Irvine, and `drivers`, `races`, and `driverStandings` were all retrieved. The
agent averages `results.points`, obtaining 4/16 = 0.25. The reference averages
`driverStandings.points`, obtaining 2.2666667. Average race points is a plausible
reading of the natural-language question, but it is not the reference measure.
The original BIRD description for standing points is also not sufficiently clear
to resolve this distinction automatically.

**dev_6 — SQL location scope plus a separate corpus gap.** The reference counts
Teacher badges for `Location = 'India'`, yielding 18. The agent includes
case-insensitive India location variants such as city-plus-country entries and
counts distinct users, yielding 45. It excludes an Indianapolis false match that
would otherwise produce 46. The broader country interpretation is plausible,
but not reference-equivalent. Independently, ten resource searches did not find
direct evidence that the Indian Half-bred horse is primarily found in India; an
eleventh search attempt was blocked by the budget. The agent openly proceeded
from the name despite the evidence requirement. Since it still chose the same
country as the reference, the numerical discrepancy is caused by its SQL filter,
not by selecting the wrong country. Both issues are retained in the attribution.

**dev_20 — overly broad location match.** The text evidence correctly points
to New York. The agent uses `Location LIKE '%New York%'`, obtaining 295; the
reference uses `Location = 'New York'`, obtaining 15. The agent inspected the
location variants and saw the exact-location count. It also acknowledged that
its broader result includes a Hudson Valley/state-level location but retained
295. This combines reference-filter divergence with an actual city/state
conflation, not missing text or missing schema.

**dev_46 — aggregation and geographic level.** The agent selects the largest
individual active loan, 590,820, in Brno-mesto, and searches Brno history. The
reference groups active loans by `district.A3` region and orders their sums,
selecting south Moravia. The answer mentions that Brno is in south Moravia, but
its requested historical explanation concerns the city rather than the region
and does not cover the reference's central regional history. All three required
tables were retrieved. The wording says district and highest active loan without
specifying regional aggregation, so the reference's intended grain is ambiguous.

### One partial answer with an evidence-mapping gap

**dev_32 — junior card versus prepaid card.** SQL correctly returns `junior`.
ContextHub also returns the known prepaid/teen-card passage `17182301/0064`, so
this is not a failure to retrieve that passage. The answer explains fund-limited
spending management, but uses junior/classic/gold tier distinctions instead of
the reference's no-borrowing/no-interest/fees features. It explicitly notes that
junior-specific evidence is absent. Neither the table labels nor the retrieved
paragraph proves that this database's junior category is a prepaid card; the
reference assumes that mapping. The answer also infers young/entry-level users
from the tier label. This is recorded as partial, not a complete reference match
and not a pure ranker failure. The original pre-run support labels had already
flagged this mapping limitation.

### One generation-budget failure

**dev_35 — no final answer.** All three reference tables were retrieved. SQL
repeatedly returned Cosmic Entity, the reference entity, and correctly found
279 maximum-valued attribute rows across 117 heroes. The reference has no
secondary tie-break, so choosing a unique race is underdetermined. The agent
continued exploring human/root-race interpretations and did not query the
available Cosmic Entity text directly. Its final model call ended with
`finish_reason = length`: usage metadata reports 4,096 output tokens, all 4,096
as reasoning, and an empty visible answer. This is not the episode timeout,
graph limit, search limit, or an unavailable schema. It is a generation-budget
failure, with tie ambiguity and misdirected text queries as contributing factors.
No larger-budget rerun is mixed into the score.

## What this says about retrieval and grounding

- First schema search: 33/42 reference-table occurrences, with complete table
  sets in 13/20 questions. After iterative searches: 42/42 and 20/20. This means
  the agent often needed query refinement; it does not make first-pass ranking
  perfect or prove schema understanding.
- Actual policy audit: all **40 table searches** match the initial vector order;
  all **42 successful text searches** match keyword reranking. The eleventh
  attempted text search in `dev_6` returned a budget error without a backend
  search. The audit uses the original stored candidates, not new model calls.
- Fourteen episodes hit a frozen reference text anchor. `dev_66` retrieved a
  different valid paragraph, `67234/0021`, explicitly calling Newark the largest
  city, bringing known reference-branch support to fifteen episodes. Missing
  anchors are not automatically retrieval failures: some agents searched for
  entities selected by a different SQL interpretation, and one skipped text.
- `dev_10` correctly answers 351 kg using identity and weight fields directly
  from SQLite, with no resource call. It counts as an answer match under the
  reference-based metric, but does not demonstrate S2A text retrieval. Thus
  **12/20** both match the core reference and retrieve supporting text as well
  as database facts. This is a separate grounding observation, not a replacement
  score or a claim that every extra sentence is supported.
- Corpus gaps remain relevant in `dev_6` and the `dev_32` category mapping.
  They should not be conflated with ranking an available, sufficient answer
  passage below the cutoff. No text was added after observing this run.

For this run, none of the non-matching outcomes can be explained simply by
failure to retrieve a required reference table. SQL meaning, location-value
matching, evidence sufficiency, and the new model's reasoning budget are the
main remaining issues. That conclusion is specific to this fixture and does
not establish performance on larger catalogs or the full benchmark corpus.

## Validation, preservation, and limits

All SQLite file hashes match before/after each episode. Index content, vectors,
versions, visibility settings, and feedback fingerprints are unchanged across
the evaluated run. Background propagation and lifecycle workers remain off.
All 56 SQL statements execute without SQL errors; syntactic executability is
distinct from matching the intended statistic. The focused suite passed **82
tests** covering retrieval policy, read-only SQL, enforced budgets, annotation
isolation, and the Deep Agents adapter.

The same non-random 20 questions and case-informed source subset are retained.
There is one evaluated attempt per question with temperature 0. The frozen
question release and KILT snapshot, original BIRD descriptions, local scoring
method, and policy differences from the paper remain as previously documented.
Historical corpus statements such as the Afghanistan political role are judged
within that frozen reference, not asserted as current facts.

The 13/20 figure measures core agreement with the published references under
the stated review rule. It should not erase ambiguities in the five differing
SQL interpretations or imply the reference is always the only sensible reading.
The junior-card partial decision is explicitly reviewable in the saved answer
and evidence. No result was removed to improve the count.

## Artifacts and replay

- [Structured evaluation and explanations for every case](artifacts/20261104-mixed-qwen/evaluation.json)
- [All evaluated agent traces](runs/20261104-mixed-qwen-thinking-agents/)
- [Initial parameter-rejection traces](runs/20261104-mixed-qwen-agents/)
- [Tool queries, SQL results, coverage and finish reasons](artifacts/20261104-mixed-qwen/analysis.json)
- [Read-only reference SQL replay](artifacts/20261104-mixed-qwen/reference-sql-audit.json)
- [Evaluation policy fixed before initial attempts](artifacts/20261104-mixed-qwen/evaluation-policy.json)
- [Corrected policy before successful model execution](artifacts/20261104-mixed-qwen/evaluation-policy-corrected.json)
- [Before evaluated run: configuration, hashes, index fingerprints](artifacts/20261104-mixed-qwen/before-thinking.json)
- [After evaluated run](artifacts/20261104-mixed-qwen/after.json)
- [Live policy checks before agent execution](artifacts/20261104-mixed-qwen/policy-check.json)
- [Policy audit for all 82 successful searches](artifacts/20261104-mixed-qwen/policy-audit.json)
- [Trace summary script](artifacts/20261104-mixed-qwen/analyze.py)
- [Earlier fixed-query vector comparison](2026-11-04-VECTOR-ONLY.md)
- [Corpus and protocol details](2026-11-04-EXPANDED-RETRIEVAL.md)

With the isolated database and credentials supplied through the environment,
start a service with `RERANK_STRATEGY=by_type`. For this model, explicitly set
`CHAT_ENABLE_THINKING=true` for the agent runner. All new runs require a fresh
output directory so the existing records are preserved:

```bash
RERANK_STRATEGY=by_type CHAT_ENABLE_THINKING=true \
python -m integrations.hydrabench.run \
  --dev data/hydrabench/dev.json \
  --database-root data/hydrabench-expanded/databases \
  --cases dev_43 dev_62 dev_5 dev_13 dev_35 dev_72 dev_10 dev_68 dev_26 dev_31 \
          dev_6 dev_20 dev_32 dev_46 dev_42 dev_60 dev_55 dev_66 dev_0 dev_11 \
  --url http://127.0.0.1:18085 --concurrency 2 \
  --output docs/hydrabench/runs/NEW_MIXED_AGENT_RUN
```

The trace summary can be recomputed without model calls using the saved
`analyze.py` script with `--cases`, `--labels`, `--runs`, and `--output`. Its
automatic coverage metrics are separate from the explicit semantic judgments
in `evaluation.json`.
