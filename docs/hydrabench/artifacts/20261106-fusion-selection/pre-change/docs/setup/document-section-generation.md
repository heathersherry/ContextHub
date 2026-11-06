# Document section generation

ContextHub stores one resource row per uploaded document: document L0, optional
L1, URI, version, provenance tags, and `file_path`. Original chunk vectors live separately in
`document_chunk_embeddings`, keyed by document/version and source range. L2 is read from the extracted
source file; `contexts.l2_content` is NULL. `document_sections` stores parent
links, titles, local descriptions, and character ranges. It does not copy the
document L0/L1 or complete body into each section.

## Document layers

- **L0:** the existing document-generation call now returns a document
  introduction together with anchored section descriptions. Its final L0 is
  embedded once. There is no separate summary-generation call. If generation is
  disabled, unconfigured, oversized, or fails, L0 is explicitly metadata-only:
  `Document: <source filename stem>`. It is never the first 80 body characters.
- **L1:** code renders navigation from verified source headings after their
  section IDs have been saved. Entries contain titles, hierarchy, section IDs,
  and character ranges. No model call is required. Without recognized source
  headings, L1 is NULL; sequential reading ranges are not promoted to chapters.
- **L2:** the complete extracted original remains in `extracted.txt`, alongside
  the copied source file and extracted Markdown. Document and section reads use
  the existing ACL and version checks.

Short standalone contexts may instead have NULL L0/L1 and an inline L2 original.
Their discovery embedding uses the original text. Table metadata keeps its own
schema/catalog path; it is not processed as an article. The HyDRA raw schema
snapshot uses L2-only text and leaves business rows in the source SQLite database.

## Source structure and model calls

The full extracted text is numbered before the existing combined generation call.
The response contains `description` and `sections`. Code checks section line
labels, exact source lines, ordering, and nesting. A valid model paragraph anchor
proves a location, not a chapter: navigation is defined by source headings, not
model-invented boundaries. Model section descriptions are reused only when title
and complete source span match the deterministic heading structure.

Recognized structure currently means Markdown headings outside fenced code
blocks, or headings reconstructed by the existing PDF extractor and reanchored
in the source. Plain text without recognized headings uses sequential ranges.
This conservative parser may miss real structure; it does not claim universal
semantic heading detection. A separate table-of-contents page is not required.
Repeated and wrapped headings are matched in source order. Original heading nodes
retain their IDs and spans; oversized leaves gain bounded child reading ranges.
Large parent sections can span multiple children.

The combined call retains the 2,000-token output budget. If the complete numbered
source exceeds 120,000 characters, generation is skipped for the entire document.
No model is asked to describe unseen tail text. NoOp/offline ingestion is allowed.
Logs report `source=markdown_headings` or `source=sequential_chunks`; these describe
structure provenance, not whether L0 generation succeeded. Malformed responses
and provider failures are logged separately.

## Original-text discovery

Normal summary/vector discovery and existing tree navigation remain available.
A separate keyword route searches all eligible file-backed resources, independent
of L0 candidate membership, vector availability, or an arbitrary document limit:

1. Apply tenant, type, scope, status and freshness filters in SQL, then ACL before
   any source file is read.
2. Tokenize query and original fragments consistently. English word/number runs
   match complete tokens; Han characters are tokenized individually for unspaced
   text. Preserve the existing query stopwords and deduplication, without stemming
   or a keyword-extraction model call.
3. Build all source ranges using the stored section tree, before filtering any
   query matches. Return a complete chapter when
   it fits the 4,000-character snippet limit; otherwise descend into its children.
   Document-wide root/title wrappers do not collapse sibling chapters into one
   snippet. Parent introductions remain searchable in their own source ranges.
