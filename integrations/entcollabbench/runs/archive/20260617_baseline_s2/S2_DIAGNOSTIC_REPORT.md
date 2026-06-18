# ContextHub × EntCollabBench S2 Offline Diagnostic

This report is generated only from existing baseline result/trajectory artifacts. No model benchmark, Docker service, ContextHub DB write, or core guardrail change was used.

## Executive Conclusion

- The strongest S2 signal is closure: the two timeout cases reached a KB delegation/error path without clean completion, so a timeout-aware closure checklist would likely flag real unfinished work.
- Existing handoff guardrails mostly verify packet completeness, ACL, and stale refs; they do not directly detect provider/runtime timeout, so timeout failures should not be claimed as handoff policy wins without adapter support.
- Tool-state can catch role/schema/object/provenance problems, but dataset-derived pseudo schemas create false-repair risk on already-passing S0 cases, especially service wrapper argument aliases.

## S0 Baseline Cases

| Case | S0 status | Failed agents | Tokens | Trace events |
|---|---:|---|---:|---:|
| mcp_single_146 | passed | - | 257073 | 133 |
| mcp_single_143 | timeout | knowledge_base_specialist, collaboration_ops_specialist, it_change_engineer, customer_support_specialist | 400605 | 247 |
| mcp_single_145 | timeout | it_change_engineer, collaboration_ops_specialist, knowledge_base_specialist, customer_support_specialist | 904473 | 144 |
| mcp_single_144 | passed | - | 388410 | 185 |
| mcp_single_142 | passed | - | 343565 | 196 |

## S2 Diagnostic Flags

