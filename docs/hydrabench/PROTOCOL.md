# HyDRA-Bench protocol and ContextHub smoke adapter

This integration checks whether Deep Agents can combine ContextHub retrieval with
queries against the original BIRD SQLite database. It is an integration smoke
test, not a reproduction of the paper's scores.

The first completed smoke run is documented in
[the 2026-11-04 report](2026-11-04-SMOKE.md), including the preserved initial
attempts and one S2A evidence-check rerun.

The subsequent [expanded retrieval diagnostic](2026-11-04-EXPANDED-RETRIEVAL.md)
uses 75 table schemas and 18,265 text passages. The current runner implements
that stricter protocol: enforced top-three/search budgets, schema enumeration
blocked, and SQL limited to retrieved tables. The smoke differences below describe
the historical two-case run; its exact earlier prompts remain in the traces.

The subsequent [pure-vector comparison](2026-11-04-VECTOR-ONLY.md) reruns the
same 20 fixed probes with keyword reranking disabled, without running an agent.

The [full 20-case run with Qwen and per-type retrieval](2026-11-04-MIXED-QWEN.md)
uses vector ordering for table searches and keyword reranking for text. It records
13 core reference matches, one partial answer, five reference mismatches, and
one generation-budget failure, with per-case evidence and ambiguity notes.

The subsequent [original-meaning review](2026-11-04-SEMANTIC-REVIEW.md) separates
reference agreement from support under the original questions. The
[paper-metric mapping](2026-11-04-PAPER-METRICS.md) documents Acc_avg, Best@k,
tool-use diagnostics, and which metrics the existing single run can support.

## Verified sources

