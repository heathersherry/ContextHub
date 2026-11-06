# Fixed vector 0.8 / BM25 0.2 fusion: saved-vector replay

Date: 2026-11-06T11:17:27.598908+08:00 (Asia/Hong_Kong).

The semantic query now returns the complete Instruction pipelining section at
rank 2 (previously fused rank 7, outside top 5). The exact query still returns
the same five original bodies, but pipelining falls from rank 1 to rank 3;
See also becomes the first result. This is a tradeoff, not a uniform relevance
improvement. No answering model or benchmark accuracy measurement is involved.
The weights were fixed at the user's requested 0.8/0.2 before this replay and
were not selected, swept or adjusted using either query's result.

## Boundaries and implementation

- Same Microarchitecture source, 21,522 characters, version 1, URI
  `ctx://resources/hydra-layer-validation/microarchitecture`.
- Same isolated database `hydra_originals_qwen37_20261105`: one document and
  sixteen existing original-range vectors. Both query vectors are loaded from
  the preserved Qwen3.7 artifact, using `qwen3.7-text-embedding`, 1,536 dimensions.
- Final `top_k=5`, vector discovery budget 15, original ranges, keyword tokenizer,
  BM25 formula/parameters, and the sixteen-fragment BM25 statistics corpus remain
  unchanged. No index or schema is created, rebuilt or updated.
- As in the preceding BM25 replay, this isolates the original-evidence route:
  document L0 recall, L1 reranking and tree navigation are disabled in the harness.
  The separate outer merge with other context types retains its existing RRF.
  This change is not a replacement of every ranking stage in ContextHub.
- `LongDocRetrievalCoordinator.search_originals` keeps the union of vector
  discovery and positive BM25 candidates, keyed by document, version and exact
  source range. It then combines actual scores rather than reciprocal ranks.
- `ChunkVectorRetriever.score_candidates` looks up cosine scores for lexical
  candidates absent from vector top 15, using only their existing stored vectors.
  Exact range, tenant, ACL-filtered document IDs, document version and source-hash
  checks apply. The same source materialization checks serve retrieval and scoring.
  This is score completion within the existing union, not new candidate discovery.
- BM25 already evaluates all sixteen source fragments and emits every positive
  match, so a vector-only candidate has a genuine BM25 score of zero. No L0/L1
  substitute, candidate-local BM25 recomputation or model call is needed.
- Normalize each route over the same candidate union with `(s-min)/(max-min)`;
  a constant or empty route contributes zero ranking distinction. Then use:

```text
final_score = 0.8 * normalized_cosine + 0.2 * normalized_bm25
```

The sample has valid stored vectors for every union candidate. In the general
product path, a missing/stale vector contributes no vector evidence and is
excluded from cosine min/max; it is not fabricated as a cosine of zero. Its
lexical evidence remains available. A request without a query vector continues
to use the lexical route. Missing-vector and constant-score cases have focused
unit tests; they are not additional benchmark queries or provider calls.

## Candidate completion and normalization

Both phases have 15 union candidates for the exact query and 16 for the semantic
query. Vector discovery still returns exactly 15 candidates per query. All raw
vector candidate scores and all BM25 candidate scores match before/after exactly.
BM25 continues to use all 16 fragments (3,318 lexical tokens, mean 207.375);
these lexical token counts are not model token measurements.

Only one stored-vector lookup was needed: Cache (section 11, range
`[11920,13335)`) was already a semantic-query BM25 candidate, but was outside
vector top 15. Its existing-vector cosine is `0.5309904096416007`, not zero.
Completing it leaves the candidate union unchanged and supplies its true score
to the common normalization range. The exact query requires no completion.

| Query | Cosine min → max in union | BM25 min → max in union |
|---|---|---|
| literal | 0.316149644 → 0.423227533 | 0.000000000 → 7.804434425 |
| semantic | 0.530990410 → 0.698152703 | 0.000000000 → 6.524940742 |

## Actual Agent result order

### Exact query

Query: `1986 VAX 8800`

