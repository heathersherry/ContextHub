# HyDRA-Bench: expanded retrieval diagnostic

The expanded fixture exposes retrieval failures that the four-table smoke test
could not measure. Fixed question queries recovered 27 of 42 required table
occurrences in the top three, and found every required table for only 7/20
questions. Iterative agent searches recovered every reference table in all nine
completed episodes. Retrieving the right tables did not guarantee correct SQL.

All 20 independent retrieval probes completed. Agent execution completed five
A2S and four S2A questions; the other eleven received a provider HTTP 403 stating
that the free model quota was exhausted. These are infrastructure interruptions,
not incorrect answers or retrieval failures. No paid mode was enabled, no model
was substituted, and no failed or incorrect attempt was removed or retried.

## Fixture and controls

| Dimension | Previous smoke | Expanded diagnostic |
| --- | ---: | ---: |
| Original SQLite databases in catalog | 1 | 11 |
| Indexed table schemas | 4 | 75 |
| Wikipedia articles | 20 | 2,060 |
| Indexed text passages | 1,494 | 18,265 |
| Selected questions | 2 | 20: 10 A2S, 10 S2A |
| Completed agent episodes in this run | — | 9: 5 A2S, 4 S2A |

The 20 questions were fixed before retrieval or agent runs: select the five
databases with the most tables (ties by database ID), then the first two dev
questions of each type per database in release order. These databases are
`formula_1` (13 tables), `superhero` (10), `codebase_community` (8), `financial`
(8), and `student_club` (8). The six other databases remain in the catalog as
distractors. The largest original database has 13 tables; the experiment does
not invent tables or move benchmark rows into PostgreSQL.

Every schema search competes across all 75 schemas. The prompt gives the bound
database ID, but there is no hard database filter in search. This deliberately
tests catalog selection as well as table ranking, beyond the paper's bound-DB
schema discovery. Results from other databases cannot unlock SQLite access.

The wrapper enforces top three results, ten schema searches and ten text
searches per episode. SQLite can read only tables previously returned by schema
search; `sqlite_master`, `PRAGMA`, and guessed source URIs cannot bypass search.
The two ten-call budgets are an adaptation, not the paper's exact tool setup.
Only the original question and database ID reach the answering agent. Reference
SQL, intermediate questions, answers, type, and evidence labels remain outside.
Each episode has a fresh graph and empty virtual filesystem, with no shell,
dataset access, memory writes, or delegated tasks.

The corpus consists of 2,000 background articles selected by a fixed title hash
from complete articles in KILT mirror shard 0000, plus 60 case-informed source
candidates and related distractors. It is not a uniform sample of all Wikipedia,
and the added topic articles make this easier than a blind full-corpus test.
The sources are Wikipedia/KILT text, not benchmark answers. None were added
after observing retrieval or agent results. The full article paragraphs were
preserved, then chunked by the existing abstract/four-sentence method with a
3,000-character cap including provenance. Forty-nine passages hit that cap.

All 18,340 indexed contexts have embeddings. The batch fixture loader used the
configured embedding client and tenant-scoped repository; search used the
unmodified ContextHub API and existing ranking. This tests retrieval, not the
document ingestion API or generated L0/L1 summaries: each passage is stored in
full at L0, L1, and L2.

## Fixed-query retrieval results

These probes separate retrieval from an agent's query formulation and SQL.

- Table query: `Database: {db_id}. {final_question}`. A required table is a table
  read by the released reference SQL, determined with SQLite's authorizer during
  `EXPLAIN QUERY PLAN`. These are reference-path labels; alternative valid SQL
  paths are possible.
- Text query: the release's annotated `search_question`. This is an explicitly
  oracle diagnostic, often containing an entity that SQL would first need to
  discover. It is never passed to the answering agent.
- Text labels: non-exhaustive core-fact passage anchors inspected by the coding
  assistant and frozen before retrieval. They are not official passage labels
  or an independent human annotation study. A missed anchor requires semantic
  inspection before calling it an evidence failure.

| Probe | Result |
| --- | --- |
| Required table occurrences recovered in top 3 | 27/42 = 64.3% |
| Questions with all reference tables in top 3 | 7/20 = 35% |
| Questions with at least one reference table in top 3 | 19/20 |
| A2S required tables / complete table sets | 16/22; 4/10 questions |
| S2A required tables / complete table sets | 11/20; 3/10 questions |
| Known text anchor in top 3, among questions with located core evidence | 18/19 |
| Known text anchor in top 3, across all selected questions | 18/20 |