### mcp_single_146
- S0: passed; tokens=257073; failure=-
- Trace counts by agent: `collaboration_ops_specialist`=36, `knowledge_base_specialist`=57, `customer_support_specialist`=24, `it_change_engineer`=16
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`it_change_engineer`, tool=`create_incident`, event=`tool_call`. Help: unlikely; false-block risk: high. dataset-derived contract required args missing: ['priority']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`knowledge_base_specialist`, tool=`update_knowledge`, event=`tool_call`. Help: unlikely; false-block risk: high. dataset-derived contract required args missing: ['title']
### mcp_single_143
- S0: timeout; tokens=400605; failure=batch#1 subtask#1 request failed: TimeoutError: timed out
- Trace counts by agent: `knowledge_base_specialist`=85, `collaboration_ops_specialist`=53, `it_change_engineer`=34, `customer_support_specialist`=75
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: low. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: low. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`knowledge_base_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: low. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_error`. Help: possible; false-block risk: low. handoff packet missing fields: ['task_intent', 'expected_action', 'required_object_ids']
- `handoff` / `handoff` -> `allow` (none), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_error`. Help: unlikely; false-block risk: low. existing handoff guardrail does not directly classify provider/runtime timeout
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`collaboration_ops_specialist`, tool=`send_channel_message`, event=`tool_call`. Help: unlikely; false-block risk: medium. dataset-derived contract required args missing: ['body']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`it_change_engineer`, tool=`create_incident`, event=`tool_call`. Help: unlikely; false-block risk: medium. dataset-derived contract required args missing: ['impact', 'worknotes']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`knowledge_base_specialist`, tool=`update_knowledge`, event=`tool_call`. Help: unlikely; false-block risk: medium. dataset-derived contract required args missing: ['owner_id', 'product_id', 'state', 'visibility']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`knowledge_base_specialist`, tool=`update_knowledge`, event=`tool_call`. Help: unlikely; false-block risk: medium. dataset-derived contract required args missing: ['knowledge_id', 'owner_id', 'product_id', 'state', 'title', 'visibility']
- `closure` / `closure` -> `block` (unclosed_workflow), agent=`collaboration_ops_specialist`, tool=`-`, event=`final_or_timeout`. Help: likely; false-block risk: low. run timed out before a clean terminal closure
### mcp_single_145
- S0: timeout; tokens=904473; failure=batch#1 subtask#1 request failed: TimeoutError: timed out
- Trace counts by agent: `it_change_engineer`=21, `collaboration_ops_specialist`=49, `knowledge_base_specialist`=50, `customer_support_specialist`=24
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: low. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: low. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_error`. Help: possible; false-block risk: low. handoff packet missing fields: ['task_intent', 'expected_action', 'required_object_ids']
- `handoff` / `handoff` -> `allow` (none), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_error`. Help: unlikely; false-block risk: low. existing handoff guardrail does not directly classify provider/runtime timeout
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`collaboration_ops_specialist`, tool=`send_channel_message`, event=`tool_call`. Help: unlikely; false-block risk: medium. dataset-derived contract required args missing: ['body']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`it_change_engineer`, tool=`create_incident`, event=`tool_call`. Help: unlikely; false-block risk: medium. dataset-derived contract required args missing: ['worknotes']
- `closure` / `closure` -> `block` (unclosed_workflow), agent=`knowledge_base_specialist`, tool=`update_knowledge`, event=`missing_expected_tool_call`. Help: likely; false-block risk: low. required ground-truth action was not observed: knowledge_base_specialist.update_knowledge
- `closure` / `closure` -> `block` (unclosed_workflow), agent=`collaboration_ops_specialist`, tool=`-`, event=`final_or_timeout`. Help: likely; false-block risk: low. missing closure actions: ['knowledge_base_specialist.update_knowledge']
### mcp_single_144
- S0: passed; tokens=388410; failure=-
- Trace counts by agent: `it_change_engineer`=21, `collaboration_ops_specialist`=51, `customer_support_specialist`=24, `knowledge_base_specialist`=89
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`it_change_engineer`, tool=`create_incident`, event=`tool_call`. Help: unlikely; false-block risk: high. dataset-derived contract required args missing: ['category', 'status']
- `closure` / `closure` -> `block` (unclosed_workflow), agent=`knowledge_base_specialist`, tool=`update_knowledge`, event=`missing_expected_tool_call`. Help: possible; false-block risk: high. required ground-truth action was not observed: knowledge_base_specialist.update_knowledge
- `closure` / `closure` -> `block` (unclosed_workflow), agent=`collaboration_ops_specialist`, tool=`-`, event=`final_or_timeout`. Help: possible; false-block risk: high. missing closure actions: ['knowledge_base_specialist.update_knowledge']
### mcp_single_142
- S0: passed; tokens=343565; failure=-
- Trace counts by agent: `customer_support_specialist`=35, `knowledge_base_specialist`=71, `collaboration_ops_specialist`=66, `it_change_engineer`=24
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `handoff` / `handoff` -> `repair` (incomplete_handoff), agent=`collaboration_ops_specialist`, tool=`-`, event=`delegate_done`. Help: unlikely; false-block risk: medium. handoff packet missing fields: ['task_intent', 'expected_action']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`collaboration_ops_specialist`, tool=`send_channel_message`, event=`tool_call`. Help: unlikely; false-block risk: high. dataset-derived contract required args missing: ['body']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`customer_support_specialist`, tool=`update_case`, event=`tool_call`. Help: unlikely; false-block risk: high. dataset-derived contract required args missing: ['assigned_to']
- `tool_call` / `tool_state` -> `repair` (schema_or_enum), agent=`it_change_engineer`, tool=`create_incident`, event=`tool_call`. Help: unlikely; false-block risk: high. dataset-derived contract required args missing: ['impact', 'status']

## Adapter Gaps

- Teams export_state/database-state fallback for deterministic mutation evidence
- runtime MCP tool schema extraction instead of dataset-argument pseudo schemas
- tool wrapper argument normalization, especially Teams body/content aliases
- closure checklist generation from ground truth, canonical diff, and trace results
- handoff packet generation with stable object IDs and context_versions
- timeout recovery boundary that converts delegate_error/partial trace into closure diagnostics

## Claim Boundaries

ContextHub can reasonably claim failures that are expressed as missing closure actions/evidence, contract/schema/role/object/provenance violations, or stale/blocked context dependencies.

ContextHub should not claim raw model planning loops, provider latency, or task timeout by itself. Those become S2-relevant only when the adapter turns the partial trace into a closure or timeout boundary with explicit unmet obligations.

## Recommended Next Step

Prioritize adapter work before online S2: closure checklist generation and tool schema/argument normalization will reduce both missed flags and false repairs. After that, run online S2 first on `mcp_single_145` and `mcp_single_146`: the former exercises timeout/KB failure, while the latter is a short passing case for false-block detection.

## Risks And Uncertainty

- Tool contracts here are inferred from dataset ground truth, not live MCP `inputSchema`; this can overstate schema violations.
- Handoff object IDs are regex-derived from natural-language trace text and need a real object mapping/export-state adapter.
- The timeout cases lack judge-level fine-grained pass/fail labels because judge did not run after timeout.