| Rank | Before: BM25 + equal-weight RRF | After: fixed normalized 0.8/0.2 |
|---|---|---|
| 1 | Instruction pipelining. (10) | See also. (17) |
| 2 | See also. (17) | Superscalar. (13) |
| 3 | Superscalar. (13) | Instruction pipelining. (10) |
| 4 | Increasing execution speed. (8) | Increasing execution speed. (8) |
| 5 | Out-of-order execution. (14) | Out-of-order execution. (14) |

### Semantic paraphrase

Query: `How can a CPU make progress on several instructions at once by handling different stages simultaneously?`

| Rank | Before: BM25 + equal-weight RRF | After: fixed normalized 0.8/0.2 |
|---|---|---|
| 1 | Multiprocessing and multithreading. (16) | Increasing execution speed. (8) |
| 2 | Aspects of microarchitecture. (4) | Instruction pipelining. (10) |
| 3 | Increasing execution speed. (8) | Multiprocessing and multithreading. (16) |
| 4 | Superscalar. (13) | Superscalar. (13) |
| 5 | Branch prediction. (12) | Aspects of microarchitecture. (4) |

The semantic result set replaces Branch prediction with Instruction pipelining.
Increasing execution speed, Multiprocessing and multithreading, Superscalar and
Aspects remain. Both phases already included some pipeline evidence in Aspects;
the change adds the dedicated chapter rather than turning an evidence-free
response into an evidence-bearing response.

The exact query shows the cost of the requested policy: the vector-first ranking
puts See also and Superscalar ahead of the direct VAX/year evidence. BM25 still
raises pipelining from its pure-vector rank 5 to fused rank 3, but no longer to
rank 1. There is no claim that this fixed weight is optimal or universally better.
Min/max normalization depends on the candidate set; these candidate budgets
were held fixed rather than tuned to the two results.

## Complete candidate scores

Before and after final scores are on different scales. The after score is neither
a probability nor a measured relevance/answer-confidence value. The following
lists every union candidate in after-order. A dash in vector discovery rank means
it was not in vector top 15; a completed cosine can still be present.

### literal

| Section | Vector discovery rank | Cosine | BM25 | Normalized vector | Normalized BM25 | Weighted score | Final rank before → after |
|---|---:|---:|---:|---:|---:|---:|---:|
| See also. (17) | 1 | 0.423227533 | 0.000000000 | 1.000000000 | 0.000000000 | 0.800000000 | 2 → 1 |
| Superscalar. (13) | 2 | 0.416058414 | 0.000000000 | 0.933047626 | 0.000000000 | 0.746438101 | 3 → 2 |
| Instruction pipelining. (10) | 5 | 0.385510637 | 7.804434425 | 0.647762054 | 1.000000000 | 0.718209644 | 1 → 3 |
| Increasing execution speed. (8) | 3 | 0.395687336 | 0.000000000 | 0.742802209 | 0.000000000 | 0.594241767 | 4 → 4 |
| Out-of-order execution. (14) | 4 | 0.387639503 | 0.000000000 | 0.667643526 | 0.000000000 | 0.534114821 | 5 → 5 |
| Multiprocessing and multithreading. (16) | 6 | 0.381761338 | 0.000000000 | 0.612747364 | 0.000000000 | 0.490197891 | 6 → 6 |
| Cache. (11) | 7 | 0.376351274 | 0.000000000 | 0.562222801 | 0.000000000 | 0.449778241 | 7 → 7 |
| Instruction set choice. (9) | 8 | 0.365678811 | 0.000000000 | 0.462552707 | 0.000000000 | 0.370042166 | 8 → 8 |
| Microarchitecture (2) | 9 | 0.355821759 | 0.000000000 | 0.370497728 | 0.000000000 | 0.296398183 | 9 → 9 |
| Aspects of microarchitecture. (4) | 10 | 0.345226902 | 0.000000000 | 0.271552406 | 0.000000000 | 0.217241925 | 10 → 10 |
| Relation to instruction set architecture. (3) | 11 | 0.345196126 | 0.000000000 | 0.271264984 | 0.000000000 | 0.217011987 | 11 → 11 |
| Microarchitectural concepts. (5) | 12 | 0.339487373 | 0.000000000 | 0.217950960 | 0.000000000 | 0.174360768 | 12 → 12 |
| Register renaming. (15) | 13 | 0.333057245 | 0.000000000 | 0.157900018 | 0.000000000 | 0.126320015 | 13 → 13 |
| Multicycle Microarchitecture. (7) | 14 | 0.329540044 | 0.000000000 | 0.125052898 | 0.000000000 | 0.100042319 | 14 → 14 |
| Instruction cycles. (6) | 15 | 0.316149644 | 0.000000000 | 0.000000000 | 0.000000000 | 0.000000000 | 15 → 15 |

