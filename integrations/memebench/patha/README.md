# Path A S8: orchestration, independent scoring and reports

S8a/S8b are implemented for independent acceptance. **No real MEME smoke,
provider call, development trial or formal experiment is authorized.** Synthetic
fixtures and their parameters are test data, never formal defaults. The accepted
S8 prerequisite version remains archived; R1/R4 passed prerequisite review §8.
R2/R3 were explicitly left outside that revision and are not described as fixed.

## Boundaries and core ownership

`system_factory.create_system` translates explicit `SystemConfiguration` and
calls the sole core `create_knowledge_service`. The experiment layer does not
implement extraction, search, adjudication, propagation, candidate generation,
profile strategy, verifier, safe commit or token pricing. Core/SDK do not import
MEME. S7 HTTP/SDK and the accepted prerequisite core code are unchanged by S8.

The public phase methods remain `prepare`, `discover_prepared`, `finalize_graph`,
`get_prepared`, `discovery_report_reference`, `get_graph_handoff`,
`ingest_assertion_change` and `execution_records`. `ingest_assertion_change`
preserves intact raw session JSON and supports direct quoted assertion carriers,
including partial facts and finite distinct matches in one input. It does not
infer authority from benchmark gold, roots, questions, paths, task types or hops.
Ambiguous/unsupported changes stay pending with their actual receipt and costs;
safe rejection is not a claim of semantic support. Deletion text is preserved,
but Del is not implemented or scored.

`PublicRQ1Runner` is the S8 route (also exported by `rq1_runner.py`). The old
`RQ1Runner` and `rq1_scorer.py` remain unchanged for S4 reproducibility. Only
selective and full-history run, with identical frozen conditions apart from the
search scope/expansion strategy and its identity. Both consume the same sealed
extraction and original source versions. `PublicRQ1Result` exports real graph
and S4 scoring evidence, shared/arm operation IDs, original attempts and measured
shared/arm wall durations. Snapshotting/report exports are outside those durations.
A failed full-history control retains its failure/costs and cannot erase, replace
or upgrade the already captured selective main.

Every RQ1 run/episode/replicate needs an explicitly named, clean exclusive account.
The runner rejects account reuse; it does not clear data or silently choose an
account. Capture occurs after selective replicate 0 and before any full-history
work. The callback must return the exact `DatabaseSnapshot` for that run,
configuration, handoff, account and agent. Other repeats cannot become the P2 main.

## Chronology and independent copies

`database_snapshot.py` captures all public SQL tables and pg_dump in one exported
repeatable-read snapshot. Capture validates the account's entire history and
artifact closure, including hidden/deleted history. Restore creates a new
`contexthub_s8_*` database with original IDs, ownership/ACLs, versions, watches,
artifacts and proofs; it compares exact table hashes and ordinary-role access.
There is no graph rebuilding, ID rewriting, destructive cleanup or default DSN.
Only dedicated localhost UTF8/011 instances and NOSUPERUSER/NOBYPASSRLS ordinary
roles are supported. Keep the source quiescent at capture and never add unrelated
experiment history to the account in a restored branch. Other tenants may exist
in the database dump but remain outside the account's maintenance history.

`RQ2Coordinator` verifies the supplied main run, input, snapshot and configuration
identities before restoration. It restores independent copies for exactly:

- `local/base-only`
- `full/strong-only`
- `adaptive`

All copies must have identical initial account-state hashes. The coordinator
reuses core registered event/watch bindings, including original artifact hashes
and permissions. It never selects a different repeat or a full-history graph.
Each profile follows this order:

1. Export and persist the exact before-state seal.
2. Release and answer the complete before-question bundle.
3. Ingest every intact source through the inclusive after-session boundary.
4. Perform one post-ingestion maintenance sweep over actual bound core events.
5. Export and persist the exact after-state seal.
6. Release and answer the complete after-question bundle.

