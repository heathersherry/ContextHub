# MemDerive tripops_w1: replace the entry while preserving successful scoped ranking

Completed: 2026-11-02T15:19:35.013547+08:00 (Hong Kong, UTC+8).

## Baseline and result

This experiment returns to the successful automatic q001 implementation. It does
not continue the parserless global-cosine experiment. Only locator proposal
construction changes. The original `candidate_paths` function is loaded from a
byte-identical source copy, and the path-ordering/selection statements have the
same Python abstract syntax as the successful version.

**q001 is preserved exactly:** same content query, entity index and paths,
Trip-Anser scope, 21 candidates, final 15 references/order, and every score.
Because its official prompt and evidence are identical, its saved successful
answer is reused and rescored. Answer accuracy, witnessed accuracy, and citation
precision/recall/F1 remain 1. This is an exact-input replay, not a fresh answer call.

Five questions produce 15 items through the scoped-content method. Two scopes
contain fewer than 15 candidates and take the previously authorized, separately
labeled vector fallback. Across all seven delivered answers, answer/witnessed
accuracy is 2/7. **The scoped-method subgroup is 1/5, and the fallback subgroup
is 1/2.** Among the six questions other than q001, four complete scoped selection
but none answers correctly; two use fallback, of which q002 answers correctly.
Thus no new non-q001 success is claimed for the scoped method.

## What changed, and what stayed fixed

The removed constraints are the mandatory `WDT`/`det` interrogative match and
exactly one prepositional noun phrase. Local spaCy now proposes locator spans:

1. Consider multiple qualified locator phrases independently of the interrogative
   and sentence-root tags. Include nested qualifiers, such as a destination,
   rather than deleting conditions that did not match.
2. Do not treat a plural noun phrase as a single-entity scope. Retain its content
   conditions and consider an explicit named owner/location instead.
3. If qualified locators produce no path, consider existing entity labels in
   locator phrases or possessive spans. Proper-name tags order these proposals;
   they are not mandatory. Explicit names come from the question and index, not
   from gold or manually supplied trip names.
4. The first proposal with paths uses the existing resolver. Delete only that
   proposal's source span to form the content query, retaining the time prefix.
   No generative rewrite is used. Some residual queries are grammatically awkward;
   they are preserved in the audit rather than polished by an LLM.

The following remain fixed: all-qualifier anchor matching, lexical entity-type
matching, one connecting-fact expansion, path ordering by best content cosine in
the target pool, selecting one target entity's complete index, preserving path
support, cosine-only ranking within that scope, and the 15-item limit. There is
no global-vector candidate union inside the primary method.

This is still a narrow local entry prototype, not a claim of general semantic
parsing. The rule set was frozen before the new embedding and answer calls.

| Question | Entry used | Removed original span | Content query |
| --- | --- | --- | --- |
| q001 | qualified_locator | for the February conference trip | As of 2026-10-07 09:00, which hotel booking is currently active? |
| q002 | qualified_locator | for Conference Falco | As of 2026-10-07 09:00, which hotel is booked? |
| q003 | explicit_entity_locator | Skarren | As of 2026-10-07 09:00, which hotel is booked for the conference trip to? |
| q020 | explicit_entity_locator | Vera's | As of 2026-10-07 09:00, how many of 2026 business trips go outside her home country? |
| q050 | explicit_entity_locator | Vera's | As of 2026-10-07 09:00, who approves reimbursement claims? |
| q064 | explicit_entity_locator | Vera's | As of 2026-10-07 09:00, which of trips need a fully itemized lodging invoice for their claims? |
| q090 | qualified_locator | for the February conference trip | As of 2026-10-07 09:00, what is the claimable total, in CR? |

## Results by method

All final answer inputs contain exactly 15 memories. When the original scoped
method has fewer than 15, it is not silently padded or widened. That case uses
the separately recorded fallback agreed in the earlier transfer test.

| Question | Scope chosen by original resolver | Scoped candidates | Answer input method | Answer | Witnessed | Answer execution |
| --- | --- | ---: | --- | ---: | ---: | --- |
| q001 | trip-anser | 21 | Scoped content | 1 | 1 | Exact-input answer reused |
| q002 | trip-falco | 3 | Vector fallback | 1 | 1 | Exact-input answer reused |
| q003 | skarren skyline standard fare | 1 | Vector fallback | 0 | 0 | Exact-input answer reused |
| q020 | vera | 40 | Scoped content | 0 | 0 | Fresh answer |
| q050 | vera | 40 | Scoped content | 0 | 0 | Fresh answer |
| q064 | vera | 40 | Scoped content | 0 | 0 | Fresh answer |
| q090 | trip-anser | 21 | Scoped content | 0 | 0 | Fresh answer |

