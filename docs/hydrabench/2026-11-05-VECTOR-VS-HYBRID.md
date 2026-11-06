# Original vector-only versus vector + keyword: fixed saved-vector comparison

Date: 2026-11-05T18:19:18.789601+08:00 (Asia/Hong_Kong).

**For the semantic query, the current keyword fusion does push the direct
Instruction pipelining passage out of the top 5.** It is vector rank 2 and fused
rank 6. For the exact-identifier query, keyword fusion helps its ordering: the
same passage moves from vector rank 5 to fused rank 1, with the same five returned
passages in both arms. These observations concern this implementation, one source
and two queries; they do not establish that keyword retrieval is generally harmful
or that vector-only retrieval is generally superior.

No product behavior was changed. This turn adds only a replay harness and separate
artifacts/report. Existing uncommitted implementation changes are preserved.

## Comparison boundaries

- Source: the same Microarchitecture document, version 1,
  `ctx://resources/hydra-layer-validation/microarchitecture`, 21,522 characters.
- Source SHA-256:
  `17571bc69825350c0f464fb9e4ff1478f3dc0fc4f7d6c6cb9f7836783bae808c`.
- Existing isolated database: `hydra_originals_qwen37_20261105`; its 16 stored
  original-range vectors and two previously saved query vectors are reused.
  They were previously generated with `qwen3.7-text-embedding`, 1,536 dimensions.
- Both arms: explicit `top_k=5`, original vector candidate budget 15, identical
  source ranges, exact query vectors, rank constant 60 and normal ACL/version
  checks. The raw vector candidates and cosine scores match between arms.
- Both arms disable L0 document recall, L1 BM25 reranking and tree navigation.
  This isolates the second, original-evidence route. The only changed registration
  is whether that coordinator also runs the original keyword retriever.
- The vector-only arm still passes through the existing coordinator's monotonic
  `1/(60+rank)` score mapping. Its result order is exactly the cosine order; this
  is not an additional content reranker. Final numeric scores are rank scores,
  not cosine similarities or probabilities.
- Actual PostgreSQL retrieval, FastAPI, SDK and DeepAgents tool messages execute.
  All search/read bookkeeping rolls back. The harness blocks external HTTP and
  loads no provider credentials. No answering model is invoked.
- There are exactly four search-tool invocations: two fixed queries × two arms.
  Section reads verify each delivered body against the same source. No benchmark
  answers/reference reasoning or additional queries are used.

Queries:

1. `1986 VAX 8800`
2. `How can a CPU make progress on several instructions at once by handling different stages simultaneously?`

## How the two rankings combine

The original keyword retriever is **not BM25**. It splits the query into literal
terms, removes its existing stopwords, and ranks original ranges by distinct
matched-term count plus a bounded occurrence-density term. Matches are substring
based. These are the unchanged existing rules, including their known limitations.

The coordinator sorts each route independently and performs equal-weight
reciprocal rank fusion (RRF):

```text
combined score = 1/(60 + vector rank) + 1/(60 + keyword rank)
```

A route contributes zero if the range is absent from that route's candidates.
Raw cosine and raw keyword scores are not added. The merge key is document,
version and exact source range, so two routes finding one passage create one
result with two contributions, not two duplicate bodies.

For the semantic query, the actual ranks are:

| Fused rank | Passage | Vector rank | Keyword rank | Combined score |
|---|---|---|---|---|
| 1 | Multiprocessing and multithreading | 6 | 1 | 0.031544958 |
| 2 | Superscalar | 4 | 4 | 0.031250000 |
| 3 | Increasing execution speed | 1 | 9 | 0.030886196 |
| 4 | Branch prediction | 8 | 2 | 0.030834915 |
| 5 | Aspects of microarchitecture | 7 | 3 | 0.030798389 |
| 6 | Instruction pipelining | 2 | 10 | 0.030414747 |

