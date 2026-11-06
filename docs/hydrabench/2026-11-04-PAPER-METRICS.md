# HyDRA-Bench: mapping the COLM 2026 metrics to our frozen 20-case run

The paper evaluates more than a single accuracy number. Its main correctness
metrics are **Acc_avg and Best@1/2/4/8**. It also analyzes tool-call counts and
joint SQL/text-search use, checks judge agreement against a human, and studies
training convergence and longer-hop generalization. These are different kinds
of evidence; tool-use rate is not answer accuracy or evidence-chain validity.

Source: the user-supplied *Bridging Databases and Documents: Data-Algorithm
Co-Design for Hybrid Question Answering*, marked COLM 2026. Sections 5.1–5.3
(printed pages 8–10), Table 2, Figure 5, Table 3, and Appendices 7.3–7.4 (pages
16–18) were checked against both extracted text and full-page renders. The
exact PDF hash is saved in metrics.json. No claim is made about another version
of the paper.

## What the paper measures

| Metric / diagnostic | Meaning and published protocol | What it establishes |
| --- | --- | --- |
| Acc_avg | Average correctness across eight independent runs on each dev question | Average success under repeated evaluation |
| Best@1, Best@2, Best@4, Best@8 | Fraction of questions answered correctly at least once in k independent runs | Coverage attainable within k attempts, using correctness judgments to identify successes |
| Mean tool calls per question | Section 5.2 reports 4.72 for Qwen3-8B TRACE-GRPO versus 6.14 for GRPO | A trajectory-length diagnostic; lower calls alone do not prove better reasoning |
| Tool-call distribution | Counts in 3 / 4 / 5 / 6 / 7+ bins; TRACE-GRPO: 6.5 / 41.6 / 32.6 / 13.4 / 5.7 percent | Whether behavior collapses to a shortcut or a fixed call count |
| Joint SQL and search usage | 94.1% TRACE-GRPO versus 90.7% GRPO invoke both run_sql and cortex_search | Both modalities were invoked, not that the dependency was valid |
| Single-call trajectories | The paper reports none for TRACE-GRPO | A coarse shortcut check |
| Judge alignment with a human | Appendix 7.4: o4-mini matches 199/200 human labels, 99.5% | Reliability of that judge on the stratified validation sample |
| Training curves / hint proportions | Figure 5: hint-level distribution and evaluation accuracy across training steps | Curriculum behavior and convergence, not an additional inference accuracy score |
| MuSiQue 3-/4-hop results | Table 3 repeats Best@k and Acc_avg on another task after its described fine-tuning | Longer-chain generalization in that separate experimental setup |

The primary correctness judge is GPT-o4-mini, comparing final answers with
gold natural-language explanations for semantic correctness. Exact string
matching is not the metric. The supplied paper and inspected dataset release
do not provide an exact executable judge prompt/evaluator; using the same model
would still require disclosing any reconstructed judge prompt.

For N questions and K independent runs, let c[i,r] be 1 if the judged answer is
correct and 0 otherwise:

```text
Acc_avg = sum(c[i,r] for all questions and runs) / (N * K)
Best@k  = count(questions with any correct answer in the k runs) / N
```

For 20 questions and eight runs, the first denominator would be 160 attempts;
the second remains 20 questions. Best@8 does not mean eight tool calls, eight
model turns within one episode, a majority vote, or a deployed selector that
can reliably choose the successful answer. Table 2's Best@1 and Acc_avg are
separately reported and need not be identical in a finite set of runs.

The judge-validation sample deliberately contains 100 o4-mini-positive and 100
o4-mini-negative trajectories. Its 50.0% o4-mini Acc is a consequence of that
sampling design, not the model's full-dev accuracy. The reported 99.5% human
alignment is not a claim that 99.5% of benchmark references are error-free.

## What our existing run supports

No new agents or judges were invoked for this report. The only evaluated run is
the same thinking-enabled Qwen run, one attempt per each of the 20 fixed cases.
The earlier 20 provider-parameter rejections are not independent reasoning
samples. Older models, different retrieval policies, and selective smoke reruns
are not combined into a Best@k estimate.

| Measure | All 20 | A2S (10) | S2A (10) |
| --- | ---: | ---: | ---: |
| Local single-run core reference agreement | 13/20 = 65% | 6/10 = 60% | 7/10 = 70% |
| Paper's o4-mini-judged Best@1 | Not measured | Not measured | Not measured |
| Acc_avg across eight runs | Not measured | Not measured | Not measured |
| Best@2/4/8 | Not measured | Not measured | Not measured |
| Mean attempted tool calls | 7.00 | 7.60 | 6.40 |
| Mean tool calls returning no error | 6.95 | 7.60 | 6.30 |
| Both SQL and text search invoked | 19/20 = 95% | 10/10 = 100% | 9/10 = 90% |
| Exactly one tool call | 0/20 | 0/10 | 0/10 |
| Final answer delivered (additional local metric) | 19/20 = 95% | 9/10 = 90% | 10/10 = 100% |

