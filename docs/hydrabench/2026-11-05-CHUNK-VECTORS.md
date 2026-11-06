# Original chunk vectors: Microarchitecture only

Date: 2026-11-05T15:29:37.320627+08:00 (Asia/Hong_Kong).

The original-vector route is implemented, but **the fixed semantic query did not
reach the intended evidence**. The exact-identifier query remains correct. This
is a functional implementation check and a recorded relevance failure, not a
claim of improved semantic recall or benchmark score.

## Implementation

- Reuse the saved source tree and natural chapter/paragraph/sentence boundaries.
  A query-independent range function now serves both lexical retrieval and
  ingestion. It covers the original once, including parent introductions, without
  indexing overlapping parent and child bodies. No LLM is used to make ranges.
- New `document_chunk_embeddings` rows store tenant, document ID/version, section
  ID, character range, source SHA-256 and pgvector embedding. The complete source
  stays in the existing file. No L0/L1 or body copies are added per chunk.
- Embedding inputs contain verified heading titles and exact source slices, not
  generated descriptions. The existing batch embedding client is reused.
- New ingestion indexes these ranges after saving sections. Configured embedding
  failures abort ingestion instead of committing a partial vector index; explicit
  NoOp ingestion remains supported without vectors. No historical backfill runs.
- Search reuses the query embedding and searches all ACL-visible, eligible
  documents independently of L0 candidates. Original vector hits do not enter the
  L1-only BM25 reranker. Version matching, source hashes, final status/version
  checks, field masks and existing original reads remain in force.
- Lexical retrieval now retains its matching ranges until range-level reciprocal
  rank fusion. Fusion keys include document, version and source range; only then
  does the coordinator select one original body per document. A different range
  cannot overwrite a previous range merely because its route ran later.
- The public search schema and DeepAgents one-body projection are unchanged.
  Existing generated L0, navigation L1 and section descriptions are untouched.
  The original tree-navigation path still exists; this change neither adds nor
  removes its generation calls.

Migration `011` adds the table and HNSW index, with tenant RLS. It was applied
**only to the new sample database**. No running service was restarted and no
migration or reindexing was applied to the historical benchmark databases.

## Fixed comparison

The source was copied from the existing generated sample, including its saved
L0 vector, version 1 and 17 section rows. The new fixture contains only this one
resource, with the same URI and exact 21,522-character original. The 16 derived
ranges include document introduction and parent text; they are not 16 newly
invented chapters.

Queries were fixed before embedding or observing new retrieval results:

1. `1986 VAX 8800`
2. `How can a CPU make progress on several instructions at once by handling different stages simultaneously?`

The before arm used the preserved pre-change source snapshot. Both arms use
identical saved query vectors, `top_k=1`, the default 3x candidate multiplier,
the same L0 vector and source, and the actual SDK/DeepAgents tool. Both use
`TreeRetriever(NoOpChatClient())`: deterministic tree fallback is available,
but there are no live tree-selection calls. This is not a comparison against
live LLM navigation. Search/read bookkeeping writes are rolled back.

| Query | Before Agent body | After Agent body | Assessment |
|---|---|---|---|
| Exact identifier | Instruction pipelining, section 10, `[9617,11920)`, 2,303 characters | Same exact body and range; keyword route | Correct evidence retained byte-for-byte. |
| Semantic paraphrase | Multiprocessing and multithreading, section 16, `[17627,20817)`, 3,190 characters | Branch prediction, section 12, `[13335,15121)`, 1,786 characters; hybrid route | Neither directly explains overlapping stages of successive instructions as well as Instruction pipelining. Desired relevance improvement not established. |

The direct vector ranks were inspected without another provider call. For the
semantic query, Instruction cycles ranked first (0.650877), Branch prediction
second (0.572757), Increasing execution speed third (0.561595), and Instruction
pipelining seventh (0.504265). The search's three-vector candidate budget excluded
the seventh result. Lexical/vector fusion then preferred Branch prediction.
Exact cosine calculations over all 16 stored vectors agreed with SQL's top three;
the observed miss is not explained by approximate-index candidate loss.
For the exact-identifier query, Instruction pipelining ranked sixth in vectors;
the keyword route preserved the correct evidence.