For example, multithreading receives `1/66 + 1/61`, while pipelining receives
`1/62 + 1/70`. The keyword ordering more than offsets pipelining's vector advantage.
It has already entered the 15-vector candidate pool; this exclusion is caused by
fusion and the final top-5 cut, not an L0 gate, a document-level collapse, or an
index/input alignment error.

For the exact query, pipelining is the only keyword match. It gets keyword rank 1
plus vector rank 5: `1/61 + 1/65 = 0.031778058`, moving it to the first position.
The raw vector first result is See also, which is not the direct VAX/1986 evidence.

## Actual results in returned order

| Query | Vector only | Vector + keyword |
|---|---|---|
| Exact identifier | See also (17); Superscalar (13); Increasing execution speed (8); Out-of-order execution (14); Instruction pipelining (10) | Instruction pipelining (10); See also (17); Superscalar (13); Increasing execution speed (8); Out-of-order execution (14) |
| Semantic paraphrase | Increasing execution speed (8); Instruction pipelining (10); Instruction cycles (6); Superscalar (13); Out-of-order execution (14) | Multiprocessing and multithreading (16); Superscalar (13); Increasing execution speed (8); Branch prediction (12); Aspects of microarchitecture (4) |

The semantic arms share Increasing execution speed and Superscalar. Fusion
replaces Instruction pipelining, Instruction cycles and Out-of-order execution
with Multiprocessing and multithreading, Branch prediction and Aspects of
microarchitecture. This is an observed result-set change, not merely a score change.

The vector-only pipelining result directly explains decoding a new instruction
while the previous one waits for results. The hybrid arm still contains useful
pipeline evidence in its fifth result (Aspects of microarchitecture), which says
multiple instructions overlap like an assembly line and names the pipeline
stages. Therefore losing the standalone pipeline passage does not mean that the
hybrid response contains no relevant evidence. No answering-model correctness or
benchmark score is measured here.

## Size, calls and checks

| Query | Vector-only body characters | Hybrid body characters | Vector-only full tool-message characters | Hybrid full tool-message characters |
|---|---|---|---|---|
| Exact | 6,033 | 6,033 | 8,482 | 8,481 |
| Semantic | 5,764 | 8,992 | 8,184 | 11,413 |

These are character counts, not token measurements. No answering-model token
usage or monetary savings can be inferred from this trial. It makes **zero new
embedding, generation, summary, reranking-model or answering-model calls**.

Checks passed during the real replay:

- The same 15 vector candidates, scores and source ranges are used in both arms.
- Vector-only output order equals the actual cosine-ranking top 5.
- Hybrid outputs exactly match the previous passage-top-k trial's result objects.
- Final range scores/order match the formula above, computed from the recorded
  actual route ranks. Each returned URI/version/range is unique.
- All 20 returned bodies equal exact source slices; existing section reads match.
- Protected before/after manifests match for implementation files, earlier
  experiment artifacts, old indexes and the current sample's stored vectors,
  contexts, sections and traces. No index was rebuilt or altered.
- No product code, ranking constant, default or candidate multiplier changed.
  No full benchmark or larger corpus/query experiment ran. Validation uses the
  actual replay assertions; no unrelated test suite was rerun for this harness.

## Complete Agent bodies

The following reproduces every complete `content` value received by the Agent,
in actual returned order. Full JSON messages include provenance, URI, version,
section ID, source character range, strategy and score. L0/L1/full-body copies are
not appended to these evidence results. Every message uses the same URI and
version given above. The quoted source is retrieval data, not a generated answer.

### Literal query — vector_only

[Full Agent message](artifacts/20261105-vector-vs-hybrid/vector_only-literal-agent.json).

Result 1: See also., section 17, version 1, range `[20817, 21522]`, 705 characters, strategy `vector`.

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

Result 2: Superscalar., section 13, version 1, range `[15121, 16310]`, 1189 characters, strategy `vector`.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 3: Increasing execution speed., section 8, version 1, range `[7303, 8317]`, 1014 characters, strategy `vector`.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 4: Out-of-order execution., section 14, version 1, range `[16310, 17132]`, 822 characters, strategy `vector`.

