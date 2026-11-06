# Text storage, optional layers, and agent evidence delivery

The duplicate bodies came from two separate decisions: the HyDRA fixture loader
copied each source into all three content columns, and the DeepAgents adapter
serialized every search-result body field. Neither is a requirement to generate
three summaries. This repair keeps short evidence and complete schema descriptors
as raw L2 content, makes that representation searchable, and gives the agent one
body per hit. It does not rebuild or modify the original experiment index.

## Design, implementation, and the fixture

The original design sources were read from the main checkout's ignored `plan/`
directory: `02-information-model.md`, `11-long-document-retrieval.md`,
`03-datalake-management.md`, and `16-implementation-delta.md`. The experiment
worktree has no copy of that directory. The delta register is a historical design
ledger, not sufficient evidence of today's implementation; its statements were
checked against the working tree, including the pre-existing experiment edits.

| Source unit | Original design | Implementation before this repair | HyDRA fixture before repair |
|---|---|---|---|
| Ordinary context | L0 about 100 tokens for discovery; L1 about 2k tokens for reranking/navigation; L2 detail/original | Generic context creation stores supplied nullable fields without generating summaries. Memory/skill generation and the resource generator use prefixes, generally 80/300 **characters**, not those design token budgets. Only L0 was automatically embedded. | Each already-cut source passage was copied in full into L0, L1, and L2. No document ingester or section generator was used. |
| Long document | Original/extracted files on disk, `resource + file_path`; L0 discovery, L1 navigation, SQL L2 NULL; section tree with summaries and character offsets; read original evidence on demand | File storage, PDF/TXT/MD ingestion, grounded sections, tree navigation and keyword fallback exist. Document L0/L1 still use the generic 80/300-character prefix generator, not a semantic abstract/TOC. LLM-generated section summaries are a separate stage. | Articles were converted to passages; no article contexts, files, or section rows were created. |
| Table metadata | L0 table identity/description; L1 schema, column meaning, examples; L2 in structured `table_metadata`, relationships, lineage, templates. Business rows remain in their source system. | Catalog sync and SQL-context assembly exist. `TableSchemaGenerator` is deterministic, with 80/500-character truncation; it does not implement the proposed LLM generation and can omit columns from L1. Real catalog connectors remain partial. | One text descriptor per SQLite table, containing database/table name, full DDL and BIRD CSV descriptions, copied to all three text columns. No structured catalog import; business data stays in original SQLite. |

L2 is not a third summary. L0/L1 are useful derived representations when they
serve discovery or navigation; their existence is not necessary for exact source
retrieval. Only one vector per context is currently stored. There are no separate
L1, L2, or section-summary vector indexes.

The design's "L0 is sufficient" statement is an assumption, not an established
recall guarantee. Current document discovery can miss a name/number present deep
in an article: the coordinator searches sections **only within already-discovered
file-backed candidates**. Its keyword fallback is not a corpus-wide file search.
This repair does not claim to solve that document-discovery limitation, implement
full PageIndex/sirchmunk behavior, or improve semantic ranking.

## End-to-end attribution

1. **Import:** `ingest.py` explicitly assigned `body` to all three fields;
   `index_snapshot.py` inserted the same `$5` three times. `prepare_passages.py`
   also cut the final source string at 3,000 characters, including provenance.
2. **Storage:** the fixture had three identical content values per row. This was
   a loader choice; PostgreSQL columns and API models already allowed NULLs.
   Generic creation does not silently synthesize summaries. Normal generated
   short memories can nevertheless have identical prefixes when the source fits
   within both limits, so identical fields are possible outside this benchmark.
3. **Embedding:** the batch loader embedded the source **once**, not three times.
   Normal create/update/backfill previously selected only L0. Clearing both L0
   and L1 without core changes would remove raw-only rows from keyword search and
   stop their automatic embedding generation.
4. **Discovery:** vectors nominate candidates, or keyword matching is used when
   query embedding is unavailable. This is fallback retrieval, not a combined
   keyword/vector union. Both candidate queries previously loaded L0/L1 only.