### semantic

| Section | Vector discovery rank | Cosine | BM25 | Normalized vector | Normalized BM25 | Weighted score | Final rank before → after |
|---|---:|---:|---:|---:|---:|---:|---:|
| Increasing execution speed. (8) | 1 | 0.698152703 | 1.993645980 | 1.000000000 | 0.305542389 | 0.861108478 | 3 → 1 |
| Instruction pipelining. (10) | 2 | 0.697435340 | 1.594242278 | 0.995708585 | 0.244330537 | 0.845432976 | 7 → 2 |
| Multiprocessing and multithreading. (16) | 6 | 0.660363535 | 6.524940742 | 0.773937247 | 1.000000000 | 0.819149798 | 1 → 3 |
| Superscalar. (13) | 4 | 0.676703493 | 3.603604324 | 0.871686317 | 0.552281540 | 0.807805362 | 4 → 4 |
| Aspects of microarchitecture. (4) | 7 | 0.652911444 | 5.718463686 | 0.729357273 | 0.876400861 | 0.758765991 | 2 → 5 |
| Instruction cycles. (6) | 3 | 0.686336776 | 0.000000000 | 0.929314641 | 0.000000000 | 0.743451713 | 14 → 6 |
| Out-of-order execution. (14) | 5 | 0.662050944 | 1.855800101 | 0.784031682 | 0.284416392 | 0.684108624 | 8 → 7 |
| Branch prediction. (12) | 8 | 0.638898191 | 4.043824926 | 0.645527048 | 0.619748912 | 0.640371421 | 5 → 8 |
| See also. (17) | 9 | 0.626034301 | 1.909356708 | 0.568572548 | 0.292624375 | 0.513382914 | 12 → 9 |
| Multicycle Microarchitecture. (7) | 10 | 0.599855459 | 4.103378872 | 0.411965209 | 0.628876036 | 0.455347374 | 6 → 10 |
| Instruction set choice. (9) | 11 | 0.590205115 | 3.026083526 | 0.354234825 | 0.463771802 | 0.376142220 | 11 → 11 |
| Register renaming. (15) | 13 | 0.574543016 | 3.669611219 | 0.260540855 | 0.562397631 | 0.320912210 | 9 → 12 |
| Microarchitectural concepts. (5) | 12 | 0.589285416 | 0.000000000 | 0.348732988 | 0.000000000 | 0.278986391 | 16 → 13 |
| Relation to instruction set architecture. (3) | 14 | 0.559568031 | 3.644175352 | 0.170957341 | 0.558499379 | 0.248465749 | 10 → 14 |
| Microarchitecture (2) | 15 | 0.540595612 | 1.717942889 | 0.057460341 | 0.263288658 | 0.098626004 | 13 → 15 |
| Cache. (11) | — | 0.530990410 | 3.027576443 | 0.000000000 | 0.464000604 | 0.092800121 | 15 → 16 |

## Validation, preservation and size

- Actual local PostgreSQL → FastAPI → SDK → DeepAgents tool execution: two fixed
  queries in each phase, four search calls and twenty section reads in total.
  Both phases completed without a retrieval retry. HTTP is restricted to the
  local ASGI transport; no model/provider client is constructed. Queries reuse
  saved vectors, and all database bookkeeping writes roll back.
