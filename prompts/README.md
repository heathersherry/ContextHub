# Prompt locations

Core prompts ship with the Python package in `src/contexthub/memory/prompts/`. MEME answer and scoring templates live in `integrations/memebench/memory_research/prompts/`. The table below covers the default LLM flow and optional Jev paths. In the Jev P2 path, Jev classifies intent; the LLM handles memory processing.

Rule and ordinary-content extraction must both provide a list of `topics` strings. Regardless of the input language, use short English descriptions of the subject matter that do not depend on its current value: for example, `residence` or `deployment platform`, rather than a city name or a generic entity type such as `location`. Forgetting and correction requests describe the matters being handled; rules cover both conditions and consequences. Keep independent facts separate even when several topics are present. Topics guide retrieval; they do not establish entity identity or authorize updates or deletion, and need not be repeated in later model inputs.

A topic pair with cosine similarity **greater than `--topic-similarity-threshold` (default 0.8)** can retain an additional candidate. Existing text, entity, and keyword paths remain active. New-content processing, correction, default direct processing, forgetting, P1, and repair retrieval share topic matching. Answer queries do not add a topic-extraction call and continue to use answer retrieval. Maintenance propagation follows confirmed rule bindings.

Extraction preserves team, project, and other subjects from the original text. Do not turn `we/our` into an individual `the user/their`. Resolve references only from the current session; retain collective references when ambiguous, and do not invent distinct identities merely because wording changes between `team` and `group`. Maintenance prompts for ordinary content, default direct processing, correction, and forgetting assess subject continuity using candidate names, ownership, scope, and specific subject matter. These labels are not automatically synonymous, and a single candidate does not prove identity. Distinct named teams and projects must remain distinct.

Candidate keyword evidence excludes generic subject words `user/person/team/group`. These words alone cannot pass the moderate-vector-similarity-plus-keyword filter or qualify as bare entity names in that filter. Existing strong text similarity, explicit entities/identifiers, and topic conditions still apply. Semantic fallback retrieval for new content and forgetting shares this filter; a retrieval match does not confirm subject identity.

Answer materials are selected by question text. Current-fact queries retrieve only valid current facts, excluding rules before ranking. Explicit policy, rule, condition, or hypothetical questions may retrieve valid rules, rendered separately as `conditional_rules`. Historical questions use historical retrieval. Routing makes no extra model call and does not read MEME task labels. The official answer template remains unchanged.

`answer_guidance.txt` contains the complete official MEME / MD-Flat answer template, including `{context}` and `{question}`, and is byte-identical to `evaluation/answer.txt`. The answer entry point formats it once, without appending completeness, attribute-scope, or conditional-rule guidance. The actual template is checked against its official fingerprint. This template change left material construction and retrieval unchanged. Earlier guidance experiments are retained in `outputs/answer-guidance-replay-20261023/` and `outputs/answer-scope-pilot-20261023/`. The full material-reuse experiment in `outputs/official-answer-replay-20261023/` completed 100 cases: official score 431/694, Agg 30/100, and none of the 25 on-call/document questions passed in full. These results did not establish that the official template resolves aggregation omissions. A shared answer template improves comparability, but this was not a complete controlled comparison with MD-Flat using identical materials, maintenance models, and providers.

| File | Purpose |
| --- | --- |
| [answer_guidance.txt](../integrations/memebench/memory_research/prompts/answer_guidance.txt) | Complete official answer template; inserts only context and question. |
| [extractor.txt](../src/contexthub/memory/prompts/extractor.txt) | First extraction pass: rules. |
| [extractor_rule_jev.json](../src/contexthub/memory/prompts/extractor_rule_jev.json) | `--rule-gate-backend jev`: reads roles and complete sentences containing `if`; supplies shared instructions once, excludes `not_rule`, and forwards `rule`/`unclear` candidates for confirmation. |
| [extractor_rules_normalize.txt](../src/contexthub/memory/prompts/extractor_rules_normalize.txt) | Reads the full current session to confirm user-established or adopted rules, resolve references, and normalize them. May reject every candidate; each output must cite a forwarded candidate ID. |
| [extractor_facts.txt](../src/contexthub/memory/prompts/extractor_facts.txt) | Second extraction pass: separates ordinary content and supplies storable text, topics, and current/historical scope. Removes conditional rules while preserving request intent and verbatim quotations. Jev `new` results with no candidates are stored directly from this output. |
| [p2_direct.txt](../src/contexthub/memory/prompts/p2_direct.txt) | Direct processing: additions, duplicates, updates, deletion, and related actions. |
| [p2_intent_jev.json](../src/contexthub/memory/prompts/p2_intent_jev.json) | `--p2-direct-backend jev`: classifies content as new/forget/correction/hold without old candidates; rules are stored directly. |
| [p2_new.txt](../src/contexthub/memory/prompts/p2_new.txt) | After Jev selects `new`, the LLM compares old facts and produces addition, confirmation, update, historical, or invalidation actions. |
| [p2_correction.txt](../src/contexthub/memory/prompts/p2_correction.txt) | After Jev selects `correction`, the LLM produces correction, replacement, or retraction actions across old facts/rules. |
| [p2_forget_scope.txt](../src/contexthub/memory/prompts/p2_forget_scope.txt) | Selects matters to delete from explicit entity matches across all versions and semantic fallback candidates retained by existing 0.50/0.35 thresholds or topic matching. Forgetting a person's identity/relationship includes shared-activity clues clearly referring to that person. Matches do not authorize automatic deletion; the Jev path need not select a primary target first. |
| [p1_conditions.txt](../src/contexthub/memory/prompts/p1_conditions.txt) | Selects condition-source matters and affected matters separately for each rule, returning two reference sets. |
| [p1_jev.txt](../src/contexthub/memory/prompts/p1_jev.txt) | `--p1-backend jev`: judges condition/affected roles separately per candidate, batching requests for the same rule. |
| [p2_conditions.txt](../src/contexthub/memory/prompts/p2_conditions.txt) | Checks complete source states and targets along bound relationships; returns invalidated facts and rule-relevant change IDs. |
| [p2_repair.txt](../src/contexthub/memory/prompts/p2_repair.txt) | Repairs old facts using rules and factual evidence. |
| [output_retry.txt](../src/contexthub/memory/prompts/output_retry.txt) | Correction prompt after output validation fails. |

## Evaluation: answers and scoring

Files in [evaluation/](../integrations/memebench/memory_research/prompts/evaluation/) remain active:

- `answer.txt`: reference copy of the official answer template; runs read the identical `answer_guidance.txt`.
- Seven `judge_*_prompt.txt` files: official scoring templates used for judging or official-template consistency checks.
- `official_manifest.json`: official version and content fingerprints.

Historical prompts remain in their respective `artifacts/memory-research/` and `docs/memory-research/validation-*/` snapshots; they are not current runtime entry points. Retired `p1.txt` and `p2.txt` files have been removed and need not be restored or archived here.

Each run snapshots the core and MEME prompt directories separately. Loading and manifests support the `evaluation/` subdirectory. The original Jev intent-classification review draft was moved into the run directory; the old multi-round Jev decision flow is no longer used.