```text
### Out-of-order execution.

The addition of caches reduces the frequency or duration of stalls due to waiting for data to be fetched from the memory hierarchy, but does not get rid of these stalls entirely. In early designs a "cache miss" would force the cache controller to stall the processor and wait. Of course there may be some other instruction in the program whose data "is" available in the cache at that point. Out-of-order execution allows that ready instruction to be processed while an older instruction waits on the cache, then re-orders the results to make it appear that everything happened in the programmed order. This technique is also used to avoid other operand dependency stalls, such as an instruction awaiting a result from a long latency floating-point operation or other multi-cycle operations.


```

Result 5: Instruction pipelining., section 10, version 1, range `[9617, 11920]`, 2303 characters, strategy `vector`.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

### Literal query — hybrid

[Full Agent message](artifacts/20261105-vector-vs-hybrid/hybrid-literal-agent.json).

Result 1: Instruction pipelining., section 10, version 1, range `[9617, 11920]`, 2303 characters, strategy `hybrid`.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

Result 2: See also., section 17, version 1, range `[20817, 21522]`, 705 characters, strategy `vector`.

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

Result 3: Superscalar., section 13, version 1, range `[15121, 16310]`, 1189 characters, strategy `vector`.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 4: Increasing execution speed., section 8, version 1, range `[7303, 8317]`, 1014 characters, strategy `vector`.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 5: Out-of-order execution., section 14, version 1, range `[16310, 17132]`, 822 characters, strategy `vector`.

```text
### Out-of-order execution.

The addition of caches reduces the frequency or duration of stalls due to waiting for data to be fetched from the memory hierarchy, but does not get rid of these stalls entirely. In early designs a "cache miss" would force the cache controller to stall the processor and wait. Of course there may be some other instruction in the program whose data "is" available in the cache at that point. Out-of-order execution allows that ready instruction to be processed while an older instruction waits on the cache, then re-orders the results to make it appear that everything happened in the programmed order. This technique is also used to avoid other operand dependency stalls, such as an instruction awaiting a result from a long latency floating-point operation or other multi-cycle operations.


```

### Semantic query — vector_only

[Full Agent message](artifacts/20261105-vector-vs-hybrid/vector_only-semantic-agent.json).

Result 1: Increasing execution speed., section 8, version 1, range `[7303, 8317]`, 1014 characters, strategy `vector`.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 2: Instruction pipelining., section 10, version 1, range `[9617, 11920]`, 2303 characters, strategy `vector`.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

Result 3: Instruction cycles., section 6, version 1, range `[4957, 5393]`, 436 characters, strategy `vector`.

```text
### Instruction cycles.

In general, all CPUs, single-chip microprocessors or multi-chip implementations run programs by performing the following steps:

BULLET::::1. Read an instruction and decode it

BULLET::::2. Find any associated data that is needed to process the instruction

BULLET::::3. Process the instruction

BULLET::::4. Write the results out

The instruction cycle is repeated continuously until the power is turned off.


```

Result 4: Superscalar., section 13, version 1, range `[15121, 16310]`, 1189 characters, strategy `vector`.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 5: Out-of-order execution., section 14, version 1, range `[16310, 17132]`, 822 characters, strategy `vector`.

```text
### Out-of-order execution.

The addition of caches reduces the frequency or duration of stalls due to waiting for data to be fetched from the memory hierarchy, but does not get rid of these stalls entirely. In early designs a "cache miss" would force the cache controller to stall the processor and wait. Of course there may be some other instruction in the program whose data "is" available in the cache at that point. Out-of-order execution allows that ready instruction to be processed while an older instruction waits on the cache, then re-orders the results to make it appear that everything happened in the programmed order. This technique is also used to avoid other operand dependency stalls, such as an instruction awaiting a result from a long latency floating-point operation or other multi-cycle operations.


```

### Semantic query — hybrid

[Full Agent message](artifacts/20261105-vector-vs-hybrid/hybrid-semantic-agent.json).

