# MemDerive q001: automatic parsing and entity-path retrieval

Subsequent [official answer and citation evaluation](MEMDERIVE-AUTOMATIC-QUERY-Q001-ANSWER-20261102.md)
uses this unchanged 15-item result: answer/witnessed accuracy and citation P/R/F1
are all 1, and both error rates are 0. The retrieval-only run below is preserved
as originally recorded.

Run completed: 2026-11-02T12:17:38.962586+08:00 (Hong Kong, UTC+8).

## Outcome

Starting from the original question and the archived eligible memory corpus, the
probe automatically resolves `trip-anser` and returns exactly 15 facts. No entity
name, answer text, witness reference, initial candidate list, or fixed first-three
prefix is supplied to the selector. The active hotel is ranked **4** (0.399362),
the canceled hotel **6** (0.382609), and the missing coffee receipt **10** (0.339099).

There are **zero generative LLM calls** in this retrieval run. One query embedding
call uses the existing `qwen3.7-text-embedding` service and reports 43 input tokens
and 43 total tokens. Output-token usage is absent, so the provider usage record
remains partial; no dollar cost is inferred. The local parser is spaCy
3.8.14 with `en_core_web_sm`
3.8.0. Existing extracted entities and memory
vectors are reused; original ingestion was a separate, earlier operation.

This is an isolated retrieval experiment. The shared production retriever is
unchanged. Answers and official benchmark scores were not rerun.

## Inputs and automatic parsing

Original question:

> As of 2026-10-07 09:00, which hotel booking is currently active for the February conference trip?

| Field | Automatically extracted value |
| --- | --- |
| Observation cutoff | `2026-10-07 09:00` |
| Requested noun phrase | `hotel booking` |
| Requested predicate tokens | `currently`, `active` |
| Locator phrase | `the February conference trip` |
| Locator object type | `trip` |
| Required locator terms | `conference`, `month:02` |
| Removed source span | `for the February conference trip` |

The ranking query is obtained by deleting that source span; no paraphrase or
entity insertion is performed:

> As of 2026-10-07 09:00, which hotel booking is currently active?

The time prefix, date spelling, and state predicate remain verbatim. The cutoff
also identifies the original observation context. This probe reuses the original
session-77 snapshot; it does not implement a new temporal-state reconstruction.

The snapshot contains 324 eligible current facts after choosing the latest
version, then checking fresh/accepted/not-invalidated state. It is the same old
snapshot used in the earlier q001 probes, not a new ingestion with current prompts.

## Candidate anchors and entity path

The local parser supplies the locator terms. A fact must contain **both** terms
(after lemmatization and month-name/ISO-date normalization) to be an anchor.
Two facts qualify:

- `d4968bf6-2201-40af-a238-572f9c1829f3:2`: Conference Anser now runs from 2026-02-02 through 2026-02-06. Entities: conference anser.
- `e52d2999-a9f7-4871-a65e-8b49f398ca14:1`: An old thread still shows Conference Lyra starting on February 8. Entities: conference lyra, february 8.

From each anchor, the program follows its existing entity index through at most
one additional fact. The destination entity name must contain the parsed object
head as a token/lemma. There is no top-six truncation and no hand-entered target.

Exactly one eligible path is found:

```text
February + conference
  -> Conference Anser now runs from 2026-02-02 through 2026-02-06.
  -> entity: conference anser
  -> Trip-Anser was opened for Conference Anser.
  -> entity: trip-anser
  -> all 20 eligible facts indexed under that entity
```

The old Conference Lyra thread matches the initial words but yields no destination
with the required type through this bounded indexed search. This does not prove
that Lyra is semantically irrelevant; it only reports what the search found.

The path's two supporting facts are the date fact
`d4968bf6-2201-40af-a238-572f9c1829f3:2` and the conference-to-trip fact
`dde5aa43-c717-4625-88eb-a988264789b8:1`. Both are computed outputs.

If multiple paths existed, this prototype would order them by the highest content
cosine in each destination pool, then fewer support facts and stable lexical ties.
For this run there is only one path, so that tie-breaking policy is **not tested**.
Its best pool cosine happens to belong to the annual travel summary, which shows
why this policy should not yet be treated as semantic disambiguation.

## Selection and scores

For each candidate memory `m`:

```text
score(m) = cosine(embedding(automatically extracted content query), saved_embedding(m))
```

There is no 0.5 multiplier and no similarity-to-already-selected penalty. Only
identical memory references are deduplicated. The path's support facts consume
budget; the remaining slots are filled from the destination entity's complete
index by descending content cosine. All selected facts are then sorted together
by the same cosine, with memory reference as the deterministic tie-breaker.

The 20 indexed facts plus the external conference-date fact make 21 candidates.
The hotel fact is not reserved. The two path facts naturally rank 2 and 9;
independent verification confirms that simply taking the highest-scoring 15 of
these 21 yields the **identical result**. Path preservation changes no membership
or position in this run.

This automatic prototype replaces the earlier manual-prefix continuation. It
does not apply the old keyword-coverage/group/bridge-gain formula or the old
original-query neighbor threshold to this complete entity pool. Comparisons with
prior manual-query probes are not a controlled single-variable ablation.