4. Split oversized remaining ranges at paragraph boundaries, then sentence
   boundaries, then whitespace. Only a single unbroken run longer than the limit
   needs a hard character split. Never merge across sibling chapter boundaries.
   Score these originals with the shared BM25 computation (`k1=1.5`, `b=0.75`).
   Document frequency and average token length use all ranges of all supplied
   eligible, ACL-filtered documents, including zero-hit ranges. Only then retain
   positive-score ranges for fusion with original-vector candidates. Neither
   vector top-k nor matching-range filtering defines the BM25 statistics corpus.
5. Independently search `document_chunk_embeddings` with the existing query
   embedding, restricted to ACL-visible eligible documents and matching document
   versions. Check the source hash before reading the exact stored range. Resolve
   each hit to its original section ID.
6. Keep the union of lexical and vector candidates, keyed by document, version
   and exact range. Complete cosine scores for lexical-only candidates by looking
   up their existing stored range vectors with the same version/source checks.
   This does not add candidates or call an embedding model. Because BM25 emits
   every positive match, vector-only candidates have a genuine BM25 score of zero.
   Normalize each score over the candidate union with `(score-min)/(max-min)`;
   a constant/empty route contributes no ranking distinction. Combine with the
   fixed weights `0.8 * normalized_cosine + 0.2 * normalized_bm25`. A range with
   no valid stored vector has no vector contribution and is excluded from cosine
   statistics, rather than receiving a fabricated cosine of zero.
   Retain different ranges from the same document, including ranges sharing a
   section ID. A range found by both routes is one hit. Replace that document/
   version's old discovery/tree representative with its original ranges. When
   other contexts remain, the separate outer merge retains its existing
   equal-weight RRF constant 60, keyed by document/version/range. When only
   originals remain, keep their weighted scores without another fusion step.
7. Apply common quality factors, validity handling, ACL masks and the final exact
   version check, then take global `top_k` evidence hits. There is no one-hit-per-
   document quota. Explicit L0 discovery still returns one result per document.
   Original-only hits do not trigger extra tree-navigation calls and are not
   reranked using only their L0/L1. The existing vector candidate budget remains
   `top_k * 3`; API/SDK defaults remain 10 and the DeepAgents tool default remains 5.

The old conditional keyword fallback after tree navigation has been removed;
independent original discovery supplies that evidence. Searches restricted to
non-resource types do not scan document files. `snippet_offset` carries a half-open
character range `[start, end)` through the API, SDK and DeepAgents tool, alongside
URI, version and section ID. It locates the unmasked extracted original, even if
field masking changes the delivered text's length. Existing section reads remain
available. Candidate and materialized traces retain individual range locations.
The public materialized trace now uses the same masked text as served results.
See the [single-query section-boundary check](../hydrabench/2026-11-05-SECTION-SNIPPETS.md)
for the exact before/after Agent bodies. This selection change does not rebuild
stored sections, change L0/L1, or regenerate embeddings.

The DeepAgents adapter emits one `content` body per hit. For L1 requests, a located
original snippet takes priority over navigation/description; URI, version and
section ID and source range remain available. For file-backed evidence hits,
the generic search result leaves L0/L1/L2 body fields empty instead of repeating
document layers alongside each snippet. Explicit L0 discovery retains overview
fields. Stored layers and complete document/section reads are unchanged.

See the [saved-vector passage top-k replay](../hydrabench/2026-11-05-PASSAGE-TOPK.md)
for actual before/after Agent bodies. This change requires no schema migration,
new model call, chunk regeneration or index rebuild. More distinct evidence can
increase the returned text; it does not by itself improve relevance.

## Limits and index handling

The [fixed 0.8/0.2 replay](../hydrabench/2026-11-06-WEIGHTED-FUSION.md) records the
completed candidate scores and all Agent bodies. It improves dedicated-pipeline
inclusion for the semantic query but lowers the exact query's pipeline rank from
1 to 3. No weight tuning or broader relevance claim is made. Min/max normalization
depends on the candidate set; the weights do not guarantee preservation of every
vector rank.