Result 1: Multiprocessing and multithreading., section 16, version 1, range `[17627, 20817]`, 3190 characters, strategy `hybrid`.

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

Result 2: Superscalar., section 13, version 1, range `[15121, 16310]`, 1189 characters, strategy `hybrid`.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 3: Increasing execution speed., section 8, version 1, range `[7303, 8317]`, 1014 characters, strategy `hybrid`.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 4: Branch prediction., section 12, version 1, range `[13335, 15121]`, 1786 characters, strategy `hybrid`.

```text
### Branch prediction.

One barrier to achieving higher performance through instruction-level parallelism stems from pipeline stalls and flushes due to branches. Normally, whether a conditional branch will be taken isn't known until late in the pipeline as conditional branches depend on results coming from a register. From the time that the processor's instruction decoder has figured out that it has encountered a conditional branch instruction to the time that the deciding register value can be read out, the pipeline needs to be stalled for several cycles, or if it's not and the branch is taken, the pipeline needs to be flushed. As clock speeds increase the depth of the pipeline increases with it, and some modern processors may have 20 stages or more. On average, every fifth instruction executed is a branch, so without any intervention, that's a high amount of stalling.

Techniques such as branch prediction and speculative execution are used to lessen these branch penalties. Branch prediction is where the hardware makes educated guesses on whether a particular branch will be taken. In reality one side or the other of the branch will be called much more often than the other. Modern designs have rather complex statistical prediction systems, which watch the results of past branches to predict the future with greater accuracy. The guess allows the hardware to prefetch instructions without waiting for the register read. Speculative execution is a further enhancement in which the code along the predicted path is not just prefetched but also executed before it is known whether the branch should be taken or not. This can yield better performance when the guess is good, with the risk of a huge penalty when the guess is bad because instructions need to be undone.


```

Result 5: Aspects of microarchitecture., section 4, version 1, range `[3111, 4924]`, 1813 characters, strategy `hybrid`.

```text
## Aspects of microarchitecture.

The pipelined datapath is the most commonly used datapath design in microarchitecture today. This technique is used in most modern microprocessors, microcontrollers, and DSPs. The pipelined architecture allows multiple instructions to overlap in execution, much like an assembly line. The pipeline includes several different stages which are fundamental in microarchitecture designs. Some of these stages include instruction fetch, instruction decode, execute, and write back. Some architectures include other stages such as memory access. The design of pipelines is one of the central microarchitectural tasks.

Execution units are also essential to microarchitecture. Execution units include arithmetic logic units (ALU), floating point units (FPU), load/store units, branch prediction, and SIMD. These units perform the operations or calculations of the processor. The choice of the number of execution units, their latency and throughput is a central microarchitectural design task. The size, latency, throughput and connectivity of memories within the system are also microarchitectural decisions.

System-level design decisions such as whether or not to include peripherals, such as memory controllers, can be considered part of the microarchitectural design process. This includes decisions on the performance-level and connectivity of these peripherals.

Unlike architectural design, where achieving a specific performance level is the main goal, microarchitectural design pays closer attention to other constraints. Since microarchitecture design decisions directly affect what goes into a system, attention must be paid to issues such as chip area/cost, power consumption, logic complexity, ease of connectivity, manufacturability, ease of debugging, and testability.


```

## Artifacts

- [Configuration](artifacts/20261105-vector-vs-hybrid/configuration.json)
- [Comparison, raw scores and per-route ranks](artifacts/20261105-vector-vs-hybrid/comparison.json)
- [Protected state before](artifacts/20261105-vector-vs-hybrid/protected-before.json)
- [Protected state after](artifacts/20261105-vector-vs-hybrid/protected-after.json)
- [Replay harness](../../integrations/hydrabench/validate_vector_keyword.py)

Raw local API responses and traces are saved alongside the Agent JSON messages.
All artifact writes use new paths/exclusive creation; historical records remain
untouched. The harness requires the existing sample DSN and source checkout on
PYTHONPATH, and obtains the query vectors from the preserved Qwen3.7 artifact.
It does not load any model credentials.
