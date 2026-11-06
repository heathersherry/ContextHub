# Minimal document-layer and original-discovery repair

Date: 2026-11-05 (Asia/Hong_Kong). Worktree: `hydra-smoke/ContextHub`.
This is a small-source implementation check, not a benchmark run or a recall study.
Existing uncommitted work was retained. The pre-change snapshot for this step is
`/private/tmp/contexthub-document-fix-before-20261105`.

## Scope and design boundary

The original information model assigns L0 to discovery, L1 to navigation/reranking,
and L2 to detail. The long-document design adds one file-backed resource with
section ranges in PostgreSQL. These designs do not require copying a document's
L0/L1 into every section. The HyDRA loader's former L0=L1=L2 copies were an
experimental import shortcut, separate from the product document ingester's
80/300-character prefix problem.

The previous [layer audit](2026-11-04-LAYER-REPAIR.md) introduced raw-only short
contexts and one-body Agent output. This change completes the agreed document
path without requiring summaries for ordinary paragraphs or table descriptions.
No universal length threshold was introduced: a standalone source unit may stay
inline; a complete uploaded article uses file-backed document ingestion. Source
structure, not length alone, determines whether L1 navigation exists.

## Implemented changes

| Area | Final behavior |
|---|---|
| Document storage | One context row with L0, optional L1, URI, version, tags and file path; complete extracted source in a file; SQL L2 NULL. |
| L0 generation | The existing bounded, full-source generation call also returns `description`. Embed the final description once. No extra summary call. |
| Generation unavailable | Explicit `Document: <filename>` metadata fallback; never a body prefix mislabeled as a summary. NoOp ingestion now works. |
| L1 generation | Render verified source headings, hierarchy, persisted section IDs and character ranges in code. No recognized headings means NULL. |
| Section structure | Source headings are authoritative. Model descriptions are accepted only for matching heading/span pairs. Paragraph anchors alone do not create chapters. |
| Large sections | Preserve source heading identity/range; add bounded child ranges for oversized leaves. Large parents can still span multiple sections. |
| Raw discovery | Scan every eligible, ACL-visible file-backed resource independently of L0/vector candidates. No first-N document gate. |
| Keyword selection | Literal query terms, Unicode and numbers included; no model keyword extraction or first-four-term truncation. Score all generated windows before selection. |
| Ranking | Standalone raw score; reciprocal rank fusion with the existing route using `1/(60+rank)`; deduplicate exact context/version; retain located original evidence on overlap. |
| Common checks | Tenant/type/scope/status filtering and ACL before file reads; shared quality, masking and final version/validity barriers afterward. Table-only requests skip document scanning. |
| Agent output | Reuse the existing single `content` projection. L1 searches prefer the original snippet. URI/version/section ID and original reads are preserved. |

The superseded conditional keyword fallback inside tree navigation was removed.
Original-only discoveries do not trigger additional tree-model calls and are not
reranked solely on summaries. Public materialized trace content now receives the
same masking as result content; character offsets are retained in that trace.
The generic search result schema, SQL schema and existing document/section APIs
were not changed.

Unchanged table handling matters: the production catalog/schema generator still
uses its structured metadata path. The benchmark schema snapshot stays L2-only;
business data remains in original SQLite tables. Neither path is treated as an
article needing chapter generation.

## Validation evidence

The complete offline check uses real KILT source text and the real indexed table
schema. It exercises PostgreSQL, services, HTTP ASGI transport, SDK, native
DeepAgents/LangChain ToolMessages, section reads and full original reads. No
answering or judging model was invoked; “Agent input” below is the actual tool
message prepared for the agent, not a completed model run.

- [Three-source storage and Agent payloads](artifacts/20261105-document-layers-offline/validation.json)
- [Unstructured article supplement](artifacts/20261105-document-layers-offline/unstructured-validation.json)
- [Structured source article](artifacts/20261105-document-layers-offline/microarchitecture.md)
- [Unstructured source article](artifacts/20261105-document-layers-offline/klaus-grutzka.txt)
- [Validation runner](../../integrations/hydrabench/validate_document_layers.py)

| Real source | Original characters | Saved L0 / L1 / inline L2 characters | Result |
|---|---:|---|---|
| Deep Space Habitat, source passage `35872624/0006` | 505 | 0 / 0 / 505 | Raw evidence retains Orion, MMSEV and the duration; only one body reaches the tool message. |
| `formula_1.constructors` schema | 857 | 0 / 0 / 857 | Full schema text remains searchable/readable; SQLite business rows are not copied. |
| Microarchitecture, source article `1410175` | 21,522 | 27 / 989 / 0 | Metadata-only offline L0; navigation for source headings; 17 stored nodes; entire original in the file. |
| Klaus Grutzka, source article `31331172` | 6,893 | 23 / 0 / 0 | Naturally unstructured source, L1 NULL, two sequential range nodes, full source readable. |

For Microarchitecture, `1986 VAX 8800` occurs in the original but none of the query
terms occurs in L0/L1. Summary discovery returns no candidate for that document;
the independent original route returns it with a real section ID and this exact
source evidence:

> By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design