5. **Rerank:** keyword BM25 selects L1, otherwise L0; it does not concatenate the
   two. Therefore the two identical fields did not double BM25 input length.
   Keyword fallback did count matches separately in both fields. The working
   tree's existing table-only vector policy is preserved. No ranking-policy
   change is attributed to this repair.
6. **Service/API:** the service always materialized L0/L1, with L2 fetched only
   for `level=L2`. A request for L1 was not a projection to L1 alone. Long-document
   evidence adds `snippet`/`section_id`. The HTTP response also contains a debug
   trace with repeated materialized bodies; that trace was already excluded by
   the typed SDK, so it was not another copy in these agent inputs.
7. **SDK/adapter/model:** the SDK retained the result fields; the old adapter
   returned `model_dump()`. The HyDRA wrapper enforced budgets/table access but
   forwarded that payload. LangChain serialized it into a ToolMessage. The saved
   model histories contain 82 search messages and 246 hits, all with identical
   L0/L1. Persisting both tool events and message histories in a trace file does
   not mean those two log copies were both sent to the model.

Thus the entire three-copy storage problem belongs to the fixture importer;
unnecessary presentation of multiple content representations was also possible
on the normal adapter path. These are distinct from the architectural value of
summaries and from the previously diagnosed reranking misses.

## Granularity and the minimal repair

The frozen corpus contains 18,265 passages: median 505 characters, p90 858, p95
1,006, maximum 3,000, all including source headers. The 2,060 source articles have
median 1,324 characters and maximum 135,943 (joining original paragraph arrays).
The 75 table descriptors have median 882, p95 6,091 and maximum 13,159 characters.

- **Passages:** retain the original unit once in L2, with L0/L1 NULL. Do not spend
  generation calls rewriting a roughly paragraph-sized source. Names, dates,
  quantities and relationships remain available to both embedding and BM25.
  Existing URI, title/section, Wikipedia ID and source URL remain intact. This
  is exact retention of the frozen passage; it cannot recover tails already
  absent from the old capped passage files.
- **Complete articles:** use the existing file/section path for document-level
  navigation, with original text and character positions. Do not switch the full
  benchmark to article granularity in this repair: that changes the retrieval
  experiment. The local sample validates navigation and reads, not discovery
  recall or summary quality. Short full articles need not automatically become
  trees merely because they are called articles.
- **Table descriptors:** keep one complete schema unit, including DDL, foreign
  keys and all available column/value descriptions. Do not sentence-chunk a
  table or replace its fields with an article-style abstract. For this SQLite
  benchmark the minimal representation is a raw L2 descriptor with the original
  full-descriptor embedding input. This is intentionally a text catalog fixture,
  not a claim to have implemented the structured catalog-sync product path.
  Database hashes and CSV encodings remain in its snapshot records. Business
  rows are never imported into ContextHub.

Implementation boundaries:

- **Importer:** both loaders now write only L2. Snapshot records/manifests carry
  `hydrabench-raw-v1`; resume checks reject old triple-copy layouts or mismatched
  source content. New passage preparation retains complete text and refuses to
  overwrite existing corpus outputs/manifests. Recomputing lengths from original
  articles identifies the same 49 previously truncated units; uncapped maximum
  is 7,508 characters. No regenerated corpus was installed.
- **Core service:** discovery embedding input is the first nonempty L0, L1, L2.
  The existing column name `l0_embedding` is retained; its optional-summary
  semantics are documented rather than adding another vector column. Create,
  update, missing-vector backfill and archive recovery follow that selection.
  Explicit NULLs on content PATCH fields can clear a supplied summary; omitted
  fields remain unchanged. The existing SDK update helper still omits None,
  so explicit clearing is an HTTP PATCH operation, not an SDK-helper migration.
  A content update clears its previous vector before regeneration so a failed
  embedding cannot leave the old vector attached to the new body.
- **Search:** for non-file contexts with neither summary, candidate queries load
  L2 as source evidence in the existing `snippet` field; keyword search and BM25
  can use it. Summarized/file-backed records do not eagerly load arbitrary L2
  bodies. The existing ACL, masking, version barriers, stale notices and trace
  fingerprints include the raw evidence path. The API response schema is not
  replaced, and direct L0/L1 reads still read those specific nullable levels.
