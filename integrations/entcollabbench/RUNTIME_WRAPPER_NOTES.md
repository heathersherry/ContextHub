# EntCollabBench Runtime Wrapper Notes

`runtime_wrapper.py` is a ContextHub-owned harness layer. It does not monkeypatch
or modify the external EntCollabBench source tree, and it does not change the
core `src/contexthub/enforcement/` guardrails.

## Current Runtime Boundary

- Tool calls can be gated before dispatch when a harness has `agent_id`,
  `server`, `tool_name`, raw wrapper args, and either a live schema record or a
  schema provider. The wrapper normalizes EntCollabBench/MCP args, builds a
  `ToolCallContract`, calls `EnforcementInterceptor.on_tool_call()`, and returns
  both the S2 decision and the action the caller should apply.
- Closure can be gated after a clean result, timeout, or partial trace boundary
  when a harness has trace-like events plus ground truth. The wrapper builds the
  `ClosureChecklist` through `closure_adapter`, calls
  `EnforcementInterceptor.on_closure()`, and returns missing actions/open
  questions for caller feedback.
- Handoff currently has only a thin method for callers that already expose a
  structured packet. It is not yet wired to EntCollabBench's internal delegate
  path, so it should not be described as full online handoff interception.

## Relation To `online_s2_pilot.py`

`online_s2_pilot.py` remains a near-online/post-run diagnostic over fixed specs
and saved trajectories. `runtime_wrapper.py` moves the tool-call check to a
pre-execution API that a ContextHub harness can call before allowing the actual
tool/agent action to run. Closure is still result/timeout-boundary driven unless
the external runtime exposes a clean terminal event in-process.

## How To Use The Wrapper

Use `ContextHubRuntimeWrapper` from a ContextHub-owned harness that sits around
EntCollabBench execution. The wrapper is intentionally dependency-injected so
tests can pass fake services, while pilot runs can pass real `PgRepository`,
`LoadedWorld`, and schema providers.

### Initialize

```python
from integrations.entcollabbench.runtime_wrapper import ContextHubRuntimeWrapper
from integrations.entcollabbench.world_loader import LoadedWorld

wrapper = ContextHubRuntimeWrapper(
    repo=repo,
    account_id="acme",
    loaded=loaded_world,  # from WorldLoader.load(...)
    audit=audit_service,
)
```

For pure tests, inject a fake `service` or fake `schema_provider` rather than
starting Docker, Postgres, or model services.

### Gate Tool Calls Before Dispatch

Call this immediately before the harness invokes the underlying MCP tool. The
inputs should be the same values the runtime is about to dispatch:

```python
result = await wrapper.enforce_tool_call_before_execute(
    agent_id="collaboration_ops_specialist",
    server="teams",
    tool_name="send_channel_message",
    raw_args={
        "team_id": "team_techcorp_001",
        "channel_id": "channel_shared_001",
        "body": {"contentType": "text", "content": "ContextHub smoke test."},
    },
    schema_record=live_schema_record,
)

if result.action.allow:
    # Safe to execute the real MCP call.
    dispatch_mcp_tool(result.normalized_args)
elif result.action.retry:
    # Apply deterministic patch or feed violations back for one retry.
    handle_repair(result.action)
else:
    # Block or mark pending/escalated.
    stop_or_escalate(result)
```

The wrapper normalizes common EntCollabBench wrapper aliases, such as
`team_id/teamId`, `channel_id/channelId`, and Teams `body/content`, before
building the `ToolCallContract`. This is important: using dataset-derived
pseudo schemas without normalization caused false repairs on passing S0 traces.

### Gate Closure After Result, Error, Or Timeout

Use closure gating when the harness has a clean result, timeout, or partial
trace boundary. The wrapper turns ground-truth obligations and trace-like events
into a `ClosureChecklist`:

```python
result = await wrapper.enforce_closure_after_result_or_timeout(
    agent_id="collaboration_ops_specialist",
    workflow_id="mcp_single_145",
    ground_truth=ground_truth_steps,
    trace_events=observed_events,
    runtime_summary={
        "timeout": True,
        "failure_reason": "TimeoutError: timed out",
        "failed_agents": ["knowledge_base_specialist"],
    },
)

if result.decision.verdict.value == "block":
    report_unclosed_workflow(result.missing_actions, result.open_questions)
```

This is the path that currently detects missing workflow obligations such as
`knowledge_base_specialist.update_knowledge` after a timeout or partial trace.

### Handoff

`enforce_handoff_before_delegate(...)` is available only when the caller already
has a structured handoff packet. The current EntCollabBench HTTP delegation path
does not expose a complete packet at the ContextHub harness layer, so handoff
should remain marked as partial until a wrapper around delegate dispatch can
construct stable `required_object_ids`, `task_intent`, `expected_action`, and
`context_versions`.

### Verdict Handling

The wrapper returns both the raw `GuardrailDecision` and an `EnforcementAction`:

- `allow`: execute the original action.
- `retry_with_patch`: apply deterministic repair and retry once.
- `retry_with_feedback`: feed violations back to the agent for one retry.
- `block`: stop the action.
- `pending`: record escalation / human review.

For benchmark accounting, keep guardrail decisions separate from EntCollabBench
judge results. S2 can claim improvements for missing closure evidence,
schema/role/object/provenance violations, and stale dependencies; it should not
claim raw provider latency or model planning loops unless the wrapper converts a
partial trace into an explicit unmet workflow obligation.

## Next Integration Step

The next step is to wrap EntCollabBench runtime dispatch without editing the
external repository:

- Capture session events around `AgentRuntime._append_session_event` so tool
  calls/results and delegate events are mirrored into ContextHub-owned trace
  structures.
- Call `enforce_tool_call_before_execute(...)` immediately before MCP tool
  dispatch. Only execute the underlying action when `action.allow` is true.
- On timeout, error, or terminal result, call
  `enforce_closure_after_result_or_timeout(...)` with the accumulated trace and
  ground truth.
- If structured delegate/handoff packets become available at the harness layer,
  pass them through `enforce_handoff_before_delegate(...)`; otherwise keep
  handoff classified as not yet fully online.
