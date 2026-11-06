# Read-only audit of Microarchitecture chunk ranking

Date: 2026-11-05T15:46:01.821416+08:00 (Asia/Hong_Kong).

Scope: the same one stored document, two saved query vectors and 16 stored chunk
vectors. **Zero model/provider calls, zero index/data mutations and zero product
code changes.** Official provider documentation was consulted; no provider
inference endpoint was called. Historical experiment artifacts are unchanged.

## Findings

The seventh-place semantic rank is the exact cosine rank of the saved vectors.
No wrong original slice, summary substitution, omitted pipeline paragraph,
non-finite vector, duplicate chunk vector, mixed effective dimensions, cosine
implementation error or HNSW candidate-loss explanation was found.
This does **not** prove that the provider's representation or the configuration
is optimal, nor does it fully reconstruct the historical provider response.

The implementation has issues worth separating from that seventh-place rank:

1. `EMBEDDING_DIMENSIONS=1536` is passed only as a local expected/storage size.
   The factory does not pass the client's API `dimensions` argument. All saved
   query/chunk vectors have a nonzero 1,024-dimensional prefix and exactly 512
   trailing zeros, consistent with the client's padding and the provider's default
   1,024-dimensional output. This is a configuration-contract mismatch, not genuine
   1,536-dimensional embedding. Removing the padding changed none of the 32
   query/chunk cosine scores; maximum difference was exactly zero.
2. The chunk candidate limit is `request.top_k * over_retrieve_factor`:
   `1 * 3 = 3`. The same multiplier was inherited from document retrieval.
   The coordinator's default of 30 is overridden by the explicit value 3.
   Requested document return count and internal chunk recall budget are coupled
   without a chunk-specific rationale. This is not a model or corpus limit.
3. Original lexical ranking counts literal substrings and density, not BM25.
   `once` matches `concepts`; `can` matches `significantly`. Such matches contribute
   to scores even though they are not those words. Inflections such as `make`
   matching `makes` happen accidentally through the same mechanism.
4. RRF itself reproduces correctly, but combines route ranks rather than cosine
   magnitude. A range found by both routes can outrank a stronger vector-only hit.
   The lexical list contains all 16 matching ranges for the semantic query while
   the vector list is truncated to 3. Equal scores keep the first inserted range,
   which currently means the lexical route wins a cross-route tie.
5. Prior usage capture retained request input hashes/counts and usage, but did not
   retain response model/version, returned per-item indices or vector hashes. The
   batch client correctly restores reversed response arrays by their `index` in a
   local mock using saved values. That verifies the code, not the exact unrecorded
   historical provider response. This evidence gap prevents an absolute claim
   that every historical response-to-row mapping has been replay-verified.

## Model and input audit

The saved request records all specify `text-embedding-v4`, not the chat model
`qwen3.8-27b`. Current configuration points to
`dashscope-intl.aliyuncs.com/compatible-mode/v1`; saved records confirm the
`/compatible-mode/v1/embeddings` request path. The runner reads the same explicit
configuration keys for query and document embedding. No generation client is
used by the new chunk indexer.

The factory/client construct requests with `model` and `input`. They do not set
`dimensions`, `text_type` or `instruct`. Alibaba's documentation lists 1,024 as
v4's default dimension and documents query/document roles and retrieval
instructions under the native DashScope API, not as supported OpenAI-compatible
fields. The current path therefore does not explicitly enable query-role
optimization. Whether changing that would improve this query cannot be inferred
from already computed vectors; this audit did not call a model to test it.

All 16 saved embedding-input strings were reconstructed from the saved tree and
exact original offsets. Their order, lengths and SHA-256 values match the actual
request-input hashes captured for batches of 10 and 6. Query strings likewise
match the recorded hashes and saved query-vector keys. All vectors are finite,
nonzero and approximately unit length; all 16 chunk vectors are distinct.

For Instruction pipelining, the exact input is 2,379 characters:

```text
Microarchitecture > Microarchitectural concepts. > Instruction pipelining.

### Instruction pipelining.

[the complete 2,303-character original section follows, without truncation]
```

The bracketed line above describes the format; it was **not** in the model input.
The [complete actual input](artifacts/20261105-chunk-ranking-audit/pipelining-actual-embedding-input.txt)
contains the paragraph explaining simultaneous in-flight instructions, followed
by RISC implementation details and SPARC/MIPS/VAX history. No generated L0/L1 or
section summary is included. Instruction cycles has only 508 input characters.
A whole-section representation may be less focused on the relevant paragraph;
this is an inference from the text composition, not a proved explanation of the
model's internal similarity function. Testing paragraph-only vectors would
require new embeddings and was deliberately not done.

Python double-precision cosine calculations and PostgreSQL's stored-vector
scores agree in order; maximum numeric difference was about 1.23e-7. The gap
around the seventh position is much larger. Scoring only the 1,024-dimensional
prefix yields exactly the same values as the padded 1,536-dimensional vectors.

## Candidate truncation and range fusion

The semantic query is unchanged:

> How can a CPU make progress on several instructions at once by handling different stages simultaneously?

The query's lexical terms are `can`, `cpu`, `make`, `progress`, `several`,
`instructions`, `once`, `handling`, `different`, `stages`, `simultaneously`.
The pipeline section matches only `cpu`, `make` (via `makes`), and `instructions`
under the current literal-substring scoring, placing it tenth lexically.

**Instruction pipelining was not entirely absent from fusion.** It was absent
from the three-vector list but still present in the lexical list at rank 10.

For each route that contains a range, the score adds `1 / (60 + route_rank)`:

