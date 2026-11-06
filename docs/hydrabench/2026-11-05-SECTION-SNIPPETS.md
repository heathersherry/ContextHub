# Source-section-bounded keyword evidence

Date: 2026-11-05, Asia/Hong_Kong.

The original keyword route found the right evidence but selected a character
window spanning several chapters and cutting words at both ends. It now selects
within existing source-section ranges before scoring snippets. This is a local
selection fix; document ingestion, L0/L1, vectors, the generic API and the Agent
adapter are unchanged.

## Selection behavior

- Read the already stored section ranges for a document with raw keyword hits.
- Skip document-wide root/title wrappers as evidence groups. Return a complete
  chapter when it fits the existing 4,000-character limit; otherwise descend into
  its child sections. Do not merge evidence across sibling chapters.
- Keep parent introductions and gaps between child sections searchable.
- For oversized ranges without usable smaller children, pack complete paragraphs,
  then sentences, then whitespace-delimited words. A single unbroken run longer
  than the limit still requires a bounded character split. Sentence detection
  uses punctuation rules, not a model.
- Retain exact source offsets and resolve the selected range to its enclosing
  stored section ID. Existing keyword scores and downstream rank fusion remain.

The implementation replaces the old arbitrary hit-centered window/merge path in
`src/contexthub/retrieval/long_doc/keyword_retriever.py`. It does not add a parallel
compatibility path or another section index.

## One existing query, before and after

Source: Microarchitecture, Wikipedia ID 1410175, 21,522 characters.

- Database: `hydra_layers_sample_20261105_qwen38_27b` (the same existing sample DB).
- URI: `ctx://resources/hydra-layer-validation/microarchitecture`, version 1.
- Query: `1986 VAX 8800`; `context_type=[resource]`, `top_k=1`, `level=L1`.
- Before: the preserved actual SDK/DeepAgents ToolMessage from the qwen3.8 run.
- After: one new SDK/DeepAgents ToolMessage against the same source and database.
- No new corpus or query cases. No answering model was invoked.

| Item | Before | After |
|---|---|---|
| Returned section ID | 5, Microarchitectural concepts | 10, Instruction pipelining |
| Exact character interval | `[10291, 13636)` | `[9617, 11920)` |
| Evidence body characters | 3,345 | 2,303 |
| Serialized tool payload characters | 3,896 | 2,843 |
| Beginning | `uld allow up to four instructions...` | `### Instruction pipelining.` followed by its opening paragraph |
| Ending | `...depend on results comin` | Complete final paragraph ending `...on which they are based.` |
| Adjacent chapters in body | Cache plus part of Branch prediction | None |

All query terms remain in the returned original text. The new body equals the
entire stored section 10 slice, including its heading and original whitespace.
The result still contains exactly one `content` field, URI, version and section
ID; it does not append copies of L0/L1. The Agent adapter itself was not modified.

Exact content and metadata:

- [Before Agent body](artifacts/20261105-section-snippets/content-before.txt)
- [After Agent body](artifacts/20261105-section-snippets/content-after.txt)
- [Before full tool result](artifacts/20261105-section-snippets/agent-before.json)
- [After full tool result](artifacts/20261105-section-snippets/agent-after.json)
- [Comparison and database fingerprints](artifacts/20261105-section-snippets/comparison.json)
- [Single-query validation runner](../../integrations/hydrabench/validate_section_snippets.py)

## Validation and preservation

Provider generation calls: **0**. Provider embedding calls: **0**. As in the
baseline, query embedding was disabled; this check isolates the raw keyword path.
There is no new actual model-usage measurement and no claim about token or cost
savings. Payload character/4 estimates in the JSON are explicitly heuristic.

The validation uses transactions that roll back normal search bookkeeping writes.
Before/after hashes match for all rows in `contexts` (including vectors and
counters), `document_sections`, and `retrieval_trace`. Source bytes and the saved
baseline artifact also remain identical. No index rebuild, re-ingestion, schema
change, service restart or broad benchmark occurred.

Focused regression suite: **112 passed, 1 skipped** (the existing integration
fixture is gated). The real PostgreSQL/SDK/tool check above ran separately. New
regressions cover complete fitting chapters, oversized parents with child
chapters, parent introductions, paragraph/sentence/word boundaries and Unicode
text. Existing ACL, version-barrier and one-body Agent output checks also passed.

Existing worktree modifications were retained. The pre-change snapshot for this
step is `/private/tmp/contexthub-section-snippets-before-20261105`.