No alternative queries, models, candidate budgets or fusion weights were tried.
There is no general recall estimate from one source and two queries. The next
relevance decision remains open; this report does not label a shorter wrong
section as a token-efficiency gain.

## Actual usage and isolation

| Operation | Requests | Embedded inputs | Provider-reported input tokens |
|---|---:|---:|---:|
| Fixed query embeddings | 1 | 2 | 30 |
| Source chunk embeddings | 2 | 16 | 4,351 |
| Total | 3 | 18 | 4,381 |

All requests used `text-embedding-v4`. There were zero chat/completion requests,
summary generations, answer-model calls or judges. Search arms replayed the same
query vectors. Character counts and character/4 estimates are stored separately;
they are not actual answering-model tokens or cost savings.

The old 18,340-context / 18,340-vector index retained content fingerprint
`266a21457a387b68246f596c9ce494e7`. Protected historical artifacts and the prior
sample's contexts, vectors, sections and retrieval traces matched before/after
hashes. The new sample's existing document, sections and counters also remained
unchanged by indexing and search; only the new chunk table gained rows.

Two fixture setup failures were preserved: a system-Python migration invocation
failed before indexing, and the first section-read check lacked app dependencies.
They made zero extra provider calls; the partial tool message is retained.

## Validation and limits

- 179 focused tests passed; 2 database-gated tests skipped. Real PostgreSQL and
  SDK/tool checks above ran separately against the isolated fixture.
- Tests cover full source coverage, natural boundaries, original batch inputs,
  no partial writes on embedding failure, independent vector discovery with zero
  L0/keyword candidates, reuse of one query embedding, ACL before file access,
  masking, version races, source-hash rejection and range-level fusion.
- The real PostgreSQL check confirmed 16 vectors of 1,536 dimensions, forced
  tenant RLS, a non-superuser/non-bypass application role, and zero visible chunk
  rows for another tenant.
- Original section reads matched the stored source ranges. Each Agent hit has
  exactly one body plus its source URI/version/section ID.
- Each new document version needs corresponding chunk vectors. Existing documents
  are not silently indexed or migrated, and vectors tied to an older version are
  excluded. Query and index embeddings must use the same model/configuration.
- The current 4,000-character body cap is not an exact token budget; provider
  input limits still apply. The current top-k budget and single-body-per-document
  return are unchanged limitations, not validated tuning choices.

## Evidence

- [Comparison and checks](artifacts/20261105-chunk-vectors/comparison.json)
- [Exact vector ranks](artifacts/20261105-chunk-vectors/exact-vector-ranks.json)
- [Original embedding inputs and ranges](artifacts/20261105-chunk-vectors/chunk-inputs.json)
- [Stored vector metadata](artifacts/20261105-chunk-vectors/chunk-index.json)
- [Real database tenant-isolation check](artifacts/20261105-chunk-vectors/rls-check.json)
- [Query provider usage](artifacts/20261105-chunk-vectors/queries-provider-usage.json)
- [Index provider usage](artifacts/20261105-chunk-vectors/index-provider-usage.json)
- [Protected state before](artifacts/20261105-chunk-vectors/protected-before.json)
- [Protected state after](artifacts/20261105-chunk-vectors/protected-after.json)

## Actual Agent evidence bodies

The following bodies are copied in full from the actual tool messages. They are
original source slices, not generated explanations. Complete tool JSON preserves
URI, version, section ID and retrieval ID.

### Exact identifier: before and after (identical)

[Before tool JSON](artifacts/20261105-chunk-vectors/literal-agent-before.json) ·
[After tool JSON](artifacts/20261105-chunk-vectors/literal-agent-after.json)

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.
```

### Semantic query: before

[Before tool JSON](artifacts/20261105-chunk-vectors/semantic-agent-before.json)

```text
### Multiprocessing and multithreading.

