# Original-only route trial: Microarchitecture, Qwen3.7 embeddings

Date: 2026-11-05T16:42:16.188091+08:00 (Asia/Hong_Kong).

**Disabling the first route did not change either returned original body.** The
exact-identifier query still returns Instruction pipelining. The paraphrase
returns Increasing execution speed, which is broadly related but does not explain
overlapping stages as directly as Instruction pipelining. This small trial does
not demonstrate a relevance gain, or establish that L0 adds noise across a corpus.

## Scope and controlled comparison

The existing Microarchitecture source is unchanged: 21,522 characters, version 1,
URI `ctx://resources/hydra-layer-validation/microarchitecture`, SHA-256
`17571bc69825350c0f464fb9e4ff1478f3dc0fc4f7d6c6cb9f7836783bae808c`.
The source's 17 saved section nodes yield the same 16 non-overlapping original
ranges. Neither ranges nor generated L0/L1/section descriptions were regenerated.
No benchmark answers or reference reasoning were read to construct the index.

Both arms use the configured `qwen3.7-text-embedding`, explicitly requesting and
receiving 1,536 dimensions. The provider's reported response model matches the
request. No zero padding is used. Saved raw responses include response indexes;
each stored vector was checked against the corresponding provider vector after
float32 conversion. Query vectors are generated once and replayed identically.

A new database, `hydra_originals_qwen37_20261105`, contains only this source. It
was cloned from the old one-document fixture; its copied old vectors were cleared
before new vectors were inserted. Only this new copy received the 16 chunk vectors
and one new-model L0 vector. The L0 vector is needed solely for the same-model
mixed-route control, not for the pure arm. Original source files were also copied
to this trial's own artifact directory. Historical indexes were not rebuilt.

Fixed settings: `top_k=1`, vector candidate multiplier 3, chunk maximum 4,000
characters, unchanged literal keyword matching, and unchanged reciprocal rank
fusion (RRF) constant 60. The two queries are:

1. `1986 VAX 8800`
2. `How can a CPU make progress on several instructions at once by handling different stages simultaneously?`

| Arm | Enabled retrieval |
|---|---|
| Mixed control | L0 document vector recall → L1 BM25 reranking → tree fallback, plus independent original vectors and literal keyword retrieval |
| Originals only | Independent original vectors and literal keyword retrieval; no L0 recall, no L1 BM25 reranking, no tree navigation |

The trial harness temporarily replaces the first-route entry with an empty
candidate source **only in the pure arm's process**, and selects no reranker.
Thus first-route SQL and tree selection are skipped, rather than run and discarded.
No original-retrieval result is mocked. The production original retrievers,
coordinator, range fusion, PostgreSQL vectors, ACL/version checks, FastAPI search,
SDK and DeepAgents tool projection execute normally. Search/read bookkeeping is
rolled back. Production code and default behavior are unchanged; no public route
selector or compatibility branch was added.

The mixed control deliberately preserves the previous trial's
`TreeRetriever(NoOpChatClient())` setting. Its tree path uses the deterministic
fallback, not a live model. This is **not** a comparison against live LLM tree
selection. No answering model was invoked either; the saved messages are the
actual DeepAgents tool messages that such a model would receive.

## Actual tool results

| Query | Mixed control body | Originals-only body | Assessment |
|---|---|---|---|
| Exact identifier | Instruction pipelining, section 10, `[9617,11920)`, 2,303 characters | Byte-for-byte identical | Contains the VAX 8800 / 1986 evidence. |
| Paraphrase | Increasing execution speed, section 8, `[7303,8317)`, 1,014 characters | Byte-for-byte identical | Broad parallel-execution motivation; does not directly explain overlapping pipeline stages. |

For each query, the only differing result field is `score`: mixed
`0.03278688524590164` versus pure `0.01639344262295082`. The outer document-level
fusion gives the sole document one vote from each route in the control, and one
vote in the pure arm. This is not a probability, a quality drop, or token savings.
On overlap the current service already gives precedence to the original route's
body. With one document, removing the other document vote cannot test ranking
among competing documents. That is why these observations cannot establish
whether the first route adds corpus-level noise.