The denominator 42 counts a table once per question, not 42 distinct tables.
Text anchor hit rate is not precision of all returned passages, complete answer
support, or end-to-end accuracy. Coverage labels include qualifications: `dev_32`
has evidence about prepaid cards for teenagers but no proof that the SQL value
`junior` means prepaid; `dev_20` requires inference from an exhibition list; and
`dev_46` uses broader Moravian history. See the preserved label notes.

Representative failures:

- `dev_20`: the expected `codebase_community.users` table is absent. Results are
  `comments`, `student_club.member`, and `badges`.
- `dev_26`: one of the three table slots goes to `toxicology.bond`; `posts` is
  present but `users` is missing. `dev_42` similarly returns football/debit-card
  tables alongside `financial.client`, omitting `district`.
- `dev_62`: the Mercedes text probe finds the right articles, but returns their
  abstract, general history, and trademark-registration paragraph. None states
  that Mercedes was named after Emil Jellinek's daughter. The supporting
  Mercedes-Benz history and Jellinek abstract passages are indexed but outside
  the returned top three. This is a real passage-ranking miss.
- `dev_6`: no direct evidence for the Indian Half-bred horse was located in the
  frozen subset. Its missing text hit is a corpus-coverage limitation, not proof
  that the ranker discarded an available answer. This makes no claim about the
  full KILT corpus.

## Deep Agents runs

All nine completed episodes eventually retrieved all 18 reference-table
occurrences. The first issued schema search found 11/18 occurrences and complete
table sets for 5/9 episodes. After one to three searches per episode, complete
coverage rose to 9/9. Concurrent searches are ordered by invocation time when
computing the first-query metric, not by response completion time.

This is not a claim that the ranker succeeds on its first attempt, nor a score
on all 20 questions. The nine completed cases cover only Formula 1, superheroes,
and one codebase question; provider exhaustion prevented completed financial or
student-club episodes. Different queries and denominators mean the fixed-query
7/20 and iterative-agent 9/9 rates are not a controlled before/after comparison.

| Case | Type | First → cumulative reference table hits | Schema/text searches | Time | Local answer review |
| --- | --- | --- | --- | ---: | --- |
| `dev_43` | A2S | 1 → 1/1 | 1 / 1 | 12.533 s | Core date and declaration significance match |
| `dev_62` | A2S | 1 → 2/2 | 2 / 1 | 13.684 s | Ferrari instead of reference Mercedes; SQL aggregation differs |
| `dev_5` | S2A | 1 → 1/1 | 1 / 1 | 8.382 s | Silverstone coordinates match |
| `dev_13` | S2A | 1 → 3/3 | 3 / 1 | 16.598 s | 0.25 instead of reference 2.27; different points table/meaning |
| `dev_35` | A2S | 3 → 3/3 | 2 / 3 | 58.197 s | Tie ambiguity plus unsupported race mapping and count error |
| `dev_72` | A2S | 2 → 2/2 | 1 / 1 | 14.412 s | Publisher and core storyline significance match |
| `dev_10` | S2A | 1 → 1/1 | 1 / 2 | 13.126 s | Weight difference 351 kg matches |
| `dev_68` | S2A | 0 → 3/3 | 3 / 1 | 16.348 s | All seven powers match |
| `dev_31` | A2S | 1 → 2/2 | 3 / 1 | 16.828 s | Location and form of government match |

Six answers match the core reference on local assistant inspection; the three
other cases are discussed below. This is not the official semantic judge,
Accavg, Best@8, or an unbiased overall benchmark accuracy estimate. Extra factual
claims in otherwise matching prose have not all been independently verified.

The agent retrieved a frozen text anchor in six completed cases. `dev_68` missed
its non-exhaustive anchor but retrieved another sufficient passage,
`565879/0013`, explicitly identifying Helen Parr/Elastigirl as Bob's wife.
Therefore seven completed episodes had retrieved text supporting the intended
core external fact. The other two (`dev_62`, `dev_35`) followed different entity
branches; their lack of reference evidence cannot be attributed to ranking alone.

All four completed S2A episodes searched text before dependent SQL. Some searches
already contained a hypothesized entity; this demonstrates verification, not
necessarily discovery from scratch. A2S examples `dev_43` and `dev_72` contain
the named declaration/storyline in the question, so the agent searched text
before SQL. Do not describe every A2S episode as strictly SQL-then-text.

### Attribution: retrieval versus SQL and reasoning

