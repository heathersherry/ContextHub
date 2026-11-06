# Passage-level top-k: saved-vector replay

Date: 2026-11-05T18:10:45.996220+08:00 (Asia/Hong_Kong).

The return-unit defect is fixed: different original ranges from one document can
now occupy separate top-k result slots, while vector/keyword matches for the same
range fuse into a single result. This is not a claim of improved relevance. With
the unchanged tool default of 5, the semantic query still does not return
Instruction pipelining: its fused rank is 6 under the corresponding 15-vector
candidate budget. No query, cutoff, weight or default was tuned to include it.

## Changes

- `LongDocRetrievalCoordinator.search_originals` keeps each fused source range,
  instead of projecting only the best range onto each document row.
- `RetrievalService` replaces a document's old discovery/tree representative with
  its original range results. Distinct ranges no longer collapse in the outer
  merge. When original ranges are the only remaining route, their fusion scores
  pass through. Otherwise the existing equal-weight RRF (`1/(60+rank)`) combines
  the remaining context and original-result lists, using range-aware identities.
  A document-level vote is not copied onto every original range.
- The final limit is global across evidence hits after common ACL, masks and
  exact-version/status checks. L0 discovery still returns one hit per document.
  Short contexts and table descriptions remain individual context hits.
- `snippet_offset` is an optional `[start,end)` character range in the generic
  API result and SDK model, and is preserved by the existing one-body DeepAgents
  projection. A section ID alone cannot distinguish multiple pieces of one large
  section. No new endpoint or request parameter was added.
- File-backed evidence results carry their original snippet without repeated
  L0/L1/full-L2 body values. Document layers remain stored once and accessible
  through existing reads. Trace records distinguish the returned ranges.
- No ingestion, source, section tree, embedding client, vector table, database
  schema, index, model configuration, candidate multiplier or default changed.

Before this change an outer document RRF reduced the pure original result to
`1/61`; the mixed control gave it `2/61`. These are replaced by the actual range
fusion scores when only original results remain. This is the removal of a
document-level aggregation, not an adjustment of the vector/keyword weights.

## Fixed replay

The same Microarchitecture source, URI and version 1 were used, with the existing
16 Qwen3.7 1,536-dimensional vectors in `hydra_originals_qwen37_20261105` and the
two saved query vectors. No vectors were inserted, updated, or regenerated.
Source SHA-256 remains
`17571bc69825350c0f464fb9e4ff1478f3dc0fc4f7d6c6cb9f7836783bae808c`.

Queries:

1. `1986 VAX 8800`
2. `How can a CPU make progress on several instructions at once by handling different stages simultaneously?`

Both queries were replayed against the pre-change source snapshot and current
code. Each uses (a) the prior explicit `top_k=1`, and (b) an omitted `top_k`, which
exercises the existing DeepAgents default of 5. Candidate budgets remain 3 and
15 respectively. Within each comparison, raw keyword/vector candidates and
scores are exactly identical before and after.

Both the pure-original harness and mixed control were checked. The control uses
the same `TreeRetriever(NoOpChatClient())` as prior samples, not live LLM tree
selection. Pure mode skips the first-route entry and L1 reranker. Actual local
PostgreSQL retrieval, FastAPI, SDK and DeepAgents tool messages execute normally;
query embeddings are replayed and all search/read bookkeeping rolls back. The
harness rejects external HTTP requests. No answering model is called.

## Results

| Request | Before result sections | After result sections | Body characters before → after |
|---|---|---|---|
| Exact, explicit top_k=1 | Instruction pipelining (10) | Same exact body (10) | 2,303 → 2,303 |
| Semantic, explicit top_k=1 | Increasing execution speed (8) | Same exact body (8) | 1,014 → 1,014 |
| Exact, tool default 5 | Instruction pipelining (10) | Instruction pipelining (10), See also (17), Superscalar (13), Increasing execution speed (8), Out-of-order execution (14) | 2,303 → 6,033 |
| Semantic, tool default 5 | Multiprocessing and multithreading (16) | Multiprocessing and multithreading (16), Superscalar (13), Increasing execution speed (8), Branch prediction (12), Aspects of microarchitecture (4) | 3,190 → 8,992 |

Mixed and pure modes return identical result objects after the change for these
requests. This one-document fixture cannot measure corpus-wide diversity or
whether the first route introduces noise among competing documents.

The exact query's pipeline range is found by both lexical and vector retrieval
under default 5. It is delivered once, with `retrieval_strategy=hybrid` and range
`[9617,11920)`. All five semantic hits are also hybrid hits, each delivered once.
The returned ranges are distinct and non-overlapping; their contents match the
source exactly. Section reads were checked against the same original.

The new fifth semantic result, Aspects of microarchitecture, does contain direct
pipeline evidence: "The pipelined architecture allows multiple instructions to
overlap in execution, much like an assembly line." It also names instruction
fetch, decode, execute and write back. Thus the missing standalone Instruction
pipelining chapter does not mean that all relevant pipeline evidence is absent.
This newly delivered evidence is observable; answer correctness was not tested
with an answering model, and no benchmark-score gain is claimed.