q002 and q003 have the same fallback evidence and official prompt as their saved
parserless-run answers, so those answers are reused and all metrics recomputed.
The other four non-q001 prompts are new and receive fresh answer calls. Model
settings remain GPT-5.6-sol, temperature 0, reasoning effort `none`, output limit
2048, with the unchanged official v9 prompt and deterministic provenance-v2 scorer.

## What the remaining failures mean

- **q002 resolves Trip-Falco**, whose scope contains just three eligible memories:
  the correct hotel booking, the annual trip list, and the conference-to-trip
  connection. The correct hotel ranks first inside that scope. This is a
  cardinality shortfall under the exact-15 requirement, not a `which` parse
  failure. The fallback's success is not credited to the primary scoped method.
- **q003 binds to a fare label containing Skarren**, with only one candidate.
  The unchanged resolver uses entity-name tokens as a type approximation and
  chooses paths by their best content score. It does not reliably distinguish a
  geographic destination from an airline/fare label sharing the token. This is
  an entity-resolution limitation exposed after the entry gate is removed.
- **q020, q050, and q064 choose the 40-fact Vera index.** That is a legitimate
  explicit-name starting point, but it does not establish a complete set of her
  trips or the cross-entity approval/policy chain. The single-entity scope is too
  limited for these questions, and the old snapshot's missing facts/rules remain.
- **q090 correctly locates Trip-Anser** and uses its 21-candidate scope, but the
  content score still drops important costs. The active hotel is rank 17 and
  the cancellation rank 19; both miss the 15-item list. The model abstains rather
  than returning the prior incorrect 360 total. Citation F1 becomes 0.350877, but
  answer accuracy remains 0. No answer-success improvement is claimed.

The q001 hotel facts remain at positions 4 and 6 with the successful booking
question. Their lower ranks under q090's different total-cost query are an
additional ranking/coverage issue, not a regression of q001's preserved ranking.

This separates three issues: input-form coverage, defining a sufficient and
correct scope, and selecting all needed evidence inside the fixed budget. Fixing
the first does not automatically solve the other two. No additional scope or
ranking change was introduced after inspecting these results.

## Verification and accounting

- q001 content query, full search state, paths, candidate scores, and final order
  match the successful snapshot exactly.
- The original path-ordering and selection statements match structurally.
- Local replay reproduces all seven entry decisions and ranked scopes.
- All 56 official metric records reproduce from saved answers.
- All seven final evidence lists contain 15 unique references, and every predicted
  source citation occurs in its supplied evidence.
- Three reused answers have exact matching evidence, prompts, and model protocol.
- The official scorer source tree matches the pinned release hash:
  `8f9c69a685740939479b4a849fc6449dd42d71ff2eec8f4c88045912d04731f9`.

New calls: **6 embeddings using the existing model, 4 answer calls, 0 generative
retrieval calls, and 0 scorer-LLM calls**. Three saved answers and the q001 content
vector are reused. Execution took 28.898 seconds, excluding preparation.
New embedding usage reports 276 input tokens;
new answer usage reports 94,535 input tokens and
95,491 total tokens. The unusually large provider
answer-token accounting seen in prior runs is preserved, not corrected by an
estimate. No monetary cost is inferred.

The same old memory snapshot is used; current extraction-prompt fixes are not
reingested. These are already inspected development questions. Shared production
retrieval and the successful historical experiment files are unchanged.

## Artifacts

- [Baseline equivalence checks](validation-memderive-preserve-scope-tripops7-20261102/baseline-checks.json)
- [New entry and unchanged ranking statements](validation-memderive-preserve-scope-tripops7-20261102/entry.py)
- [Byte-identical successful source](validation-memderive-preserve-scope-tripops7-20261102/source/q001_baseline.py)
- [Locator proposals, exclusions, and path candidates](validation-memderive-preserve-scope-tripops7-20261102/entry-audit.json)
- [Scoped ranks, fallback labels, final 15, and prompts](validation-memderive-preserve-scope-tripops7-20261102/prepared.json)
- [All official results](validation-memderive-preserve-scope-tripops7-20261102/results.json)
- [Summary](validation-memderive-preserve-scope-tripops7-20261102/summary.json)
- [Historical comparison](validation-memderive-preserve-scope-tripops7-20261102/comparison.json)
- [Verification](validation-memderive-preserve-scope-tripops7-20261102/verification.json)
- [Call ledger](validation-memderive-preserve-scope-tripops7-20261102/attempts.jsonl)
- [New-call usage](validation-memderive-preserve-scope-tripops7-20261102/new-call-usage.json)
- [Runner and input manifest](validation-memderive-preserve-scope-tripops7-20261102/run.py), [manifest](validation-memderive-preserve-scope-tripops7-20261102/manifest.json)
- [Artifact hashes](validation-memderive-preserve-scope-tripops7-20261102/artifact-sha256.json)

The archive includes credential-redacted raw responses, saved content vectors,
and reused answer sources. No credential file is copied. This small MemDerive
experiment is not part of the MEME-32k 100-case index.
