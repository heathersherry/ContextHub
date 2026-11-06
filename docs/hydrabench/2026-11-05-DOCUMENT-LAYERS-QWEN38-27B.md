# Document generation retry with qwen3.8-27b

Date: 2026-11-05 (Asia/Hong_Kong).

The user replaced the unavailable generation model with `qwen3.8-27b`. The retry
used the updated local configuration and the existing validation runner with
`--live-generation`, a new empty sample database, and a new artifact directory.
Both generation and embedding returned HTTP 200. No core-code change was needed.
The earlier HTTP 403 attempt and offline results remain intact.

## Result

One real combined-generation call processed the complete 21,522-character
Microarchitecture article. Its generated introduction was stored verbatim as L0
and embedded once. It is 314 characters:

> A technical overview of microarchitecture in computer engineering, detailing its relationship to instruction set architecture (ISA), key design aspects like pipelining and execution units, and advanced concepts such as caching, branch prediction, superscalar processing, out-of-order execution, and multithreading.

The response contained 16 section descriptions. All **16/16** source heading
anchors, titles and complete spans matched the deterministic source structure;
all 16 descriptions were reused in the saved sections. L1 remains code-generated
navigation (989 characters), with section IDs and character ranges. The document
has 17 stored nodes including the root; L0/L1 are stored once on the document row.

The entire four-source check passed:

| Source | Check |
|---|---|
| Microarchitecture | Generated L0, source-derived L1, final-L0 embedding, independent original discovery, exact section/full-text reads. |
| Klaus Grutzka | No recognized headings; L1 stays NULL and original evidence remains retrievable. This probe uses NoOp generation, not an additional live model call. |
| Deep Space Habitat passage | L0/L1 NULL, complete original in L2, one evidence body in the Agent tool message. |
| Formula 1 constructors schema | L2-only benchmark schema snapshot, one evidence body, original schema readable. |

`1986 VAX 8800` appears in the article body but none of those terms appears in
its generated L0 or navigation L1. Summary discovery returned zero document
candidates; independent original-text retrieval still returned the correct source
snippet and a readable section ID. No answering model was invoked. Agent input
means the actual SDK/LangChain tool message prepared for a model.

## Content review

The introduction accurately identifies the article's topic and major mechanisms.
It is a useful document-level description for this sample. This is not evidence
of a corpus-wide retrieval improvement.

Source-position checks are stronger than semantic-summary checks. Two section
descriptions deserve caution: the branch-prediction wording can be read as saying
speculative execution should be minimized, and the register-renaming description
adds logical-to-physical register mapping not explicitly stated in that source
section. These generated descriptions remain navigation aids. Retrieved evidence
and section reads use the original text. No extra generation or judge call was
made to rewrite or grade these descriptions.

## Actual provider usage

| Operation | Model | Input tokens | Output tokens | Total tokens |
|---|---|---:|---:|---:|
| Combined document introduction + section descriptions | qwen3.8-27b | 5,460 | 1,181 | 6,641 |
| Final document L0 embedding | text-embedding-v4 | 56 | N/A | 56 |

These are provider-reported counts, not character/4 estimates. There was one
combined generation invocation and one document embedding invocation; no separate
summary-generation call, keyword-extraction call, answering call, or judging call
was added. The short passage/schema checks replayed their existing vectors for
identical text. No cost estimate or token-savings claim is made.

## Isolation and evidence

- New database: `hydra_layers_sample_20261105_qwen38_27b`, four sample contexts.
- Old index unchanged: 18,340 contexts / 18,340 embeddings, content fingerprint
  `266a21457a387b68246f596c9ce494e7` before and after.
- The original benchmark usage file, previous failed-attempt records and offline
  result files passed SHA-256 equality checks (five files).
- No full benchmark, old-index rebuild, or service restart was performed.
- Source text was selected from the same corpus as the prior sample. No benchmark
  answers or reference reasoning were used for ingestion.

Evidence:

- [Storage, Agent payloads and original reads](artifacts/20261105-document-layers-qwen38-27b/validation.json)
- [Raw generated response](artifacts/20261105-document-layers-qwen38-27b/ingestion-response.json)
- [Actual provider usage](artifacts/20261105-document-layers-qwen38-27b/provider-usage.json)
- [Generation/source review and preserved-record hashes](artifacts/20261105-document-layers-qwen38-27b/generation-review.json)
- [Complete source article](artifacts/20261105-document-layers-qwen38-27b/microarchitecture.md)
- [Earlier implementation and failed-attempt report](2026-11-05-DOCUMENT-LAYERS.md)

This closes the earlier live-generation availability check for this document.
It does not replace the prior implementation regression results or establish
summary quality, recall, ranking, or latency at full-corpus scale.