- Every returned body equals its exact original character slice and lies inside
  the section returned by the existing section-read endpoint. Each response has
  five distinct document/version/range keys. Source URI, version, section ID and
  character offsets are retained; L0/L1/full-body copies are not appended.
- Before outputs exactly reproduce the prior BM25 replay. After vector and BM25
  discovery lists match before exactly, as does the candidate union. The one
  additional cosine and every normalized/weighted score are saved and checked
  against the actual tool results. No new candidate was introduced by scoring.
- Stored source/layer/section/vector data and protected historical artifacts
  have identical before/after fingerprints. Existing uncommitted edits are
  preserved; only the original coordinator, vector retriever, associated tests
  and maintenance documentation change. New report/artifact paths are separate.
- 108 focused tests pass, one generic live-database fixture test is skipped.
  The actual isolated-database replay passes. Tests cover missing-score completion,
  negative versus absent cosine, fixed weighting, constant/empty routes,
  source/version checks, tenant restriction, range identity, ACL/masking,
  unchanged chunk boundaries and existing BM25/retrieval behavior.
- No full benchmark, additional corpus/query evaluation, new embedding,
  generation, reranking-model or answering-model invocation occurs.
  Provider calls: **0**. Answering-model calls: **0**.

| Query | Body characters before → after | Tool-message characters before → after |
|---|---:|---:|
| literal | 6,033 → 6,033 | 8,481 → 8,470 |
| semantic | 8,992 → 9,509 | 11,425 → 11,940 |

These are character counts, not token estimates or actual model usage. The
semantic body grows by 517 characters because a 2,303-character pipeline section
replaces a 1,786-character branch-prediction section. No token/fee reduction or
answering-accuracy improvement is inferred.

## Complete original bodies received by the Agent

All five full `content` fields are reproduced for each query/phase in actual
returned order. These are source bodies, not generated summaries. Full JSON
links include URI, version, range, strategy and final score. Every result uses
`ctx://resources/hydra-layer-validation/microarchitecture`, version 1.

### literal — before

[Complete Agent JSON](artifacts/20261106-weighted-fusion/before/hybrid-literal-agent.json)

Result 1: Instruction pipelining. Section 10, range `[9617, 11920]`, 2303 characters.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

Result 2: See also. Section 17, range `[20817, 21522]`, 705 characters.

```text
## See also.

BULLET::::- List of AMD CPU microarchitectures

BULLET::::- List of Intel CPU microarchitectures

BULLET::::- Microprocessor

BULLET::::- Microcontroller

BULLET::::- Digital signal processor (DSP)

BULLET::::- CPU design

BULLET::::- Hardware description language (HDL)

BULLET::::- Hardware architecture

BULLET::::- Harvard architecture

BULLET::::- von Neumann architecture

BULLET::::- Multi-core (computing)

BULLET::::- Datapath

BULLET::::- Dataflow architecture

BULLET::::- Stack machine

BULLET::::- Belt machine

BULLET::::- Very-large-scale integration (VLSI)

BULLET::::- VHDL

BULLET::::- Verilog

BULLET::::- Stream processing

BULLET::::- Instruction level parallelism (ILP)
```

Result 3: Superscalar. Section 13, range `[15121, 16310]`, 1189 characters.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 4: Increasing execution speed. Section 8, range `[7303, 8317]`, 1014 characters.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 5: Out-of-order execution. Section 14, range `[16310, 17132]`, 822 characters.

```text
### Out-of-order execution.

The addition of caches reduces the frequency or duration of stalls due to waiting for data to be fetched from the memory hierarchy, but does not get rid of these stalls entirely. In early designs a "cache miss" would force the cache controller to stall the processor and wait. Of course there may be some other instruction in the program whose data "is" available in the cache at that point. Out-of-order execution allows that ready instruction to be processed while an older instruction waits on the cache, then re-orders the results to make it appear that everything happened in the programmed order. This technique is also used to avoid other operand dependency stalls, such as an instruction awaiting a result from a long latency floating-point operation or other multi-cycle operations.


```