The evidence window crosses child headings, so its smallest enclosing readable
section is a larger parent (`4924:20817` in extracted-source character offsets).
The search snippet remains bounded. The subsequent section read and full L2 read
were compared byte-for-byte as decoded source text against the corresponding
character slices/original. This checks location integrity, not whether a model
would answer a benchmark question correctly.

For Klaus Grutzka, `Sybil Lancaster 2011` retrieves the closing biographical
paragraph with L1 still NULL. Sequential read ranges do not appear as invented
chapter navigation. This source was a new probe; no historical “before” payload
is claimed for it.

The ingestion regression with a scripted model verifies that one existing call
supplies both document description and section descriptions, and that exactly
the final description is embedded. It also checks that fabricated paragraph
chapters do not become L1, code-fence examples are not headings, Unicode source
coordinates remain correct, and heading IDs survive body splitting.

Retrieval regressions cover an available embedding route returning no candidates,
late-window evidence beyond the former first-five-window limit, numeric/Unicode
terms, ACL filtering before file access, version changes, duplicate route hits,
masking and table-only requests.

Final targeted suite: **225 passed, 15 skipped**. The skips are the repository's
gated integration fixtures; the separate isolated PostgreSQL checks above did
run. Suite includes ingestion, long-document retrieval, generic retrieval,
raw-only contexts, document API, lifecycle, SDK, HyDRA adapter and DeepAgents
tests. No full benchmark or general recall-quality evaluation was run.

## Model calls and usage

No additional standalone summary call was introduced. A normal configured
ingestion uses its existing one combined chat call and one final-L0 embedding.
Retries, if any, are separate HTTP attempts. L1 rendering and original-text
keyword retrieval make zero model calls. Existing tree navigation on already
recalled documents retains its existing model behavior.

A live sample was attempted before the offline check:

- One configured chat request returned **HTTP 403 Forbidden**. The provider
  returned no usage block. This is not evidence of zero billed tokens.
- Ingestion then used the explicit filename fallback and source-heading L1.
  One embedding request succeeded; provider usage reports **5 input tokens**.
- The live validation intentionally stopped at the assertion requiring a
  successfully generated introduction. The partial sample database and artifacts
  were retained. No provider retry or replacement-model request was made.
- The subsequent four-source offline checks made **zero provider calls**. Exact
  existing vectors were replayed only for unchanged passage/schema text. No old
  document-prefix vector was reused for a new introduction.

See [provider responses and usage](artifacts/20261105-document-layers/provider-usage.json)
and [attempt status / final fingerprints](artifacts/20261105-document-layers/attempt-status.json).
The successful real-model introduction path still needs a small follow-up once
the configured endpoint accepts the request. Its call wiring is regression-tested;
its generated-text quality is not established by this run.

Serialized Agent payload sizes (Unicode characters, not model tokens):

| Source | Before | After | Character/4 heuristic before → after |
|---|---:|---:|---|
| Short passage | 1,470 | 960 | 367.5 → 240 |
| Table description | 2,211 | 1,328 | 552.75 → 332 |
| Structured article | 4,964 | 3,896 | 1,241 → 974 |
| New unstructured probe | N/A | 1,713 | N/A → 428.25 |

The article's before payload is the preserved prior sample under a different
query; it only illustrates storage/response shape. It is not a paired token
savings result. Character/4 is a rough estimate, not a tokenizer count or provider
usage. No answering-model token or cost reduction is claimed.

## Isolation and reproducibility

- Old index `hydra_retrieval_20261104`: unchanged at **18,340 contexts / 18,340
  embeddings**, content fingerprint `266a21457a387b68246f596c9ce494e7`.
- Live partial sample: `hydra_layers_sample_20261105_documents` (three contexts).
- Completed offline sample: `hydra_layers_sample_20261105_documents_offline`
  (four contexts; the initial three-source JSON and separate supplement are both
  retained without rewriting the initial result).
- Original `20261104-paper-metrics/token-usage.json` SHA-256 remains
  `3bdfcbdabd5c704ac1a1db1a0ce9e9458bfefe75435eb02fb4356e7622ace971`.
- No benchmark answers or reference reasoning were used to build indexed content.
  Queries were selected directly from public source text to test specific paths.

The runner requires an empty dedicated `hydra_layers_sample_*` database seeded
with schema and team memberships, plus separate source/read-only and sample DSNs.
Use a new database and a new output directory for each run. By default it stays
offline; `--live-generation` explicitly enables the single existing generation
and document-embedding path. It refuses to reuse an output directory. Source
identifiers, URLs, hashes, URIs and versions remain available in artifacts/storage.

## Deferred work

The raw route currently performs linear file scanning. Inverted indexing,
section embeddings, broader heading extraction, query segmentation, summary
quality and fusion tuning are deferred until supported by measurements. Literal
raw recall is useful for source names/numbers; it is not a substitute for semantic
retrieval. No per-paragraph summaries or automatic index migration were added.

Any later replacement of document L0 requires regeneration of that document's
vector in a separate index/import. L1-only navigation changes do not. No historical
index was rebuilt, and the running old experiment service was not restarted.