- Branch prediction: vector rank 2 + lexical rank 2 = `2/62 = 0.03225806`.
- Instruction cycles: vector rank 1 + lexical rank 15 = `1/61 + 1/75 = 0.02972678`.
- Instruction pipelining: no vector contribution + lexical rank 10 =
  `1/70 = 0.01428571`.

The last column below is **arithmetic using the already saved full ranking**, not
an executed search with changed configuration, a parameter sweep or a new
benchmark. It asks whether removing only the vector truncation would change the
winner. It would not: Branch prediction still wins; pipelining would score
`1/67 + 1/70 = 0.02921109`.

| Section | Vector rank | Cosine | Lexical rank | Current RRF (vector top 3) | RRF without vector truncation, arithmetic only |
|---|---:|---:|---:|---:|---:|
| Instruction cycles. | 1 | 0.650877 | 15 | 0.02972678 | 0.02972678 |
| Branch prediction. | 2 | 0.572757 | 2 | 0.03225806 | 0.03225806 |
| Increasing execution speed. | 3 | 0.561595 | 9 | 0.03036577 | 0.03036577 |
| Out-of-order execution. | 4 | 0.551905 | 16 | 0.01315789 | 0.02878289 |
| Multicycle Microarchitecture. | 5 | 0.535633 | 6 | 0.01515152 | 0.03053613 |
| Multiprocessing and multithreading. | 6 | 0.507114 | 1 | 0.01639344 | 0.03154496 |
| Instruction pipelining. | 7 | 0.504265 | 10 | 0.01428571 | 0.02921109 |
| Superscalar. | 8 | 0.500491 | 4 | 0.01562500 | 0.03033088 |
| Register renaming. | 9 | 0.496806 | 8 | 0.01470588 | 0.02919864 |
| Aspects of microarchitecture. | 10 | 0.495890 | 3 | 0.01587302 | 0.03015873 |
| Instruction set choice. | 11 | 0.453049 | 5 | 0.01538462 | 0.02946912 |
| Relation to instruction set architecture. | 12 | 0.445537 | 7 | 0.01492537 | 0.02881426 |
| Cache. | 13 | 0.414749 | 11 | 0.01408451 | 0.02778314 |
| Microarchitecture | 14 | 0.388683 | 14 | 0.01351351 | 0.02702703 |
| See also. | 15 | 0.352901 | 13 | 0.01369863 | 0.02703196 |
| Microarchitectural concepts. | 16 | 0.325472 | 12 | 0.01388889 | 0.02704678 |

For `1986 VAX 8800`, pipelining is vector rank 6 and the sole lexical hit. Its
current score is `1/61`, tied with the first vector hit (Multicycle
Microarchitecture). The coordinator returns pipelining because the lexical
range was inserted first and replacement requires a strictly higher score.
Without vector truncation, the same saved ranking gives pipelining an additional
`1/66`; it remains the winner. Exact-identifier success should not be described
as proof of calibrated hybrid relevance.

## The other ranking stages

The complete read-only stage trace is saved separately:

- L0 cosine scores are 0.248850 for the exact query and 0.514289 for the semantic
  query. The single eligible document is recalled in both cases.
- Existing L1 BM25 assigns zero to both queries: the navigation text does not
  contain those exact token forms. This is a document-level behavior upstream of
  chunk discovery; it does not cause the seventh-place chunk cosine rank.
- Deterministic NoOp tree navigation picks pipelining for the exact query and
  Multicycle Microarchitecture for the semantic query. No live chat is invoked.
- The original route selects its range before outer document-level fusion.
  The outer fusion preserves that body; it does not compare paragraph relevance
  against the tree snippet. Both document lists contain this one document at rank
  1, so both final Agent scores are `2/61 = 0.03278689`. This final score is a
  document rank-fusion value, not the snippet's cosine or confidence.

No hidden LLM reranker or generated chapter description participated in the new
chunk-vector scoring. Existing summaries are still consumed by the separate
NoOp tree heuristic, as before.

## Smallest changes to discuss, not applied

- Make the requested embedding dimension explicit and enforce its contract;
  distinguish it from storage width in records. Do not silently start mixing true
  1,536-dimensional embeddings with the current padded 1,024-dimensional index.
  Any representation change needs a separately identified index and consistent
  query/document embeddings. This audit does not rebuild it.
- Give chunk recall its own internal candidate budget, independent of the final
  number of returned documents. Do not choose 7, 8 or another value solely to make
  this question pass. Record both lexical and vector candidate depths.
- Fix token matching consistency for word-based text, rather than accepting
  arbitrary substrings as words. Treat morphological normalization explicitly;
  do not tune the stopword list around this query. This need not introduce a new
  search engine or LLM.
- Record response model/dimensions and input-index/vector correspondence so that
  future response-to-storage audits are reproducible. Make tie behavior explicit.
- Keep the RRF formula unchanged pending broader evidence. Widening candidates
  alone demonstrably does not solve this case. Query-role encoding, finer
  embedding granularity, BM25 retrieval or a stronger reranker are separate
  relevance options, not justified fixes to silently introduce here.

## Evidence

- [Detailed audit, dimensions, inputs and ranks](artifacts/20261105-chunk-ranking-audit/audit.json)
- [Full stage trace](artifacts/20261105-chunk-ranking-audit/stages.json)
- [Current sanitized configuration](artifacts/20261105-chunk-ranking-audit/current-config-redacted.json)
- [Original two-query result](2026-11-05-CHUNK-VECTORS.md)
- [Provider API: dimensions and response indices](https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api)
- [Provider guide: query/document roles and instructions](https://www.alibabacloud.com/help/en/model-studio/embedding)
