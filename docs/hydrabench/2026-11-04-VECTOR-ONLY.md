# HyDRA-Bench: keyword reranking versus pure vector retrieval

Disabling keyword reranking substantially improves table discovery on this fixed
20-question subset, but slightly worsens text evidence retrieval. No answering
agent or SQL was run in this comparison.

## Controlled comparison

The baseline is the preserved 20-question probe run from the expanded experiment.
The new run uses the same questions, exact query strings, reference table sets,
frozen text labels, 75 schemas, 18,265 passages, index vectors, account, permissions,
and `text-embedding-v4` model with 1,536 dimensions. Each query retrieves nine
candidates and returns three. There is still no hard database filter.

Only the keyword-reranking step is disabled. The application previously declared
`rerank_strategy` but always constructed the keyword router at startup. The setting
is now wired into startup and accepts `keyword` or `none`. The existing default
remains `keyword`; this experiment service explicitly uses `RERANK_STRATEGY=none`.
When disabled, retrieval keeps cosine similarity as the score. ACL, validity,
quality-factor, and other search policies remain in place. All fixture records
are fresh, have zero adoption/ignore counts, and have no long-document paths,
so none of those policies changes the vector ordering in this experiment.

The service reuses the existing isolated database `hydra_retrieval_20261104` and
listens on localhost port 18084. The original keyword service on port 18083 is
separate. No corpus was imported again and no stored embeddings were recomputed.
The user's newly configured chat model, `qwen3.8-2.4t-a95b`, was not called;
only the unchanged embedding model was needed for the 40 query embeddings.

Controls were checked against the actual stored retrieval traces:

- All 40 new queries have exactly the same nine candidate IDs, in the same order,
  as their baseline queries.
- All 40 new result lists equal the first three candidates of those original
  lists. Thus this is a paired reranking comparison, without candidate drift.
- Before/after database fingerprints of content, vectors, versions, validity,
  scope, and owner match. Feedback counts remain zero. Search access counters and
  audit logs naturally update; they do not affect these rankings.
- Every request returned successfully. The focused retrieval suite passed
  **34 tests**, including both enabled/disabled modes with conflicting keyword
  and semantic relevance. The original keyword behavior is retained by default.

## Results

| Metric | Keyword reranking | Pure vector | Change |
| --- | ---: | ---: | ---: |
| Required table occurrences in top 3 | 27/42 (64.3%) | 35/42 (83.3%) | +8; +19.0 percentage points |
| Questions with every reference table in top 3 | 7/20 (35%) | 14/20 (70%) | +7; +35 percentage points |
| Questions with any reference table in top 3 | 19/20 | 19/20 | unchanged count, different failing case |
| Table hits from the wrong database, out of 60 result slots | 4 | 0 | −4 |
| Frozen text-anchor hit in top 3, among 19 covered questions | 18/19 (94.7%) | 16/19 (84.2%) | −2; −10.5 percentage points |
| Frozen text-anchor hit at rank 1, among 19 covered questions | 10/19 | 11/19 | +1 |
| Known core text support, including inspected alternative passages | 18/19 (94.7%) | 17/19 (89.5%) | −1; −5.3 percentage points |

The 42 required tables are question-table occurrences, not 42 unique tables.
Table labels are the tables accessed by the released reference SQL; this does
not establish whether a different valid SQL path exists. Ten questions gained
required-table hits, two lost hits, and eight retained the same hit count.
Equal hit counts do not imply identical result sets.

The frozen text-anchor metric is the unchanged, automatically reproducible
metric. The last row is a separately disclosed local evidence inspection:
`dev_31` retrieves sufficient alternative passages despite missing its original
anchor. The labels were not expanded or changed, and neither mode was rerun
after inspecting results. This is not an official semantic judge or independent
human evaluation, and it does not claim support for every detail of a reference
answer. Existing qualifications in the frozen labels still apply, including the
`junior` credit-card mapping and inferences from exhibition/history passages.

`dev_6` still has no located direct evidence in the frozen subset and is excluded
from the 19-question coverage denominator. Across all 20 selected questions,
the frozen text-anchor counts are 18/20 versus 16/20; the counts including the
verified alternative are 18/20 versus 17/20.

| Type | Required tables, keyword → vector | Complete table sets | Frozen text-anchor hits |
| --- | --- | --- | --- |
| A2S, 10 questions | 16/22 → 20/22 | 4/10 → 8/10 | 9/10 → 8/10 |
| S2A, 10 questions | 11/20 → 15/20 | 3/10 → 6/10 | 9/10 → 8/10 |

## Concrete changes

**Table improvements:** `dev_0` now returns `event`, `attendance`, and `member`,
recovering the missing attendance link and finding all three required tables.
`dev_20` now ranks `codebase_community.users` first; previously it returned no
reference table and spent a slot on `student_club.member`. `dev_68` now returns
`superpower` and `hero_power` but still omits `superhero`, so it improves from
one to two required tables rather than fully solving table discovery.

**Table regressions:** `dev_46` falls from all three required financial tables
to two: `trans` replaces the necessary `loan` table. `dev_60` falls from one of
two required tables to none: results are `trans`, `order`, and `disp`, omitting
both `account` and `district`. Pure vectors therefore do not dominate on every
question or resolve the remaining financial-schema problems.

**Text gain — `dev_62`, Mercedes:** the history passage
`ctx://team/engineering/hydra/kilt/18564164/0001`, which explains that the name
came from Emil Jellinek's daughter, returns at rank 3. Keyword reranking had
pushed this same candidate to rank 9.