- **DeepAgents adapter:** each hit exposes one `content` and `content_level`.
  L1 searches prefer source snippets, then L1, then L0; L0 prefers L0; L2 prefers
  returned L2 detail. If only a snippet/overview is available, its actual kind
  is labeled, not called full detail. URI, version, validity, tags, scores,
  section ID, strategy, retrieval ID and stale notices remain. SDK/API clients
  can still inspect all original fields. This is a deliberate presentation
  contract, not an equality test hiding a malformed old record.

## Real-source validation

Validation completed at **2026-11-04 22:26:53 Hong Kong time (UTC+8)**.
Three contexts were exercised in `hydra_layers_sample_20261104_02`, with the real
PostgreSQL schema, non-superuser `hydra_app`, tenant sessions, ACL, services,
FastAPI routes, typed SDK and LangChain ToolMessage serialization. Provider
clients were replaced with exact-input vector replay, no-op query embedding,
and an offline chat client returning no section choice. This tests the real
storage/retrieval/serialization code, not a live model-driven agent episode.
Only schema
and team identity seeds were copied from the old database. The two raw samples
were changed from version 1 to 2 inside this isolated database; new fixture
imports naturally start at version 1. The original index remains versioned as
before. The first local attempt stopped at the document author's root-team write
check; the second sample database adds that membership only for its test author.

| Real source | Storage before → after | Agent body before → after |
|---|---|---|
| Deep Space Habitat, `35872624/0006`, "Suggested support craft." | L0=L1=L2=505-character source → NULL/NULL/same 505-character L2 | Two source copies → one exact original. It still names NASA, Lockheed Martin, Airbus, Orion's 4 crew, and MMSEV's two-person/two-week capacity. |
| `formula_1.constructors` | L0=L1=L2=857-character descriptor → NULL/NULL/same complete L2 | Two schema copies → one full DDL+CSV descriptor, retaining constructorId, constructorRef, name, nationality, url and column descriptions. |
| Microarchitecture, complete 21,522-character source rendering | File-backed resource, L0 80 characters, L1 300, L2 NULL, 22 sections → unchanged | L0+L1+located snippet → one 4,000-character source snippet, with URI/version/section ID; full section and full document remain readable. |

For the article, KILT section markers were rendered as Markdown headings; source
paragraph bodies were preserved, and the source URL, Wikipedia ID and SHA-256
were retained. Section reads were compared exactly with `source[start:end]`;
all ranges were checked against the source length. The article's deterministic
heading path and local tree selection were used; no generated summary quality is
claimed. Its discovery used keyword matching; no article vector was fabricated.

The two raw samples passed keyword search, BM25, identical-text vector
self-queries, full L2 reads and vector backfill. Existing source vectors were
replayed only when the full embedding input matched byte-for-byte. These checks
establish dataflow correctness, not semantic ranking quality. Unit regressions
also cover single-body level selection, lost-tail prevention, raw-source create,
update embedding failure, archive recovery, and existing snippet masking.

Across the targeted suites, **251 distinct tests passed and 26 database-gated
tests were skipped**. The three-source PostgreSQL validation above ran separately;
the skipped legacy tests were not redirected at an experiment database. Five
long-document tests used the old router-shaped stub after the worktree's existing
router change; they now construct the real current router instead of adding a
compatibility branch to production code. `git diff --check` passed. Pre-existing
config, startup, ranking-policy and retrieval-test edits were preserved exactly.

Exact serialized tool-response sizes (characters, not provider tokens):

| Sample | Before characters | After characters | Before / after estimate (`characters / 4`) |
|---|---:|---:|---:|
| Passage | 1,470 | 960 | 367.5 / 240 |
| Schema | 2,211 | 1,328 | 552.75 / 332 |
| Article | 4,964 | 4,542 | 1,241 / 1,135.5 |