The semantic range rankings under default 5 are:

| Fused rank | Section | Keyword rank | Vector rank | Fused score |
|---|---|---|---|---|
| 1 | Multiprocessing and multithreading | 1 | 6 | 0.031544958 |
| 2 | Superscalar | 4 | 4 | 0.031250000 |
| 3 | Increasing execution speed | 9 | 1 | 0.030886196 |
| 4 | Branch prediction | 2 | 8 | 0.030834915 |
| 5 | Aspects of microarchitecture | 3 | 7 | 0.030798389 |
| 6 | Instruction pipelining | 10 | 2 | 0.030414747 |

Vector rank 2 is not fused rank 2. Expanding the candidate pool from 3 to 15 allows
more keyword matches to also receive vector contributions. This existing
`top_k * 3` behavior explains why the default-5 ordering differs from the top-1
ordering. No extra K values were tried to obtain a desired result. The noisy
literal-query extra hits and semantic pipeline miss are retained in the record.

## Counts and verification

- **0 new embedding calls, 0 chat/summary calls, 0 answering-model calls.** There
  are no new provider usage totals or measured answering-model input tokens.
- Character counts above are actual delivered body characters. A rough
  characters/4 estimate is about 576 → 1,508 tokens for the exact default-5
  bodies, and 798 → 2,248 for the semantic bodies. These are estimates, not model
  token counts. More evidence increases the context size here; no token or cost
  saving is claimed. Exact full tool-message character counts are in comparison.json.
- Before/after protected manifests match for the old full index, earlier sample
  databases, Qwen3.7 sample vectors/context/sections, and historical artifacts.
- Existing worktree changes were preserved; a pre-change source/test snapshot is
  at `/private/tmp/contexthub-passage-topk-before-20261105`.
- Focused tests verify range deduplication, multiple ranges sharing a section,
  global top-k across documents, one query embedding, masks, removal of all ranges
  on a version change, L0 document uniqueness, L2 evidence, and SDK/tool locators.
- Core/retrieval/adapter checks: **130 passed, 1 skipped**. SDK/document API checks:
  **45 passed, 6 skipped**. Total: **175 passed, 7 skipped**. `git diff --check`
  and report-link checks pass. No full benchmark ran.

## Actual Agent content, before and after

The following sections reproduce the complete `content` values for the tool's
unchanged default 5 in pure mode. Mixed mode has the same bodies. Metadata is
listed separately for readability; full JSON messages retain URI, version, source
range, section ID, strategy, status and provenance tags. The pre-change adapter
did not expose the range field; its source ranges below are independently checked
from the body. The explicit-top-1 complete messages are also preserved as JSON.

### Literal query — before

[Complete tool message](artifacts/20261105-passage-topk/before-originals_only-default5-literal-agent.json).

Result 1: section 10 (Instruction pipelining.), version 1, source range `[9617, 11920]`, 2303 characters, `hybrid`.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

### Literal query — after

[Complete tool message](artifacts/20261105-passage-topk/after-originals_only-default5-literal-agent.json).

Result 1: section 10 (Instruction pipelining.), version 1, source range `[9617, 11920]`, 2303 characters, `hybrid`.

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.


```

Result 2: section 17 (See also.), version 1, source range `[20817, 21522]`, 705 characters, `vector`.

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

Result 3: section 13 (Superscalar.), version 1, source range `[15121, 16310]`, 1189 characters, `vector`.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 4: section 8 (Increasing execution speed.), version 1, source range `[7303, 8317]`, 1014 characters, `vector`.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 5: section 14 (Out-of-order execution.), version 1, source range `[16310, 17132]`, 822 characters, `vector`.

```text
### Out-of-order execution.

The addition of caches reduces the frequency or duration of stalls due to waiting for data to be fetched from the memory hierarchy, but does not get rid of these stalls entirely. In early designs a "cache miss" would force the cache controller to stall the processor and wait. Of course there may be some other instruction in the program whose data "is" available in the cache at that point. Out-of-order execution allows that ready instruction to be processed while an older instruction waits on the cache, then re-orders the results to make it appear that everything happened in the programmed order. This technique is also used to avoid other operand dependency stalls, such as an instruction awaiting a result from a long latency floating-point operation or other multi-cycle operations.


```

### Semantic query — before

[Complete tool message](artifacts/20261105-passage-topk/before-originals_only-default5-semantic-agent.json).

Result 1: section 16 (Multiprocessing and multithreading.), version 1, source range `[17627, 20817]`, 3190 characters, `hybrid`.

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

### Semantic query — after

[Complete tool message](artifacts/20261105-passage-topk/after-originals_only-default5-semantic-agent.json).

Result 1: section 16 (Multiprocessing and multithreading.), version 1, source range `[17627, 20817]`, 3190 characters, `hybrid`.

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

Result 2: section 13 (Superscalar.), version 1, source range `[15121, 16310]`, 1189 characters, `hybrid`.

```text
### Superscalar.