The lexical route still scans eligible files: I/O grows with corpus size. The
original-vector route adds semantic candidates without a summary gate; neither
route guarantees relevance. No inverted index, generated per-chunk summary or
new model reranker is introduced. BM25 statistics are computed in memory on
search; no schema migration or vector rebuild is required. English inflections
remain distinct (`make`/`makes`, `cpu`/`cpus`); Han character tokenization is not
linguistic word segmentation. Ranking quality and large-corpus latency require
evaluation. The [fixed BM25 replay](../hydrabench/2026-11-06-ORIGINAL-BM25.md)
records all before/after Agent bodies and the absence of a top-5 set improvement
on the two sample queries. Existing L1 reranking retains its own tokenizer and
candidate-local statistics; the shared scoring formula does not merge these routes.

Migration `011` adds version-bound chunk vectors and an HNSW index. New document
ingestion reuses source headings and natural boundaries to cover the original
once, including parent introductions, without overlapping parent/child vectors.
Embedding inputs are source title paths plus exact slices. The existing batch
client embeds them; this adds no chat calls. Configured chunk-embedding failures
abort ingestion instead of silently saving a partial index. Explicit NoOp
embedding ingestion remains supported without vectors. Source bodies and L0/L1
are not duplicated into this table.

The query-independent range builder is shared by lexical selection and indexing.
The 4,000-character body limit is not a token count; provider token limits still
apply to the title-plus-body input. Search reuses its query embedding. Existing
LLM tree navigation remains separate and can still incur its original calls.
Changing a document version invalidates older chunk rows for retrieval; indexing
the current version is explicit, with no automatic historical backfill.

See the [one-document, two-query check](../hydrabench/2026-11-05-CHUNK-VECTORS.md).
The exact-identifier result remained correct; the fixed semantic query did not
reach the desired section. This is not evidence of improved corpus-wide recall.

Changing L0 changes its embedding input. New document introductions must be
embedded in a new isolated ingestion/index; do not reuse old prefix embeddings.
L1 navigation and section-range changes alone do not require embedding changes.
The new chunk-table migration does not backfill data or rebuild old L0 vectors.
Existing indexes are not changed automatically; apply schema changes deliberately
before running the updated service. The sample check applied the migration only
to its new isolated database.

## Model configuration

`EMBEDDING_MODEL` selects the embedding model independently of `CHAT_MODEL`.
`EMBEDDING_DIMENSIONS` is sent as the provider's `dimensions` parameter and is
also the expected response width. A mismatched response is rejected, not padded.
The model must support that requested width, and the database vector column must
match it. Changing the model or effective dimensions requires a separate,
consistently embedded index; equal vector widths do not make different model
spaces compatible. Historical v4 sample results remain v4 results after a local
configuration change to `qwen3.7-text-embedding`. No automatic regeneration runs.


For a supported hybrid-thinking endpoint, `CHAT_ENABLE_THINKING=false` can leave
the output budget available for final JSON. The parameter is sent only when
configured. Credentials, model names, and embedding settings remain separate.
A successful fallback ingestion does not prove model-generation quality.

## Verification

See [the document-layer validation report](../hydrabench/2026-11-05-DOCUMENT-LAYERS.md)
for real structured/unstructured articles, a short passage and table description,
provider usage, isolated databases, and exact limitations. The subsequent
[qwen3.8-27b retry](../hydrabench/2026-11-05-DOCUMENT-LAYERS-QWEN38-27B.md)
completed real combined generation, verified all 16 source heading spans, and
records actual provider usage and remaining summary-quality caveats. The earlier
[storage and evidence-delivery audit](../hydrabench/2026-11-04-LAYER-REPAIR.md)
is a historical record of the preceding implementation.

The [manual checks](../../scripts/task6_manual_verification.md#verify-model-generation-and-source-boundaries)
and [Deep Agents tutorial](deepagents-integration-guide.md#6-upload-and-read-a-document)
exercise document and section reads. Compare character slices with `extracted.txt`,
not PDF page positions or byte offsets.