The estimate is a crude English-text heuristic, not the answering model's
tokenizer. JSON metadata is included. IDs/scores/version metadata are included
in these measured samples; the old adapter representation was reconstructed
locally for comparison, not obtained from a new answering-model run.

The original 82 saved search ToolMessages were also projected offline through
the new adapter. All 246 bodies, URIs and versions were preserved. Duplicate
body characters total 241,074 (147,127 schema + 93,947 passage). Normalizing both
sides with identical JSON serialization gives 586,741 → 338,604 characters;
the heuristic estimates are 146,685.25 → 84,651. These are single appearances of
search messages, not the accumulated inputs of subsequent model turns.

The historical [usage record](artifacts/20261104-paper-metrics/token-usage.json)
still reports 104 model calls, 968,658 input tokens, 41,509 output tokens and
753,408 cached input tokens. This validation made **zero provider model calls
and zero provider embedding calls**. New actual model usage is unavailable;
there is no measured token saving, cost reduction, or accuracy improvement.
Cache reuse, repeated history, changed tool descriptions and future agent choices
prevent deriving those quantities from character differences.

## Isolation, vector rebuilding, and follow-up boundaries

The original database remained at 18,340 contexts/embeddings; its ordered content
and version fingerprint matched before and after. The source connection for the
successful validation was explicitly read-only. Original source files, experiment
reports, traces and usage records were not overwritten. No benchmark answers,
reference reasoning or reference SQL were used to create index content. No new
full benchmark, full-index rebuild or running-server restart was performed.

Clearing an old duplicate L0 does **not by itself** require a different vector:
if the new selected L2 text is exactly the old L0 input and model/dimensions are
unchanged, the vector represents the same text. This sample validated that case.
The normal update path still regenerates/refreshes the vector; no general-purpose
vector reuse migration was added. A future loader rerun currently calls the
configured embedding client for new rows.

New vectors are required whenever the selected input changes: restored tails in
the 49 capped passages, a new L0 abstract, modified schema/column descriptions,
article-level discovery text, or a different embedding model/dimension. Unchanged
inputs can only be reused with verified input/model identity. New article contexts
need their own discovery vectors; sections currently have no vectors to rebuild.
Use a fresh database, snapshot/output directory and document store for any new
experiment. The old index is retained as the triple-copy baseline.

Open quality issues are explicit: document discovery still uses truncated
prefixes; the table catalog generator can truncate schema overviews; the old
passage splitter is still a simple four-sentence approximation; and raw-only
keyword fallback has one matching source field instead of two identical ones.
Its fallback score scale can therefore change, even though full-text BM25 and
vector input remain the same. This repair makes no ranking-equivalence claim
for mixed summarized/raw corpora. Broader ranking, source-first discovery and
generated navigation changes should be evaluated separately, not bundled into
this duplicate-content fix.

## Evidence and replay

- [Full before/after storage, Agent payloads, section ranges and database fingerprints](artifacts/20261104-layer-validation/validation.json)
- [Offline projection of all 82 original search messages](artifacts/20261104-layer-validation/payload-replay.json)
- [Preserved full article rendering](artifacts/20261104-layer-validation/microarchitecture.md)
- [Three-source validator](../../integrations/hydrabench/validate_layers.py)
- [Original expanded experiment](2026-11-04-EXPANDED-RETRIEVAL.md)

To rerun, prepare a fresh `hydra_layers_sample_*` database with the normal schema
and the experiment's team identity seeds. Export `HYDRA_SOURCE_DATABASE_URL`
(read-only source) and `HYDRA_SAMPLE_DATABASE_URL` (isolated writable target).
Run with the worktree's `src`, `sdk/src`, and `plugins/deepagents/src` on
`PYTHONPATH` and the installed DeepAgents test dependencies:

```bash
python -m integrations.hydrabench.validate_layers \
  --corpus data/hydrabench-expanded \
  --output /tmp/NEW_LAYER_VALIDATION
```

The validator rejects nonempty sample databases and existing output directories.
Local document files for the recorded validation remain under
`/private/tmp/contexthub-layer-validation-02/documents/`; the retained article
artifact allows reconstruction without depending on that temporary store.