- [Snowflake/HyDRA-Bench](https://huggingface.co/datasets/Snowflake/HyDRA-Bench):
  question release; 15,516 training and 2,527 development examples. Development
  contains 849 A2S and 1,678 S2A questions across 11 databases.
- User-supplied paper: **Bridging Databases and Documents: Data-Algorithm Co-Design
  for Hybrid Question Answering**, COLM 2026 version. Section 5.1 (p. 8) describes
  evaluation; Appendix 7.3 (pp. 16–17) describes tools and retrieval; Appendix 7.4
  (pp. 17–18) describes judge validation. The runtime page was visually checked
  against extracted text.
- [Original BIRD dev archive](https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip):
  `dev_20240627/dev_databases.zip`, containing `toxicology/toxicology.sqlite` and
  column-description CSV files. The original SQLite bytes are preserved.
- [Official KILT knowledge source](https://dl.fbaipublicfiles.com/KILT/kilt_knowledgesource.json):
  Wikipedia snapshot dated 2019-08-01, not current live Wikipedia. Its raw JSONL
  download is 37,318,876,722 bytes. The question release does not itself contain
  these documents.
- [s-nlp/kilt paragraph mirror](https://huggingface.co/datasets/s-nlp/kilt): the
  acquisition source for the smoke subset. Each row preserves one original KILT
  paragraph and its Wikipedia ID plus paragraph number. The full mirror has
  approximately 112M rows. The source revision and exact subset hashes are saved
  with the experiment. It is not Snowflake's prebuilt passage index.

The inspected HyDRA-Bench release supplies `db_id`, question, natural-language
answer, intermediate SQL/search questions and answers, SQL, evidence, hints and
decomposition. It does **not** supply source passage IDs or a scoring script.
The paper specifies judge identity and semantic correctness, but not an exact
executable judge prompt. Those missing artifacts must not be presented as an
official evaluator that we have reproduced.

## Paper protocol

| Component | Published setting |
| --- | --- |
| A2S | SQL-derived answer supplies the next text-search query |
| S2A | Text-search answer supplies the next SQL query |
| Database | One database bound by `db_id`; no cross-database routing test |
| SQL tool | One read-only SQLite statement, 10-second timeout |
| Schema | Not injected; discovered via `sqlite_master` and `PRAGMA` |
| Corpus | KILT 2019-08-01; abstracts retained, article bodies split into 4–5 sentences; about 38.5M chunks |
| Search | Cortex Search: vector, keyword and semantic reranking; `snowflake-arctic-embed-m-v1.5` |
| Search limits | Top 3 passages, at most 10 search calls per episode, 3,000 characters per passage |
| Hints | Strict zero-hint H0 evaluation |
| Repetitions | 8 independent runs; mean accuracy and Best@8 |
| Correctness | GPT-o4-mini semantic judgment of final answer against reference; not exact string equality |

Keeping SQLite avoids altering SQL dialect, types, null behavior and reference
execution. ContextHub's PostgreSQL database stores text contexts, table metadata,
embeddings and retrieval records; it is not the SQL execution target.

## Deliberate smoke-test differences

1. Only `dev_19` (A2S) and `dev_39` (S2A): the first question of each type for
   `toxicology` in release order, chosen before any model run. The database has
   four tables. Neither this choice nor its results measures large-catalog table
   discovery or database routing.
2. A fixed chemistry corpus containing the first 20 elements by atomic number is
   used instead of the complete Wikipedia index. This subject was chosen with
   knowledge of the two cases. It lowers retrieval difficulty and is not a blind
   retrieval benchmark. Reference answers are never indexed as documents.
3. ContextHub performs retrieval with its configured embedding model and ranking.
   Each small source passage is stored verbatim with a provenance header at all
   three content levels. There is no LLM-generated summary or per-question
   retrieval tuning. The sentence splitter is a simple punctuation-based grouping
   into four sentences, not a claimed copy of Snowflake's unpublished splitter.
4. Table schemas **and original BIRD column-description CSVs** are available
   through ContextHub. This adds metadata beyond the paper's SQLite-only schema
   discovery interface. Tables and samples are not injected into the prompt.
5. Deep Agents receives ContextHub search/read plus a bound read-only `run_sql`
   tool. Its default virtual filesystem remains empty; it has no shell or local
   dataset access. ContextHub write and feedback tools are not exposed. Each case
   starts a fresh graph with no persistent conversation memory.
6. The harness supplies only `db_id` and `final_question` to the agent. It excludes
   `type`, reference SQL, evidence, hints, decomposition, key term and all answers.
   Initial tool-use instructions were generic and identical for both cases. After
   the first S2A answer bypassed text retrieval, a generic instruction to verify
   external facts before using them in SQL was added for an S2A rerun. Both
   prompts and all three attempts are preserved; the rerun is not an independent
   sample for a benchmark score.
7. One initial attempt per case plus the disclosed S2A rerun; temperature 0,
   4,096 maximum output tokens per model
   call, 600-second episode deadline, graph recursion limit 80. SQL output has a
   separately disclosed 200-row cap with a truncation flag. The prompt requests
   top 3 and at most 10 resource searches; these search limits are instructions,
   not enforced quotas. Actual use must be audited in the trace.
8. Final answers are reviewed against the references and the executed SQL results.
   This local inspection is not GPT-o4-mini scoring, a human annotation study,
   Accavg, Best@8, or a general accuracy estimate.

## Run the adapter

Use an isolated ContextHub server and account, following the
[Deep Agents setup guide](../setup/deepagents-integration-guide.md). Install its
server, SDK and adapter dependencies plus `langchain-openai`. Keep both background
propagation and lifecycle workers disabled for this fixed-corpus smoke test.

Export `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `CHAT_MODEL`, `CONTEXTHUB_API_KEY`.
For DashScope, set `CHAT_ENABLE_THINKING=false` if needed. Credentials must not be
written into experiment reports. The test server uses a non-superuser PostgreSQL
role; the `query-agent` needs `engineering` team read/write membership for corpus import.
The schema importer uses the existing `catalog_sync` identity.

```bash
python -m integrations.hydrabench.prepare_passages \
  data/hydrabench/kilt-elements.jsonl data/hydrabench/passages.jsonl

python -m integrations.hydrabench.ingest \
  --database data/hydrabench/databases/toxicology/toxicology.sqlite \
  --passages data/hydrabench/passages.jsonl \
  --output data/hydrabench/ingestion.json

python -m integrations.hydrabench.run \
  --dev data/hydrabench/dev.json \
  --database-root data/hydrabench/databases \
  --cases dev_19 dev_39 \
  --output docs/hydrabench/runs/NEW_RUN

python -m pytest tests/test_hydrabench_sqlite_tool.py \
  tests/test_hydrabench_public_input.py -q
```

The importer expects an empty experiment corpus and fails on duplicate URIs.
The runner requires a new output directory rather than replacing old results.
Record corpus/database hashes, model and package versions, source provenance,
retrieval hits and reads, exact generated SQL, tool errors, final answers, timing
and evaluation method. Preserve failed attempts as well as successful ones.