### literal — after

[Complete Agent JSON](artifacts/20261106-weighted-fusion/after/hybrid-literal-agent.json)

Result 1: See also. Section 17, range `[20817, 21522]`, 705 characters.

```text
## See also.

BULLET::::- List of AMD CPU microarchitectures

BULLET::::- List of Intel CPU microarchitectures

BULLET::::- Microprocessor

BULLET::::- Microcontroller

BULLET::::- Digital signal processor (DSP)

BULLET::::- CPU design

BULLET::::- Hardware description language (HDL)

BULLET::::- Hardware architecture

BULLET::::- Harvard architecture

BULLET::::- von Neumann architecture

BULLET::::- Multi-core (computing)

BULLET::::- Datapath

BULLET::::- Dataflow architecture

BULLET::::- Stack machine

BULLET::::- Belt machine

BULLET::::- Very-large-scale integration (VLSI)

BULLET::::- VHDL

BULLET::::- Verilog

BULLET::::- Stream processing

BULLET::::- Instruction level parallelism (ILP)
```

Result 2: Superscalar. Section 13, range `[15121, 16310]`, 1189 characters.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 3: Instruction pipelining. Section 10, range `[9617, 11920]`, 2303 characters.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

Result 4: Increasing execution speed. Section 8, range `[7303, 8317]`, 1014 characters.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 5: Out-of-order execution. Section 14, range `[16310, 17132]`, 822 characters.

```text
### Out-of-order execution.

The addition of caches reduces the frequency or duration of stalls due to waiting for data to be fetched from the memory hierarchy, but does not get rid of these stalls entirely. In early designs a "cache miss" would force the cache controller to stall the processor and wait. Of course there may be some other instruction in the program whose data "is" available in the cache at that point. Out-of-order execution allows that ready instruction to be processed while an older instruction waits on the cache, then re-orders the results to make it appear that everything happened in the programmed order. This technique is also used to avoid other operand dependency stalls, such as an instruction awaiting a result from a long latency floating-point operation or other multi-cycle operations.


```

### semantic — before

[Complete Agent JSON](artifacts/20261106-weighted-fusion/before/hybrid-semantic-agent.json)

Result 1: Multiprocessing and multithreading. Section 16, range `[17627, 20817]`, 3190 characters.

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

Result 2: Aspects of microarchitecture. Section 4, range `[3111, 4924]`, 1813 characters.

```text
## Aspects of microarchitecture.

The pipelined datapath is the most commonly used datapath design in microarchitecture today. This technique is used in most modern microprocessors, microcontrollers, and DSPs. The pipelined architecture allows multiple instructions to overlap in execution, much like an assembly line. The pipeline includes several different stages which are fundamental in microarchitecture designs. Some of these stages include instruction fetch, instruction decode, execute, and write back. Some architectures include other stages such as memory access. The design of pipelines is one of the central microarchitectural tasks.

Execution units are also essential to microarchitecture. Execution units include arithmetic logic units (ALU), floating point units (FPU), load/store units, branch prediction, and SIMD. These units perform the operations or calculations of the processor. The choice of the number of execution units, their latency and throughput is a central microarchitectural design task. The size, latency, throughput and connectivity of memories within the system are also microarchitectural decisions.

System-level design decisions such as whether or not to include peripherals, such as memory controllers, can be considered part of the microarchitectural design process. This includes decisions on the performance-level and connectivity of these peripherals.

Unlike architectural design, where achieving a specific performance level is the main goal, microarchitectural design pays closer attention to other constraints. Since microarchitecture design decisions directly affect what goes into a system, attention must be paid to issues such as chip area/cost, power consumption, logic complexity, ease of connectivity, manufacturability, ease of debugging, and testability.


```

Result 3: Increasing execution speed. Section 8, range `[7303, 8317]`, 1014 characters.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 4: Superscalar. Section 13, range `[15121, 16310]`, 1189 characters.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 5: Branch prediction. Section 12, range `[13335, 15121]`, 1786 characters.