**`dev_62`, constructor points:** ContextHub supplied `constructorStandings` and
`constructors` before SQL. The agent used `SUM(cs.points) GROUP BY constructorId`
and selected Ferrari (63,855.5 summed points). The reference orders individual
standing records by points and takes one, selecting Mercedes. Text retrieval
then found Ferrari material appropriate to the agent's SQL result. The actual
episode diverged at SQL interpretation, independently of the Mercedes passage
miss in the separate oracle probe. The natural-language question does not spell
out the aggregation, so this is a reference mismatch, not a SQL syntax failure.

**`dev_13`, Eddie Irvine:** The nickname evidence was retrieved correctly.
`driverStandings`, `drivers`, and `races` were all available; the first schema
result already included `driverStandings`. The agent nevertheless averaged
`results.points`, returning 4/16 = 0.25. The reference averages
`driverStandings.points`, yielding 2.2666667. This is table/measure selection by
the agent, not failure to retrieve the reference table. The wording "average
points scored" is underspecified, and the imported BIRD description for
`driverStandings.points` itself says points acquired in each race. Better table
ranking alone cannot resolve that semantic ambiguity.

**`dev_35`, maximum attribute:** All three required schemas were retrieved in
the first search. The agent's first SQL result already contained Cosmic Entity,
the reference answer, alongside other tied races. A post-run audit confirms 279
maximum-valued attribute rows spanning 117 heroes. The reference's `ORDER BY ...
DESC LIMIT 1` has no secondary tie-break, so the question is genuinely ambiguous.
The agent then chose the most common race, Human, and mapped it to a Theosophical
race without source support for that mapping. It also described all 3,738 table
rows as maximum-valued rows after misreading `MAX(...), COUNT(*)`. Relevant text
retrieval followed that speculative branch. This combines an ambiguous benchmark
reference with agent reasoning/counting errors; it is not simply a table-search
failure. No question-specific prompt fix was introduced after seeing it.

For comparison, the earlier calcium A2S smoke case also received the relevant
table schemas and a calcium passage. Its key deviation was counting atoms with
`COUNT(*)` instead of distinct molecules, with a separate unresolved tie. The
earlier S2A rerun matched all six bond IDs after retrieving text. These established
integration, while the old four-table return-all setup could not establish table
retrieval quality. See the [original report](2026-11-04-SMOKE.md).

## Infrastructure, validation, and limitations

- ContextHub runs in a separate database, `hydra_retrieval_20261104`, using the
  existing local PostgreSQL container on port 55433. Its API is on port 18083;
  the earlier smoke database/server on port 18082 is separate. Background
  propagation and lifecycle workers are disabled. Application access uses a
  non-superuser, non-RLS-bypass role.
- Model: `deepseek-v4-pro-0813`, Deep Agents 0.7.32, temperature 0, thinking off,
  max output 4,096 tokens/call, episode deadline 600 seconds, concurrency two.
  Existing ContextHub retrieval: `text-embedding-v4`, 1,536 dimensions, keyword
  reranking. No search code, model, or ranking parameters changed during this run.
- The model provider returned quota-exhaustion HTTP 403 for `dev_26`, `dev_6`,
  `dev_20`, `dev_32`, `dev_46`, `dev_42`, `dev_60`, `dev_55`, `dev_66`, `dev_0`,
  and `dev_11`. The first two have partial tool traces; the remaining nine failed
  before any search. In `dev_26`, an early SQL call raced the schema search and
  was correctly denied access to `users`; a later call succeeded after retrieval.
  Raw errors are retained. Further agent evaluation needs
  available model quota; there is no automatic billing change or retry loop.
- All attempted SQLite file hashes are identical before and after execution.
  Reference SQL for the nine completed episodes was separately replayed to
  verify the audit. All searches returned at most three results and stayed within
  the enforced budget. No tool errors occurred in the completed episodes.
- The relevant test suite passed **44 tests**, including read-only SQL, timeout,
  annotation isolation, retrieval budgets, blocked schema bypass, table access
  gates, and the existing Deep Agents adapter tests.
- Setup correction: four original BIRD description CSVs require cp1252 decoding.
  The first fixture import stopped before inserting contexts; after recording
  their encoding, the full index completed. This was not a search-result-driven
  change. Network acquisition retries also occurred before the corpus was frozen.
- This is a controlled subset diagnostic with a deliberately larger catalog,
  not the paper's full 38.5M-passage corpus, official evaluator, or eight-run
  protocol. The surviving nine model episodes are not representative of all 20.

The practical priority suggested by this evidence is to improve first-pass table
ranking and database scoping, while testing SQL measure selection separately.
The text component can locate useful evidence in this easier corpus, but the
Mercedes example shows that finding the article is not enough. None of these
observations establishes performance on the full KILT corpus.

## Follow-up: locating two ranking failures

A read-only inspection of the original stored retrieval candidate lists, followed
by offline recomputation of the unchanged BM25 keyword scores, identifies the
stage of two failures. All candidate versions remain 1 with no adoption/ignore
feedback; the recomputed order matches the recorded final results. No new query
embedding or model call was made, and no search implementation was changed.

- `dev_68` agent query `superhero character superpowers table`: `superpower`
  moved from vector-candidate rank 1 to keyword rank 7, `hero_power` from 2 to 5,
  and `superhero` from 5 to 9. The final top three were `alignment`, `race`, and
  `gender`. The tokenizer does not normalize plural `superpowers` to singular
  `superpower`; the `character` term in the alignment description contributed
  1.5495 of its 1.6887 score and dominated the ordering.
- `dev_62` fixed text probe: the Mercedes-Benz history passage `18564164/0001`,
  explicitly explaining the daughter-name origin, moved from vector-candidate
  rank 3 to keyword rank 9. It was available in the initial candidate set but
  removed by final top-three truncation after reranking. This is separate from
  the actual agent episode, which searched Ferrari after a SQL interpretation
  mismatch.

The current retrieval path selects nine vector candidates for top-three requests,
then sorts by keyword score without blending the original vector similarity.
These two examples establish a reranking problem; they do not attribute every
miss in the experiment to the same stage.

[Candidate ranks, recomputed keyword contributions, and original retrieval IDs](artifacts/20261104-expanded/retrieval-ranking-audit.json)

## Artifacts and replay

- [Fixed selection and all 75 table names](artifacts/20261104-expanded/selection.json)
- [Corpus provenance, selection rules, and hashes](artifacts/20261104-expanded/corpus-manifest.json)
- [Complete index manifest](artifacts/20261104-expanded/index-manifest.json)
- [Database hashes and schema metadata](artifacts/20261104-expanded/table-catalog.json)
- [Text labels frozen before runs](artifacts/20261104-expanded/text-support-labels.json)
- [Fixed-query summary](artifacts/20261104-expanded/probe-summary.json)
- [Agent summary, queries, results, and all statuses](artifacts/20261104-expanded/agent-analysis.json)
- [Post-run SQL audit](artifacts/20261104-expanded/sql-audit.json)
- [Local semantic review](artifacts/20261104-expanded/answer-review.json)
- [Metric recomputation script](artifacts/20261104-expanded/summarize.py)
- [All fixed-query traces](runs/20261104-expanded-probes/)
- [All original agent traces, including quota errors](runs/20261104-expanded-agents/)
- [Runtime code hashes frozen before runs](artifacts/20261104-expanded/code-hashes.json)
- [Official protocol comparison](PROTOCOL.md)

Source articles, passages, all original SQLite files and BIRD CSV descriptions,
the full index snapshot, acquisition records, and private evaluation inputs are
retained locally under ignored `data/hydrabench-expanded/`. Source release and
mirror revisions are pinned in the manifests. No credentials are included.

With the existing API and credentials supplied through the environment:

```bash
# A fresh probe directory preserves previous observations.
python -m integrations.hydrabench.probe \
  --cases data/hydrabench-expanded/selected-private.json \
  --url http://127.0.0.1:18083 \
  --output docs/hydrabench/runs/NEW_PROBES

# Only after model quota is available; keep the original errors.
python -m integrations.hydrabench.run \
  --dev data/hydrabench/dev.json \
  --database-root data/hydrabench-expanded/databases \
  --cases dev_26 dev_6 dev_20 dev_32 dev_46 dev_42 dev_60 dev_55 dev_66 dev_0 dev_11 \
  --url http://127.0.0.1:18083 --concurrency 1 \
  --output docs/hydrabench/runs/NEW_QUOTA_RETRY

# Recompute this report's metrics locally without any model/API calls.
python docs/hydrabench/artifacts/20261104-expanded/summarize.py
```

To rebuild a fresh isolated index, initialize ContextHub's normal database schema,
seed the `acme` account and `query-agent` engineering membership, and export its
`DATABASE_URL` plus the configured embedding credentials. Run
`python -m integrations.hydrabench.index_snapshot --passages
data/hydrabench-expanded/passages.jsonl --database-root
data/hydrabench-expanded/databases --output data/hydrabench-expanded/NEW_INDEX`.
Do not point this fixture loader at a production database or mix it with the old
smoke corpus. The loader checks the frozen snapshot when resuming an import.
