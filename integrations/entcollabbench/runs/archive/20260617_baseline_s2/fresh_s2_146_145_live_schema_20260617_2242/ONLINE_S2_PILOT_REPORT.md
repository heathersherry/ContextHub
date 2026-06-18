# ContextHub × EntCollabBench Online S2 Pilot

This report uses fixed case specs and existing baseline artifacts for a near-online/post-run S2 diagnostic. No benchmark rerun, model call, ContextHub DB write, core guardrail change, or external EntCollabBench source edit was performed.

## Execution Mode

- Benchmark rerun: no
- Diagnostic mode: near-online/post-run S2 diagnostic
- Tool-state contract source: live MCP schema when available, with normalized observed args; schema lookup failures are recorded per call.

## Case Results

### mcp_single_146
- S0 status: passed; passed=True; tokens=715579
- Closure decision: `allow`; missing_actions=[]
- Closure alignment: misaligned_actions=[]; argument_diffs=1; identity_mismatches=0; soft_identity_diffs=0
- Tool-state decisions: {'allow': 13}; repair_or_block_count=0
- False-block readout: no S2 block is expected for a passed case if closure is `allow` and live-schema tool_state has no repair/block.

### mcp_single_145
- S0 status: failed; passed=False; tokens=748809
- Closure decision: `block`; missing_actions=['knowledge_base_specialist.update_knowledge']
- Closure alignment: misaligned_actions=['knowledge_base_specialist.link_case_knowledge']; argument_diffs=3; identity_mismatches=1; soft_identity_diffs=0
- Tool-state decisions: {'allow': 19}; repair_or_block_count=0
- Timeout/KB readout: closure should block when timeout/partial trace leaves `knowledge_base_specialist.update_knowledge` incomplete.
- S0 failure reason: judge_failed_partial_correctness
- Closure open questions: ['missing_required_action: knowledge_base_specialist.update_knowledge', 'argument_mismatch: knowledge_base_specialist.link_case_knowledge knowledge_id 195 != 371']

## Tool-State False Positive Risk

Live schema plus argument normalization avoids the dataset pseudo-schema problem where wrapper aliases such as Teams `content`/`body` create false repairs. Remaining tool_state repairs in this report should be read as live-schema validation findings, not ground-truth argument-diff findings.

## Online Boundary

- Closure: evaluated through `ClosureGuardrail` on adapter-built post-run payloads, not inserted into the agent runtime close path.
- Tool call: evaluated through `ToolStateGuardrail` on observed trace events, not before the external runtime executed each tool.
- Handoff: not intercepted in the external agent loop in this pilot.

## Engineering Next Steps

- Add real runtime hooks around EntCollabBench agent handoff, tool_call, and closure boundaries in a ContextHub-owned wrapper before claiming full online interception.
- Keep live MCP schema extraction and wrapper argument normalization in the online path to reduce false repairs on passing cases.
- Add timeout recovery that emits a closure boundary with unmet required actions, especially missing KB mutations such as `knowledge_base_specialist.update_knowledge`.
