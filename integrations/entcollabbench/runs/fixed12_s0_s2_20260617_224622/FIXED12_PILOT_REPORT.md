# Fixed-12 S0 + Near-Online/Post-Run S2 Pilot Report

This report covers a fresh S0 benchmark over only the 12 cases in `FIXED_CASES_MANIFEST_12.json`, followed by a near-online/post-run S2 diagnostic over the saved S0 artifacts. It is not a full online S2 run.

## Artifact Paths

- Fresh S0 artifact dir: `/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs/fixed12_s0_s2_20260617_224622/s0`
- S2 diagnostic output dir: `/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs/fixed12_s0_s2_20260617_224622/s2`
- Compatible S0 analysis summary: `/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs/fixed12_s0_s2_20260617_224622/s0/analysis_summary.json`
- S2 summary: `/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs/fixed12_s0_s2_20260617_224622/s2/online_s2_pilot_summary.json`
- Machine-readable final summary: `/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs/fixed12_s0_s2_20260617_224622/fixed12_final_summary.json`

## Execution Boundary

- Cases were run in manifest order only; no random sample and no expanded case set.
- `batch-concurrency` was 1.
- Timeouts: `TASK_TIMEOUT_SECONDS=1000`, `AGENT_HTTP_TIMEOUT_SECONDS=400`, `JUDGE_TIMEOUT_SECONDS=500`.
- Docker, MCP services, and 11 agent health checks were clean before S0.
- S2 used live MCP schema where available and evaluated saved S0 result/trajectory artifacts post-run.

## Cases Run / Skipped

- Run: 12 cases: `mcp_single_146`, `mcp_single_115`, `mcp_single_72`, `mcp_single_137`, `mcp_single_145`, `mcp_single_143`, `mcp_single_52`, `mcp_single_61`, `mcp_single_64`, `mcp_single_67`, `mcp_single_151`, `mcp_single_87`
- Skipped: 0 cases
- Stop condition triggered: `False`
- Stop reason: `none`

## Aggregate S0

- Passed / failed / timeout / stopped: 7 / 5 / 0 / 0
- Token anomalies: `mcp_single_52`, `mcp_single_64`
- Failure taxonomy: high_tokens=2, judge_or_state_diff_failure=3

## Per-Case S0

| Case | Status | Passed | Timeout | Run tokens | Judge tokens | Duration s | Trace events | Failed agents | Failure reason |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| `mcp_single_146` | passed | True | False | 255764 | 56305 | 188.523 | 179 | none | none |
| `mcp_single_115` | passed | True | False | 367215 | 68917 | 279.682 | 223 | none | none |
| `mcp_single_72` | passed | True | False | 271928 | 70850 | 206.438 | 211 | none | none |
| `mcp_single_137` | passed | True | False | 457238 | 75504 | 178.111 | 221 | none | none |
| `mcp_single_145` | failed | False | False | 486939 | 60281 | 422.094 | 191 | none | judge_failed_or_incomplete |
| `mcp_single_143` | passed | True | False | 414444 | 82134 | 521.369 | 258 | none | none |
| `mcp_single_52` | failed | False | False | 1092227 | 55484 | 212.728 | 156 | none | judge_failed_or_incomplete |
| `mcp_single_61` | passed | True | False | 713822 | 52861 | 176.955 | 152 | none | none |
| `mcp_single_64` | failed | False | False | 1062040 | 45216 | 191.322 | 125 | none | judge_failed_or_incomplete |
| `mcp_single_67` | failed | False | False | 206749 | 42901 | 115.989 | 117 | none | judge_failed_or_incomplete |
| `mcp_single_151` | passed | True | False | 560429 | 87245 | 454.398 | 240 | none | none |
| `mcp_single_87` | failed | False | False | 375787 | 59576 | 138.629 | 168 | none | judge_failed_or_incomplete |

## Per-Case S2 Diagnostic