Even with all of the added complexity and gates needed to support the concepts outlined above, improvements in semiconductor manufacturing soon allowed even more logic gates to be used.

In the outline above the processor processes parts of a single instruction at a time. Computer programs could be executed faster if multiple instructions were processed simultaneously. This is what superscalar processors achieve, by replicating functional units such as ALUs. The replication of functional units was only made possible when the die area of a single-issue processor no longer stretched the limits of what could be reliably manufactured. By the late 1980s, superscalar designs started to enter the market place.

In modern designs it is common to find two load units, one store (many instructions have no results to store), two or more integer math units, two or more floating point units, and often a SIMD unit of some sort. The instruction issue logic grows in complexity by reading in a huge list of instructions from memory and handing them off to the different execution units that are idle at that point. The results are then collected and re-ordered at the end.


```

Result 3: section 8 (Increasing execution speed.), version 1, source range `[7303, 8317]`, 1014 characters, `hybrid`.

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.


```

Result 4: section 12 (Branch prediction.), version 1, source range `[13335, 15121]`, 1786 characters, `hybrid`.

```text
### Branch prediction.

One barrier to achieving higher performance through instruction-level parallelism stems from pipeline stalls and flushes due to branches. Normally, whether a conditional branch will be taken isn't known until late in the pipeline as conditional branches depend on results coming from a register. From the time that the processor's instruction decoder has figured out that it has encountered a conditional branch instruction to the time that the deciding register value can be read out, the pipeline needs to be stalled for several cycles, or if it's not and the branch is taken, the pipeline needs to be flushed. As clock speeds increase the depth of the pipeline increases with it, and some modern processors may have 20 stages or more. On average, every fifth instruction executed is a branch, so without any intervention, that's a high amount of stalling.

Techniques such as branch prediction and speculative execution are used to lessen these branch penalties. Branch prediction is where the hardware makes educated guesses on whether a particular branch will be taken. In reality one side or the other of the branch will be called much more often than the other. Modern designs have rather complex statistical prediction systems, which watch the results of past branches to predict the future with greater accuracy. The guess allows the hardware to prefetch instructions without waiting for the register read. Speculative execution is a further enhancement in which the code along the predicted path is not just prefetched but also executed before it is known whether the branch should be taken or not. This can yield better performance when the guess is good, with the risk of a huge penalty when the guess is bad because instructions need to be undone.


```

Result 5: section 4 (Aspects of microarchitecture.), version 1, source range `[3111, 4924]`, 1813 characters, `hybrid`.

```text
## Aspects of microarchitecture.

The pipelined datapath is the most commonly used datapath design in microarchitecture today. This technique is used in most modern microprocessors, microcontrollers, and DSPs. The pipelined architecture allows multiple instructions to overlap in execution, much like an assembly line. The pipeline includes several different stages which are fundamental in microarchitecture designs. Some of these stages include instruction fetch, instruction decode, execute, and write back. Some architectures include other stages such as memory access. The design of pipelines is one of the central microarchitectural tasks.

Execution units are also essential to microarchitecture. Execution units include arithmetic logic units (ALU), floating point units (FPU), load/store units, branch prediction, and SIMD. These units perform the operations or calculations of the processor. The choice of the number of execution units, their latency and throughput is a central microarchitectural design task. The size, latency, throughput and connectivity of memories within the system are also microarchitectural decisions.

System-level design decisions such as whether or not to include peripherals, such as memory controllers, can be considered part of the microarchitectural design process. This includes decisions on the performance-level and connectivity of these peripherals.

Unlike architectural design, where achieving a specific performance level is the main goal, microarchitectural design pays closer attention to other constraints. Since microarchitecture design decisions directly affect what goes into a system, attention must be paid to issues such as chip area/cost, power consumption, logic complexity, ease of connectivity, manufacturability, ease of debugging, and testability.


```

## Artifact links

- [Final checks](artifacts/20261105-passage-topk/final-validation.json)
- [Comparison, range rankings and text sizes](artifacts/20261105-passage-topk/comparison.json)
- [Before report](artifacts/20261105-passage-topk/before-report.json)
- [After report](artifacts/20261105-passage-topk/after-report.json)
- [Protected state before](artifacts/20261105-passage-topk/protected-before.json)
- [Protected state after](artifacts/20261105-passage-topk/protected-after.json)
- [Replay script](../../integrations/hydrabench/validate_passage_topk.py)

The artifact directory includes raw local API responses/traces and all actual
Agent messages for both modes and budgets, with exclusive writes to avoid
overwriting earlier records. The replay uses a pre-change PYTHONPATH snapshot for
`before` and current sources for `after`, with the same existing database DSN and
saved vectors. It does not require provider credentials.