The local 65% is a reference-based approximation to the single-attempt
correctness concept, not an official Best@1 score. The previous semantic audit
remains separate: **9 supported, 3 defensible alternative measures, 2 conditional,
4 unresolved central premises, 1 demonstrable error, 1 no answer**. These are
not silently converted into binary labels for a new official-looking score.

| Tool attempts per episode | 3 | 4 | 5 | 6 | 7+ |
| --- | ---: | ---: | ---: | ---: | ---: |
| Our case count | 1 | 3 | 6 | 4 | 6 |
| Our fraction | 5% | 15% | 30% | 20% | 30% |

Our median is 5.5 calls and the range is 3–17. The total **140 invocations**
contains 56 SQL calls, 40 schema searches, 43 text-search attempts, and one
context read. One text-search attempt returns the explicit budget-exhaustion
error, giving 139 non-error tool calls. Parallel invocations count separately;
these are function invocations, not conversation turns. All 20 episodes,
including no-answer dev_35, stay in the denominator. The paper does not specify
its error-call counting convention, so both local conventions are disclosed.

For the both-tools metric, only `context_type=resource` qualifies as text search.
Counting all `contexthub_search` calls would incorrectly report 100%, because
schema discovery uses that function too. dev_10 uses SQL plus schema retrieval
but no text search; it nevertheless answers correctly from database identity
and weight fields. The other 19 cases each have at least one non-error SQL and
text-search result, so excluding the rejected text call does not change 95%.

## Comparison limits and interpretation

The paper's 8B TRACE-GRPO numbers (Acc_avg 53.91%, Best@8 72.89%, mean 4.72
calls, 94.1% both-tools use) are contextual examples of the metric definitions,
not a fair head-to-head baseline for our numbers. We use a different model,
20 nonrandom cases, 18,265 passages with case-informed corpus additions, a
different retrieval system, and extra BIRD schema descriptions. The paper uses
the dev evaluation protocol with eight independent runs and about 38.5 million
passages. Our percentages cannot establish that we outperform its models.

The tool APIs also differ. The paper discovers schemas through run_sql queries
to sqlite_master/PRAGMA. We use ContextHub table search/read plus data SQL.
Schema discovery contributes to both systems' work; simply removing our schema
calls would undercount it. There is no justified conversion making 7.00 versus
4.72 a controlled efficiency comparison.

Most importantly, 95% joint-tool usage does not prove reliable hybrid reasoning.
dev_11 invokes both tools but substitutes a club vice president for a national
officeholder. dev_32 retrieves a prepaid-card passage without establishing that
the database's junior card has that meaning. dev_6 invokes text search repeatedly
without obtaining the required horse-country evidence. Their invocation flags
are true while their evidence-chain limitations remain.

The paper's main results do not report retrieval Precision/Recall/F1 or SQL
execution correctness as leaderboard metrics. Our table coverage, text evidence
coverage, SQL/filter audits, and ambiguity review are useful additional
ContextHub diagnostics and should be labeled as such. Existing table coverage
is 33/42 occurrences on the first query and 42/42 cumulatively, with all tables
found in 13/20 then 20/20 cases. All 56 SQL statements execute successfully;
that does not establish semantic correctness of their filters or measures.

We can also compute episode time from the logs: mean 56.12 seconds and median
39.49 seconds across all 20 episodes. This is supplementary engineering data,
not a reported metric from the paper's Evaluation Results. It is per-episode
elapsed time, not total batch runtime or a hardware-normalized speed comparison.

## Recommended reporting protocol

Keep three explicitly separated parts in future reports:

1. **Paper-style correctness:** fixed judge/reference policy, single-run result,
   and Acc_avg plus Best@1/2/4/8 only after eight independent attempts for every
   case under the same configuration. Do not selectively rerun failures or count
   internal tool retries as new runs. If the prior one-run configuration cannot
   be reused exactly, start a separately labeled eight-run experiment.
2. **Paper-style behavior:** mean and distribution of tool calls, joint SQL/text
   usage, and single-call rate, with a precise call/error convention.
3. **ContextHub-specific diagnosis:** table/text coverage, successful execution
   versus semantic SQL errors, cross-source evidence validity, and the separate
   original-meaning review of ambiguous references. A binary reference score
   cannot replace that review.

Judge-human agreement requires independent human labels; our coding-assistant
review is not such a study. RL curriculum/convergence and MuSiQue generalization
remain unmeasured and outside this integration experiment.

## Artifacts

- [Deterministically computed metrics and per-case call accounting](artifacts/20261104-paper-metrics/metrics.json)
- [Recompute script](artifacts/20261104-paper-metrics/summarize.py)
- [Observed answer-model token usage and duplicate payload audit](artifacts/20261104-paper-metrics/token-usage.json)
- [Original reference-based run report](2026-11-04-MIXED-QWEN.md)
- [Original-meaning semantic review](2026-11-04-SEMANTIC-REVIEW.md)

From the checkout root, using the standard Python library only:

```bash
python3 docs/hydrabench/artifacts/20261104-paper-metrics/summarize.py /tmp/hydra-paper-metrics-recomputed
```

The source PDF is read only to record its hash. The script performs no network,
agent, judge, or database calls.