Computer architects have become stymied by the growing mismatch in CPU operating frequencies and DRAM access times. None of the techniques that exploited instruction-level parallelism (ILP) within one program could make up for the long stalls that occurred when data had to be fetched from main memory. Additionally, the large transistor counts and high operating frequencies needed for the more advanced ILP techniques required power dissipation levels that could no longer be cheaply cooled. For these reasons, newer generations of computers have started to exploit higher levels of parallelism that exist outside of a single program or program thread.

This trend is sometimes known as "throughput computing". This idea originated in the mainframe market where online transaction processing emphasized not just the execution speed of one transaction, but the capacity to deal with massive numbers of transactions. With transaction-based applications such as network routing and web-site serving greatly increasing in the last decade, the computer industry has re-emphasized capacity and throughput issues.

One technique of how this parallelism is achieved is through multiprocessing systems, computer systems with multiple CPUs. Once reserved for high-end mainframes and supercomputers, small-scale (2–8) multiprocessors servers have become commonplace for the small business market. For large corporations, large scale (16–256) multiprocessors are common. Even personal computers with multiple CPUs have appeared since the 1990s.

With further transistor size reductions made available with semiconductor technology advances, multi-core CPUs have appeared where multiple CPUs are implemented on the same silicon chip. Initially used in chips targeting embedded markets, where simpler and smaller CPUs would allow multiple instantiations to fit on one piece of silicon. By 2005, semiconductor technology allowed dual high-end desktop CPUs "CMP" chips to be manufactured in volume. Some designs, such as Sun Microsystems' UltraSPARC T1 have reverted to simpler (scalar, in-order) designs in order to fit more processors on one piece of silicon.

Another technique that has become more popular recently is multithreading. In multithreading, when the processor has to fetch data from slow system memory, instead of stalling for the data to arrive, the processor switches to another program or program thread which is ready to execute. Though this does not speed up a particular program/thread, it increases the overall system throughput by reducing the time the CPU is idle.

Conceptually, multithreading is equivalent to a context switch at the operating system level. The difference is that a multithreaded CPU can do a thread switch in one CPU cycle instead of the hundreds or thousands of CPU cycles a context switch normally requires. This is achieved by replicating the state hardware (such as the register file and program counter) for each active thread.

A further enhancement is simultaneous multithreading. This technique allows superscalar CPUs to execute instructions from different programs/threads simultaneously in the same cycle.
```

### Semantic query: after

[After tool JSON](artifacts/20261105-chunk-vectors/semantic-agent-after.json)

```text
### Branch prediction.

One barrier to achieving higher performance through instruction-level parallelism stems from pipeline stalls and flushes due to branches. Normally, whether a conditional branch will be taken isn't known until late in the pipeline as conditional branches depend on results coming from a register. From the time that the processor's instruction decoder has figured out that it has encountered a conditional branch instruction to the time that the deciding register value can be read out, the pipeline needs to be stalled for several cycles, or if it's not and the branch is taken, the pipeline needs to be flushed. As clock speeds increase the depth of the pipeline increases with it, and some modern processors may have 20 stages or more. On average, every fifth instruction executed is a branch, so without any intervention, that's a high amount of stalling.

Techniques such as branch prediction and speculative execution are used to lessen these branch penalties. Branch prediction is where the hardware makes educated guesses on whether a particular branch will be taken. In reality one side or the other of the branch will be called much more often than the other. Modern designs have rather complex statistical prediction systems, which watch the results of past branches to predict the future with greater accuracy. The guess allows the hardware to prefetch instructions without waiting for the register read. Speculative execution is a further enhancement in which the code along the predicted path is not just prefetched but also executed before it is known whether the branch should be taken or not. This can yield better performance when the guess is good, with the risk of a huge penalty when the guess is bad because instructions need to be undone.
```