```text
### Branch prediction.

One barrier to achieving higher performance through instruction-level parallelism stems from pipeline stalls and flushes due to branches. Normally, whether a conditional branch will be taken isn't known until late in the pipeline as conditional branches depend on results coming from a register. From the time that the processor's instruction decoder has figured out that it has encountered a conditional branch instruction to the time that the deciding register value can be read out, the pipeline needs to be stalled for several cycles, or if it's not and the branch is taken, the pipeline needs to be flushed. As clock speeds increase the depth of the pipeline increases with it, and some modern processors may have 20 stages or more. On average, every fifth instruction executed is a branch, so without any intervention, that's a high amount of stalling.

Techniques such as branch prediction and speculative execution are used to lessen these branch penalties. Branch prediction is where the hardware makes educated guesses on whether a particular branch will be taken. In reality one side or the other of the branch will be called much more often than the other. Modern designs have rather complex statistical prediction systems, which watch the results of past branches to predict the future with greater accuracy. The guess allows the hardware to prefetch instructions without waiting for the register read. Speculative execution is a further enhancement in which the code along the predicted path is not just prefetched but also executed before it is known whether the branch should be taken or not. This can yield better performance when the guess is good, with the risk of a huge penalty when the guess is bad because instructions need to be undone.


```

### semantic — after

[Complete Agent JSON](artifacts/20261106-weighted-fusion/after/hybrid-semantic-agent.json)

Result 1: Increasing execution speed. Section 8, range `[7303, 8317]`, 1014 characters.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 2: Instruction pipelining. Section 10, range `[9617, 11920]`, 2303 characters.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

Result 3: Multiprocessing and multithreading. Section 16, range `[17627, 20817]`, 3190 characters.

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

Result 4: Superscalar. Section 13, range `[15121, 16310]`, 1189 characters.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 5: Aspects of microarchitecture. Section 4, range `[3111, 4924]`, 1813 characters.

```text
## Aspects of microarchitecture.

The pipelined datapath is the most commonly used datapath design in microarchitecture today. This technique is used in most modern microprocessors, microcontrollers, and DSPs. The pipelined architecture allows multiple instructions to overlap in execution, much like an assembly line. The pipeline includes several different stages which are fundamental in microarchitecture designs. Some of these stages include instruction fetch, instruction decode, execute, and write back. Some architectures include other stages such as memory access. The design of pipelines is one of the central microarchitectural tasks.

Execution units are also essential to microarchitecture. Execution units include arithmetic logic units (ALU), floating point units (FPU), load/store units, branch prediction, and SIMD. These units perform the operations or calculations of the processor. The choice of the number of execution units, their latency and throughput is a central microarchitectural design task. The size, latency, throughput and connectivity of memories within the system are also microarchitectural decisions.

System-level design decisions such as whether or not to include peripherals, such as memory controllers, can be considered part of the microarchitectural design process. This includes decisions on the performance-level and connectivity of these peripherals.

Unlike architectural design, where achieving a specific performance level is the main goal, microarchitectural design pays closer attention to other constraints. Since microarchitecture design decisions directly affect what goes into a system, attention must be paid to issues such as chip area/cost, power consumption, logic complexity, ease of connectivity, manufacturability, ease of debugging, and testability.


```

## Artifacts

- [Replay harness](../../integrations/hydrabench/validate_weighted_fusion.py)
- [Before rankings and raw scores](artifacts/20261106-weighted-fusion/before/comparison.json)
- [After rankings, completed scores and normalization](artifacts/20261106-weighted-fusion/after/comparison.json)
- [BM25 full-corpus statistics](artifacts/20261106-weighted-fusion/after/corpus-statistics.json)
- [Validation summary](artifacts/20261106-weighted-fusion/validation.json)
- [Protected state before](artifacts/20261106-weighted-fusion/before/protected-before.json)
- [Protected state after](artifacts/20261106-weighted-fusion/after/protected-after.json)