| Rank | Raw cosine | Memory text | Inclusion reason |
| --- | ---: | --- | --- |
| 1 | 0.465539 | The business travel record for 2026 is fully synced; it lists Trip-Anser, Trip-Grus, Trip-Vireo, Trip-Tanit, Trip-Ibex, Trip-Corax, Trip-Saiga, Trip-Milvus, Trip-Lorne, Trip-Rukh, Trip-Falco and no other business trips. | Content cosine |
| 2 | 0.425772 | Conference Anser now runs from 2026-02-02 through 2026-02-06. | Path support |
| 3 | 0.416201 | On 2026-02-12 at 09:10, item PS-1 for Trip-Anser was closed after review. | Content cosine |
| 4 | 0.399362 | Copperline Hotel in Velle is booked for Trip-Anser at 655 CR for the stay. | Content cosine |
| 5 | 0.383851 | The refund ledger for Trip-Anser is fully synced. | Content cosine |
| 6 | 0.382609 | The Velle Arcade Hotel reservation for Trip-Anser was canceled free of charge. | Content cosine |
| 7 | 0.370650 | Trip-Anser is wrapped up and marked completed. | Content cosine |
| 8 | 0.355321 | The pre-submission tracker for Trip-Anser is fully synced. | Content cosine |
| 9 | 0.343318 | Trip-Anser was opened for Conference Anser. | Path support |
| 10 | 0.339099 | There is no receipt for the espresso at the Anser venue for Trip-Anser. | Content cosine |
| 11 | 0.329747 | The portal verified the travel receipt for the Vantia Crest flex fare (Trip-Anser); the receipt for 528 CR is on file. | Content cosine |
| 12 | 0.328141 | The receipt export for Trip-Anser finished. | Content cosine |
| 13 | 0.318926 | The receipt for the portable slide dock for Trip-Anser is saved. | Content cosine |
| 14 | 0.304533 | The ride-hail from the Anser venue for Trip-Anser cost 31 CR. | Content cosine |
| 15 | 0.297765 | The espresso at the Anser venue for Trip-Anser cost 8 CR. | Content cosine |

Remaining candidates:

| Cosine rank | Raw cosine | Memory text |
| --- | ---: | --- |
| 16 | 0.294464 | Every submitted receipt for Trip-Anser is included in the receipt export. |
| 17 | 0.293889 | The portable slide dock for Trip-Anser cost 39 CR. |
| 18 | 0.293848 | The receipt for the airport shuttle into Velle for Trip-Anser is saved. |
| 19 | 0.284990 | The airport shuttle into Velle for Trip-Anser cost 23 CR. |
| 20 | 0.271220 | The Velle metro pass for Trip-Anser cost 27 CR. |
| 21 | 0.265776 | The receipt for the Velle metro pass for Trip-Anser is saved. |

## Interpretation and limits

- The original question can resolve the correct entity for this example without
  a generative query-rewriting step. The active hotel and cancellation both
  outrank the coffee receipt under the automatically extracted content query.
- Relevance is still imperfect: the annual travel summary ranks first and the
  PS-1 review record third. Preserving the original date prefix and generic state
  words may contribute, but this run does not isolate that cause.
- Matching all locator terms in one fact, matching the destination type through
  its name, and taking one indexed connection are explicit restrictions. Opaque
  names, split locator evidence, longer paths, or ambiguous relations are not
  solved by this probe. Shared entities identify possible paths, not entailment.
- Local parsing is brittle. A synthetic meal/March/workshop/visit question parses
  and finds the expected synthetic path. Adding the same time prefix to that
  synthetic question changes spaCy's dependency analysis and the current rule
  rejects it. The failure is preserved in `local-checks.json`; the frozen selector
  was not patched to hide it. q001 itself parses successfully.
- q001 has been examined in prior experiments. Despite no hardcoded entity/ref
  or fixed prefix here, it is a development example, not a held-out generalization
  result. No answer-accuracy or citation-score improvement is claimed.

## Verification, timing, and artifacts

The method and generated search space were saved before the embedding call.
Source and input hashes are checked before execution. An offline replay produced
identical paths, all candidate scores, and the final order. The call ledger has
one successful `query_embedding` operation and no generative model operations.
A source check found no q001 target entity, hotel-name, coffee-name, or target-ID
literals in the selector. Target labels are attached only in post-selection
`evaluation.json`.

Preparation took 0.876 seconds. The embedding client took
0.593 seconds; embedding plus ranking took 0.691 seconds.
These measurements exclude earlier memory ingestion and this investigation's
coding/reporting time.

- [Selector source](validation-memderive-automatic-query-q001-20261102/replay.py)
- [Frozen parsing, index, paths, rules, and input hashes](validation-memderive-automatic-query-q001-20261102/prepared.json)
- [Results and all candidate scores](validation-memderive-automatic-query-q001-20261102/results.json)
- [Post-selection target inspection](validation-memderive-automatic-query-q001-20261102/evaluation.json)
- [Local parser checks, including failure](validation-memderive-automatic-query-q001-20261102/local-checks.json)
- [Offline replay verification](validation-memderive-automatic-query-q001-20261102/replay-check.json)
- [Selection and API-ledger checks](validation-memderive-automatic-query-q001-20261102/result-checks.json)
- [Credential-redacted attempt ledger](validation-memderive-automatic-query-q001-20261102/attempts.jsonl)
- [Saved query vector and usage](validation-memderive-automatic-query-q001-20261102/content-vector.json)
- [Source-message provenance for the candidate pool](validation-memderive-automatic-query-q001-20261102/candidate-sources.json)
- [Artifact checksums](validation-memderive-automatic-query-q001-20261102/artifact-sha256.json)

The archive also contains the eligible fact snapshot, saved memory vectors,
embedding request configuration, and redacted raw response. No credential file
is copied. The original baseline and dependencies named by the manifest are
required for the script's full input-hash check. To replay from the repository
root with the original output directory retained:

```bash
PYTHONPATH=src:sdk/src:. .venv/bin/python outputs/memderive-automatic-query-q001-20261102-r1/replay.py
```

This replay uses saved embeddings and makes no remote calls. Do not pass
`--embed` to replay a completed run.