Official `position_after_session` is inclusive: before ends at `before + 1`;
changes use `[before + 1, after + 1)`. Session labels never remove text. There is
no maintenance per question, Cas/Abs category, hop or filler message. A round is
a coordinator scheduling sweep, **not a claim of one core invocation**:
`maintenance_rounds`, `core_event_executions`, original event/operation IDs and the
full timeline are exported separately. The verified synthetic case has one root
event, two downstream targets, a raw change plus deletion text, and a later filler
source. There is no new batch API or change to production event scheduling.
General repeated changes to a frozen graph and interacting independent root
events are not certified; existing version/pending protections remain effective.

`QuestionVault` accepts only boundary/hash headers before sealing. Only release
opens question payloads. Runtime preflight reads S1 identity manifests, not
question/label payloads. Offline `package-questions` validates S1 bundles and
creates new per-phase files and boundary identities without rewriting S1 files.

`AnswerRunner` passes question text and ACL-readable sealed contexts to a model
with no maintenance tools. It uses the core HTTP client/attempt ledger in a
separate account. The complete maintenance account export, including attempts,
proofs, causes, clocks and IDs, must be identical before and after each answer
phase. Errors/empty answers remain rows. Answers, maintenance and judge logs are
separate. No privileged label fields are inserted into core traces.

## Staged configuration and authorization

`run_gates.py` is additive; S1's six validator files and archived manifests are
not rewritten to make their hashes pass. `RunManifest` and `RunAuthorization`
require explicit code/dirty-diff/dependency identities, parent S1 contract,
inputs/labels/scorer/configuration/price/timeout identities, finite development
manifest, repeat/aggregation/seed protocol, episode membership and stage grants.
The code dependency closure includes every Python file in core, SDK and Path A.
The parent S1 manifests pin the original 100 episodes, 20 locked / 80 development
split, 294 Cas/Abs records and pl_030. Runtime and scoring inputs must match the
old frozen bytes. A second scorer identity records the new implementation;
S1's historical pending-scorer declaration is retained as history.

RQ1 preflight does not require a graph that RQ1 has yet to build. RQ2, answer and
judge stages require the sealed selective main graph identities. All RQ1 arms
are preflighted before constructing services; missing authorization/configuration
or price rejects before external clients. A successful gate-only contract test
is not a runnable manifest. No complete formal manifest is provided here.

All experiment choices remain explicit: models/endpoints/prompts/decoding,
search scope/expansion/aliases/rules, finite lambda grids and selected values,
timeouts/retries, prices, repeats, aggregation, bootstrap seed and auxiliary
cost tolerances. The core HTTP client currently has no provider-seed parameter;
a manifest requiring a supported provider seed **rejects**, rather than dropping
it. Do not falsely mark a provider unsupported to bypass this restriction.
A future chosen configuration needing that parameter requires separately scoped
core support before running.

Synthetic CLI plans require hash-pinned HTTP response fixtures and use
`httpx.MockTransport`. JSON fixtures replay exact request hashes. A trusted local
`.py` fixture may expose `build_transport()` for generated IDs; it must return a
MockTransport. Execute fixture code only under the same explicit external-network
block used by the contract harness. No fixture/model values become formal values.
Credentials come only from explicitly named environment variables, not `.env`;
plans export the variable names, never the secret values. Endpoint URLs reject
embedded credentials and query/fragment secrets.

## Independent scoring and complete denominators

`scorer.score_report` reuses S4 `score_rq1`'s semantic path checks: `kappa_P1=1` requires
at least one complete semantically reviewed, version/evidence-correct necessary
path per frozen event-target. Reachability and runtime verifier PASS do not
certify that path. S4 `PathReview` identifies reviewer/method/version, exact
reference record, node versions, edge hashes and original evidence spans.

P2 uses separate `CaseReference` and `P2Review` materials. The former binds the
reference's recoverable-Cas judgment once for all profiles/repeats. The latter
binds run/profile/replicate, graph, before/final target versions, sealed terminal
state, original source snapshots/spans and independent human semantic judgment.
Both require `offline_human_semantic_review`, reviewer/version and rationale.
Actual fresh comes from SQL status/validity/deletion/cause state, not work status.
Missing/failed/unknown or unreviewed records never become correct automatically.
The scorer emits every frozen record for every profile/repeat and reports:

- All-case joint P1/P2 results, retaining P1 misses.
- P2 conditional results on selective main replicate 0's correctly delivered targets.
- Official before/after answer results and their separate categories.

S8-R3 adds an optional complete-episode view to both scorers. With no view, the
original whole-parent denominator remains required. With a view, the complete
parent labels/paths and contract are still validated; every record of each
selected episode is retained, including missing results, all tasks and repeats.
No subset S1 file or replacement S1 contract is created. The new scorer code
requires a new dependency identity; archived S1/S4/S8 identities remain intact.

`ScorePlan` development episodes must equal the run manifest's declared episodes
and be a subset of its finite development list, itself restricted to S1's fixed
80 development episodes. Full frozen input/label/path files remain the inputs.
Only that authorized view is evaluated; locked-side results/reviews cannot enter
the development score. Offline full-material integrity checks are not semantic
evaluation or permission to use forward-locked content for development choices.
The emitted `scope.json` binds parent file hashes, lock identity, manifest hash
and every selected episode/record ID. Scoring grants no execution authorization.

`official_adapter.py` hash-loads only the official `LLMJudge` class and reuses its
actual `u_check` prompt semantics; it does not run the module CLI or instantiate
its provider client. Only Cas/Abs are evaluated. Like official `judge_episode`,
the first `entity_values` entry supplies gold, **not `gold_answer`**. Before uses
the common prompt; after Cas expects the new definitive value; after Abs checks
uncertainty against the old value. Categories are real (both pass), trivial
(after only), knew_but_failed (before only), never_knew (neither), plus an explicit
unknown category for unavailable judgments. Empty/failed answers fail; missing
judge evidence cannot pass. `OfficialJudgeRunner` uses separate core-recorded
attempts, explicit configuration and a separate stage authorization.

`tau_FF=0%` and `rho_P2=90%` are exact observed-count gates. False-fresh main uses
actual fresh; the all-case ratio is auxiliary. Zero fresh is N/A, not zero or
an automatic pass. Unknown fresh/review status prevents certification. Cas
all-stale remains exposed through recovery, and Abs correctly unresolved is
reported separately. Cas/Abs internal quality and official real-pass quality
are four separate metrics with a 5 percentage-point noninferiority margin.

## Statistics, costs and report artifacts

`bootstrap.py` draws 10,000 paired whole-episode clusters, retaining every case,
arm and repeat within each draw. It records seed, episode order, draw-index hash,
point estimate, one-sided 95% bounds and two-sided 95% percentile interval
(linearly interpolated quantiles). `pooled_counts` pools counts; the alternative
`mean_episode_rates` equally weights per-episode rates. Both are explicit choices.
An undefined point or resample invalidates the interval; samples are not discarded
to fabricate support. Absolute frozen gates use the reported exact counts.

Differences use candidate minus comparator: cost upper < 0; adaptive-local
recovery lower > 0; all four adaptive-full quality lowers >= -0.05; false-fresh
upper <= 0 where defined. Zero-fresh N/A comparisons use raw errors and the frozen
Cas recovery/Abs unresolved rule; partially undefined sampling with defined point
rates does not gain that exception. Raw false-fresh also stays separate for
all cases, Cas and Abs.

`ledger.py` validates/recomputes exported core attempts using core pricing and
append contracts, deduplicates identical exports and rejects conflicting facts.
It never appends an experimental charge. Shared extraction is included once in
each arm's logical comparison and once in the physical bill. Failed/retried calls,
change location, local work, maintenance, answers and judge remain attributable.