| Case | Closure | Missing actions | Open Qs | Misaligned actions | Identity / soft diffs | Tool decisions | Repair/block | Schema sources |
|---|---:|---|---:|---|---:|---|---:|---|
| `mcp_single_146` | allow | none | 0 | none | 0 / 0 | allow=15 | 0 | live-mcp-schema=15 |
| `mcp_single_115` | block | it_service_desk_l1.create_knowledge_article | 1 | none | 0 / 0 | allow=24 | 0 | live-mcp-schema=8, schema-unavailable:URLError=16 |
| `mcp_single_72` | allow | none | 0 | none | 0 / 0 | allow=20, repair=8 | 8 | live-mcp-schema=26, schema-unavailable:McpRuntimeAdapterError=2 |
| `mcp_single_137` | allow | none | 0 | none | 0 / 0 | allow=24, repair=7 | 7 | live-mcp-schema=31 |
| `mcp_single_145` | block | knowledge_base_specialist.update_knowledge | 2 | knowledge_base_specialist.link_case_knowledge | 1 / 0 | allow=16 | 0 | live-mcp-schema=16 |
| `mcp_single_143` | block | none | 1 | knowledge_base_specialist.update_knowledge | 1 / 0 | allow=31, repair=1 | 1 | live-mcp-schema=29, schema-unavailable:McpRuntimeAdapterError=3 |
| `mcp_single_52` | allow | none | 0 | none | 0 / 1 | allow=13, repair=4 | 4 | live-mcp-schema=17 |
| `mcp_single_61` | block | none | 1 | collaboration_ops_specialist.send_message | 1 / 0 | allow=13, repair=4 | 4 | live-mcp-schema=17 |
| `mcp_single_64` | block | none | 2 | collaboration_ops_specialist.send_message, knowledge_base_specialist.update_knowledge | 2 / 0 | allow=13, repair=2 | 2 | live-mcp-schema=15 |
| `mcp_single_67` | block | none | 1 | collaboration_ops_specialist.send_message | 1 / 0 | allow=8, repair=1 | 1 | live-mcp-schema=9 |
| `mcp_single_151` | allow | none | 0 | none | 0 / 0 | allow=31 | 0 | live-mcp-schema=10, schema-unavailable:URLError=21 |
| `mcp_single_87` | allow | none | 0 | none | 0 / 0 | allow=18, repair=1 | 1 | live-mcp-schema=19 |

## Claim Boundary

Claimable signals:
- mcp_single_146: pass-control no false block signal
- mcp_single_115: closure block with explicit missing/open obligations
- mcp_single_145: closure block with explicit missing/open obligations
- mcp_single_143: closure block with explicit missing/open obligations
- mcp_single_52: tool_state repair/block signal over failed or imperfect S0 trace
- mcp_single_61: closure block with explicit missing/open obligations
- mcp_single_64: closure block with explicit missing/open obligations
- mcp_single_64: tool_state repair/block signal over failed or imperfect S0 trace
- mcp_single_67: closure block with explicit missing/open obligations
- mcp_single_67: tool_state repair/block signal over failed or imperfect S0 trace
- mcp_single_151: pass-control no false block signal
- mcp_single_87: tool_state repair/block signal over failed or imperfect S0 trace

Non-claimable / caution signals:
- mcp_single_72: S2 repair/block on S0 pass is false-block risk, not an improvement claim
- mcp_single_137: S2 repair/block on S0 pass is false-block risk, not an improvement claim
- mcp_single_143: S2 repair/block on S0 pass is false-block risk, not an improvement claim
- mcp_single_61: S2 repair/block on S0 pass is false-block risk, not an improvement claim
- This diagnostic is near-online/post-run only, not full online interception.
- Do not claim raw token or latency reduction from this S2 diagnostic.
- Do not claim full handoff enforcement; structured handoff packets were not intercepted.

## Remaining Questions

- S2 produced repair signals on several S0 passes; those need false-block review before claiming benefit.
- Several S0 failures were judge/state-diff failures without timeout; closure and tool_state evidence should be inspected against concrete state diffs before assigning root cause.
- Token usage was high in `mcp_single_52` and `mcp_single_64`, but this S2 diagnostic does not measure token reduction.
