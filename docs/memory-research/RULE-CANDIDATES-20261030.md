# Expanded rule candidates (2026-10-30)

## Prompt review follow-up

The rule-screening rubric now follows TASK, HOW TO DECIDE, BOUNDARIES,
EXAMPLES and OUTPUT. Its decision boundary requires a stated applicable case
and treatment, prerequisite, constraint, calculation or matter-to-matter effect;
the presence of policy vocabulary alone is insufficient. Mixed fact/rule
sentences remain eligible for later separation.

Thirteen illustrative sentences include five positive/negative contrasts,
one MEME-style conditional, an uncertain draft and a mixed sentence. Every
example was checked with the current production `rule_candidates`: each yields
exactly one candidate. Negative examples match `approved by`, `before`, `after`,
`above`, or `policy`, then illustrate why the sentence still lacks a rule.
These labels are intended rubric examples, not measured model predictions.

Eighteen focused fixture tests passed; one database-backed test was deliberately
not selected for this prompt-only follow-up. JSON/output keys and diff checks
passed. The vocabulary, forgetting rubric and execution flow were not changed.
No hosted JEV call or benchmark was run.

This implements the keyword-prefilter option on `codex/memory-research` in
preparation for a later one-world MemDerive / one-episode MEME comparison.
No benchmark or hosted model was run.

## Candidate selection

`jev_candidate_gate.py` uses the exact 48-expression `domain_extended` vocabulary
from the previous MemDerive audit:

```text
if, unless, requires, eligible, except, starting from,
require, required, requirement, requirements, need, needs,
must, only, when, once, never, cannot, before, after,
without, while, until, within, above, below, rule, rules,
policy, excluded, excludes, exclude, exception, exceptions,
effective, at least, at most, no more than, from today onward,
approved by, handled by, claimable, refundable, allowance,
cap, caps, count toward, counts toward
```

Matching is case-insensitive, with word boundaries and whitespace-flexible
phrases, without stemming. User/assistant messages remain eligible. Hints
overlapping recognized code regions cannot nominate a sentence. Original text
and source offsets remain available for auditing.

The unit is now one source sentence, rather than one `if` occurrence. Multiple
hints in a sentence produce one question. Identical sentences at different
locations remain distinct. The obsolete `candidate_text` function and
`<target>` injection are removed. Audit fields become `hint_start`/`hint_end`
alongside `sentence_start`; these offsets are not sent to the models.

## Prompts and flow

1. Expanded hints nominate rule sentences; the forgetting filter is unchanged.
2. With JEV enabled, the existing batched request judges the whole sentence.
   `extractor_rule_jev.json` covers conditional outcomes, prerequisites,
   responsibility, validity, eligibility, exclusions, thresholds, calculations
   and policy revisions. It distinguishes policies from individual event facts.
3. Choices remain `rule`, `not_rule`, `unclear`. The first and third proceed to
   confirmation. Mixed sentences and plausible drafts/proposals are not rejected
   solely for lack of context or adoption evidence.
4. The existing joint extractor reads the full session and accepted candidates.
   `extractor_rules.txt` confirms support/adoption/status and normalizes the
   applicable scope and prescribed treatment. `extractor.txt` prevents policy
   outcomes from being duplicated as already-true facts. Explicit policy-status
   reports can be content, without promoting a draft to an effective rule.
5. The existing output remains `items` with `content`, `rule`, `forget_request`,
   evidence in `source_message_nos`, and rule text in
   `If <condition>, then <consequence>` form. Storage and downstream contracts
   are unchanged.

Prerequisite direction is explicit in the prompt: `Enrollment in A requires
completing B` may become `If enrollment in A is to be permitted, then B must
already be completed`, never `If B is completed, then enrollment in A is
permitted`. Scope, exceptions, uncertainty and effective dates must be preserved;
the event timestamp alone does not establish a policy's effective date.

The runtime's optional JEV/backend defaults are unchanged. Without JEV, expanded
candidates go directly to the existing joint extractor. The later JEV experiment
must select the JEV backend explicitly.

No rule candidates means no rule-screening questions, not no model processing:
the full-session extractor still reads ordinary content, and forgetting
candidates can independently require JEV. The earlier 480/4,692 estimate concerns
event nomination, not sentence-level question count or total pipeline cost.

## Validation

- Exact equality with the frozen 48-expression audit vocabulary verified.
- 158 relevant tests passed in 7.03 seconds. Models/embeddings used fixture HTTP
  responses. A newly created dedicated local PostgreSQL database was removed
  afterward; existing experiment databases were not used.
- One optional MemDerive adapter test module was skipped because the separately
  installed DerivEval source is not on the environment's import path.
- The initial run had 157 passes and one environment-path failure for the MEME
  official judge. Its existing public source was located, its frozen SHA-256
  verified, and the rerun used `MEME_OFFICIAL_JUDGE` to point to that source.
- Ruff E9/F passed on changed Python files.

These tests verify wiring, code exclusion, source anchors, candidate batching,
prompt loading and unchanged output contracts. They do not establish model
classification accuracy or token savings. The previous audit's 127/132 strict
and 209/214 dependency-inclusive coverage is descriptive of the data used to
assemble the vocabulary. It is not production extraction recall; its denominator
also includes historical drafts that the extractor must not treat as in-force
policies.