`C_billed` is the recomputed external simple-token-tariff axis and is explicitly
labelled as such, not an exact supplier invoice. Missing usage, price, model
identity or unfinished attempts make it unknown. `T_wall` is measured around the
actual phase, not a sum of overlapping nested spans. `W_local` retains individual
core work metrics. S4/S2's conservative `billed_total`/`complete_monetary_amount`
and unpriced-local flags remain in the export; local work is never asserted free.
A complete-cost comparison requires the monetary bound plus frozen, satisfied
wall/local-work tolerances; missing dimensions/tolerances cannot certify it.

`reporting.py` validates all file hashes and scorer dependency identities, joins
runtime exports with offline reviews, and emits `scores.json`, `statistics.json`,
`costs.json`, `report.md`, the input `plan.json` and a file-hash index. Failed
runs retain partial account/attempt exports; incomplete cost attribution is
labelled rather than silently treated as zero. Per-run state artifacts preserve
source versions, graph/watch/proof/work/operation/event/attempt/retry chains.

Formal scoring still requires the original 100 episodes and retains all 294
Cas/Abs records. `forward-locked/` independently summarizes the original 20
complete episodes from those same scored rows, without a second run or new
selection. Its scores, P1/conditional/task/official summaries, costs, Markdown
and paired 10,000-resample statistics use that fixed episode membership and the
same explicit repeat/seed/aggregation protocol. Its scope binds the parent
`scores.json` hash. The top-level file index covers both reports. This is
forward-locked evidence, not an unseen test. The judge ledger currently has
run-wide attribution only: the subset explicitly leaves judge cost unavailable
and points to the parent costs, rather than inventing a per-episode allocation.

S8 review §7 accepts R1 (cost export after a specific injected return-boundary
failure) and R2 (contradictory semantic review and unchanged Cas terminal state)
as unfixed limitations. This R3 revision does not change those paths or claim
their original counterexamples are resolved. Incomplete costs or inconsistent
review materials must not be described as complete valid experimental results.

## CLI and acceptance

Use `python -m integrations.memebench.patha.cli STAGE --plan /absolute/plan.json
--output /absolute/new-output` with the repository and `src` on PYTHONPATH.
The output directory must be new. Stages and plan schemas are:

| Stage | Plan |
| --- | --- |
| `package-questions` | `maintenance_inputs` and `answer_bundles` FileIdentity objects; offline only |
| `freeze-scorer` | parent S1 contract FileIdentity; creates a new code/dependency identity, no run grant |
| `preflight-rq1`, `preflight-rq2`, `rq1`, `rq2` | `cli.StagePlan` (preflight needs no output) |
| `judge` | `judge_runner.JudgePlan`, existing sealed outputs and separate authorization |
| `score` | `reporting.ScorePlan`, exact runtime/review files and explicit statistical protocol |

Pydantic schemas expose all required fields; no real-ready example manifest or
implicit experiment parameter values are supplied. `rq1` writes per-episode/main
snapshots/results; their FileIdentity/run IDs then populate the RQ2 plan. A new
manifest/grant binds the produced main graphs for later stages. Do not edit an
old freeze identity in place.

Contract tests include the required chronology, coordinator, official contract,
scorer, bootstrap and integration runner files, plus CLI, gate and ledger tests.
The integration uses the actual public factory/service and S3–S7, three restored
SQL databases, independent synthetic reviews and official judge MockTransport.
It records original and explicitly normalized model requests while perturbing
privileged labels/questions, compares real business state, and checks pending
changes and failed answers/control costs. This proves synthetic contracts, not
real model effectiveness or general multi-root/continuous-update semantics.

Database testing requires a newly provisioned dedicated instance, explicit
`CONTEXTHUB_INTEGRATION=1`, `S8_TEST_DATABASE_URL`, `S8_TEST_IDENTITY`,
`S8_TEST_ADMIN_URL`, `S8_TEST_PG_BIN`, offline legacy/background replacements and
an external-network guard. Never use default db_pool/clean_db, old databases,
implicit `.env`, model downloads or real MEME smoke. Exact commands, failures,
final hashes and retained evidence are in the S8 implementation handoff.