**Text loss — `dev_10`, Abomination:** the vector top three discuss the Ultimate
Marvel version (Chang Lam), Teen Abomination, and television appearances. None
states the intended alter ego, Emil Blonsky. The original keyword result
included the supporting abstract. These are related passages but do not supply
the required identity; the first one could mislead a later answering agent.
No agent was run here, so that downstream error was not observed.

**Text loss — `dev_66`, New Jersey:** the vector results discuss Jersey City's
population growth, the general distribution of municipalities, and statewide
population. They do not identify Newark as the most populous city. The original
keyword result included a supporting source passage.

**Anchor miss with valid evidence — `dev_31`, UK government:** the vector results
include `31717/0056` describing the parliamentary/Westminster government and
`31717/0054` explicitly describing a constitutional monarchy. Together they
support the core answer. Missing the frozen abstract anchors should therefore
not be treated as a semantic evidence failure. These are historical KILT texts;
their statements about the incumbent monarch are not current-state claims.

## Per-question results

Table cells are recovered reference tables / required tables. Text cells show
the rank of the first frozen support anchor; `—` means no such anchor in top 3.

| Case | Type | Table hits, keyword → vector | Text anchor rank, keyword → vector |
| --- | --- | --- | --- |
| dev_0 | S2A | 2/3 → 3/3 | 1 → 1 |
| dev_5 | S2A | 1/1 → 1/1 | 1 → 1 |
| dev_6 | S2A | 1/2 → 2/2 | — → —; corpus gap |
| dev_10 | S2A | 1/1 → 1/1 | 1 → — |
| dev_11 | S2A | 2/2 → 2/2 | 1 → 1 |
| dev_13 | S2A | 1/3 → 2/3 | 1 → 1 |
| dev_20 | S2A | 0/1 → 1/1 | 2 → 2 |
| dev_26 | A2S | 1/2 → 2/2 | 1 → 1 |
| dev_31 | A2S | 1/2 → 2/2 | 2 → —; alternative support verified |
| dev_32 | A2S | 2/3 → 3/3 | 1 → 1 |
| dev_35 | A2S | 2/3 → 2/3 | 1 → 1 |
| dev_42 | S2A | 1/2 → 1/2 | 3 → 1 |
| dev_43 | A2S | 1/1 → 1/1 | 2 → 1 |
| dev_46 | A2S | 3/3 → 2/3 | 3 → 2 |
| dev_55 | A2S | 1/2 → 2/2 | 2 → 1 |
| dev_60 | S2A | 1/2 → 0/2 | 2 → 2 |
| dev_62 | A2S | 1/2 → 2/2 | — → 3 |
| dev_66 | A2S | 2/2 → 2/2 | 2 → — |
| dev_68 | S2A | 1/3 → 2/3 | 1 → 3 |
| dev_72 | A2S | 2/2 → 2/2 | 1 → 1 |

## Interpretation and limits

This controlled comparison supports disabling the current keyword reranker for
table discovery on this fixture. It does not support a universal claim that pure
vectors improve text retrieval: one text failure is repaired, two new evidence
failures appear, and one anchor miss is actually valid alternative evidence.
The comparison also shows why a better-looking first result does not guarantee
that all required evidence is in the returned set.

The test keeps the earlier experiment's limitations: 20 deterministically
selected questions; a case-informed 2,060-article subset; fixed table queries
using the complete question; and text queries using the release's annotated
search question, which often already contains the correct intermediate entity.
This measures retrieval under those conditions, not agent query formulation,
SQL accuracy, final-answer accuracy, or full HyDRA/KILT performance. No additional
query reformulations, database filters, embeddings, labels, or chunking changes
were introduced during the comparison.

## Records and replay

- [Baseline retrieval run](runs/20261104-expanded-probes/)
- [New pure-vector retrieval run](runs/20261104-vector-only-probes/)
- [Paired metrics and exact returned URIs](artifacts/20261104-vector-only/comparison.json)
- [Candidate-order audit for all 40 query pairs](artifacts/20261104-vector-only/candidate-audit.json)
- [Before-run configuration and database fingerprints](artifacts/20261104-vector-only/before.json)
- [After-run configuration and database fingerprints](artifacts/20261104-vector-only/after.json)
- [Text evidence review of changed cases](artifacts/20261104-vector-only/text-review.json)
- [Metric recomputation script](artifacts/20261104-vector-only/compare.py)
- [Original corpus, selection, and protocol details](2026-11-04-EXPANDED-RETRIEVAL.md)

To start the alternative API with the same isolated database and embedding
credentials already supplied through the environment:

```bash
RERANK_STRATEGY=none python -m uvicorn contexthub.main:app \
  --host 127.0.0.1 --port 18084

python -m integrations.hydrabench.probe \
  --cases data/hydrabench-expanded/selected-private.json \
  --url http://127.0.0.1:18084 \
  --output docs/hydrabench/runs/NEW_VECTOR_PROBES
```

To recompute this comparison without any API or model calls:

```bash
python docs/hydrabench/artifacts/20261104-vector-only/compare.py \
  --baseline docs/hydrabench/runs/20261104-expanded-probes \
  --vector docs/hydrabench/runs/20261104-vector-only-probes \
  --labels data/hydrabench-expanded/text-support-labels.json \
  --output /tmp/hydra-vector-comparison.json
```