## Why the paraphrase still does not select Instruction pipelining

The new model puts Instruction pipelining at vector rank **2**, versus rank 7 in
the historical v4 run. This difference belongs to the new embedding configuration,
not to disabling the first route: both current arms have exactly these same vectors.
It is now inside the three-candidate budget.

| Original range | Vector rank / cosine | Keyword rank | Range fusion score |
|---|---|---|---|
| Increasing execution speed | 1 / 0.698153 | 9 | `1/61 + 1/69 = 0.030886196` |
| Instruction pipelining | 2 / 0.697435 | 10 | `1/62 + 1/70 = 0.030414747` |
| Instruction cycles | 3 / 0.686337 | 15 | `1/63 + 1/75 = 0.029206349` |

The current fusion uses ranks, not the magnitude of the cosine difference.
Increasing execution speed ranks slightly above Instruction pipelining in both
lists, so it wins. The coordinator then retains one original range per document.
The runner-up pipeline evidence exists and is readable, but is not included in
the selected tool body. This outcome occurs within the second route; removing
L0 cannot change it. No weights, candidate limits, queries, token boundaries or
tie-break rules were tuned after seeing these results.

The literal query's sole keyword hit is Instruction pipelining. Its vector rank
is 5, outside the top 3. Keyword rank 1 and vector rank 1 (See also) have equal
RRF scores; existing lexical-first insertion and strict greater-than replacement
retain Instruction pipelining. The precise query therefore succeeds through
literal evidence and the existing tie behavior, not strong vector matching.

This trial keeps the existing keyword mechanism, including its substring matching
limitations. It is not a new full-text BM25 index. It also keeps the one-body-per-
document policy. Neither was repaired or tuned as part of this route comparison.

## Actual Agent body: exact identifier

Both modes send this same complete `content` value, once:

```text
### Instruction pipelining.

One of the first, and most powerful, techniques to improve performance is the use of instruction pipelining. Early processor designs would carry out all of the steps above for one instruction before moving onto the next. Large portions of the circuitry were left idle at any one step; for instance, the instruction decoding circuitry would be idle during execution and so on.

Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time. In the same basic example, the processor would start to decode (step 1) a new instruction while the last one was waiting for results. This would allow up to four instructions to be "in flight" at one time, making the processor look four times as fast. Although any one instruction takes just as long to complete (there are still four steps) the CPU as a whole "retires" instructions much faster.

RISC makes pipelines smaller and much easier to construct by cleanly separating each stage of the instruction process and making them take the same amount of time—one cycle. The processor as a whole operates in an assembly line fashion, with instructions coming in one side and results out the other. Due to the reduced complexity of the classic RISC pipeline, the pipelined core and an instruction cache could be placed on the same size die that would otherwise fit the core alone on a CISC design. This was the real reason that RISC was faster. Early designs like the SPARC and MIPS often ran over 10 times as fast as Intel and Motorola CISC solutions at the same clock speed and price.

Pipelines are by no means limited to RISC designs. By 1986 the top-of-the-line VAX implementation (VAX 8800) was a heavily pipelined design, slightly predating the first commercial MIPS and SPARC designs. Most modern CPUs (even embedded CPUs) are now pipelined, and microcoded CPUs with no pipelining are seen only in the most area-constrained embedded processors. Large CISC machines, from the VAX 8800 to the modern Pentium 4 and Athlon, are implemented with both microcode and pipelines. Improvements in pipelining and caching are the two major microarchitectural advances that have enabled processor performance to keep pace with the circuit technology on which they are based.

```

Metadata retains URI, version 1, section 10, `retrieval_strategy=keyword`, and
`content_level=snippet`. There are no extra L0/L1/L2 body fields in the tool result.

## Actual Agent body: paraphrase

Both modes send this same complete `content` value, once:

```text
### Increasing execution speed.

Complicating this simple-looking series of steps is the fact that the memory hierarchy, which includes caching, main memory and non-volatile storage like hard disks (where the program instructions and data reside), has always been slower than the processor itself. Step (2) often introduces a lengthy (in CPU terms) delay while the data arrives over the computer bus. A considerable amount of research has been put into designs that avoid these delays as much as possible. Over the years, a central goal was to execute more instructions in parallel, thus increasing the effective execution speed of a program. These efforts introduced complicated logic and circuit structures. Initially, these techniques could only be implemented on expensive mainframes or supercomputers due to the amount of circuitry needed for these techniques. As semiconductor manufacturing progressed, more and more of these techniques could be implemented on a single semiconductor chip. See Moore's law.

```

Metadata retains URI, version 1, section 8, `retrieval_strategy=hybrid`, and
`content_level=snippet`. The more direct explanation remains in the source's
Instruction pipelining section, including:

> Pipelining improves performance by allowing a number of instructions to work their way through the processor at the same time.

The SDK's section-read operation was checked against exact source offsets for
the returned sections. No source or navigation content was discarded.

## Usage and validation

There were **3 actual embedding HTTP requests, 19 inputs, and 4,796 provider-
reported input tokens**. The first batch embeds two queries and one L0 (131 tokens
in aggregate); the two chunk batches contain 10 and 6 inputs (2,896 and 1,769
tokens). All responses are HTTP 200. The shared batch usage cannot allocate the
131 tokens to each of its individual inputs. There were zero chat-generation,
summary-generation or answering-model calls. Comparison searches replay the saved
query vectors and make no further provider calls.

Body character counts are 2,303 and 1,014; a rough characters/4 estimate is 576
and 254 tokens. These are not measured answering-model token counts. Actual tool
message lengths are 2,843 and 1,542 characters in both arms. The bodies do not
shrink in this comparison, and no answering-token or cost reduction is claimed.

Checks passed:

- All 16 range vectors and the L0 vector match their saved provider responses.
- The same queries, source, chunk boundaries and saved vectors serve both arms.
- The pure arm executes zero tree selections; both original retrievers run for
  both queries. The L1 reranker is disabled in the pure arm.
- URI/version and exact original section reads are retained; each tool result
  contains one body. Other accounts see zero source/chunk rows under RLS.
- Search/read operations leave the new sample database unchanged after rollback.
- Protected old databases, prior result artifacts, and existing source files
  have matching before/after fingerprints.
- Focused existing checks: **85 passed, 1 skipped** across embedding client,
  chunk vectors, long-document retrieval and DeepAgents tool tests.
- `git diff --check` passes. No full benchmark or historical index rebuild ran.

## Artifacts and reproduction

- [Comparison and stage candidates](artifacts/20261105-originals-only-qwen37/comparison.json)
- [Full vector ranking](artifacts/20261105-originals-only-qwen37/full-vector-ranks.json)
- [Provider usage](artifacts/20261105-originals-only-qwen37/provider-usage.json)
- [Configuration](artifacts/20261105-originals-only-qwen37/configuration.json)
- [Index checks](artifacts/20261105-originals-only-qwen37/index-check.json)
- [Exact-query pure tool message](artifacts/20261105-originals-only-qwen37/literal-agent-originals_only.json)
- [Paraphrase pure tool message](artifacts/20261105-originals-only-qwen37/semantic-agent-originals_only.json)
- [Exact-query control tool message](artifacts/20261105-originals-only-qwen37/literal-agent-mixed.json)
- [Paraphrase control tool message](artifacts/20261105-originals-only-qwen37/semantic-agent-mixed.json)
- [Trial harness](../../integrations/hydrabench/validate_originals_only.py)

The artifact directory also preserves raw provider request bodies/responses (no
authorization headers), exact chunk inputs, query vectors, copied source files,
and protected-state manifests. `prepare` is a one-time operation and refuses an
existing output directory; `compare` also refuses to overwrite result artifacts.
The harness expects `HYDRA_SAMPLE_DATABASE_URL` to identify the new sample database
and model credentials/configuration to be supplied through the environment.
Existing fixture assumptions are intentional; this is not a general ingestion CLI.
