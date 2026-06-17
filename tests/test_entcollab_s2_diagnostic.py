from integrations.entcollabbench.s2_diagnostic import (
    actual_tool_call,
    diagnose_closure,
    diagnose_tool_calls,
    extract_object_ids,
)


def test_extract_object_ids_from_handoff_text() -> None:
    ids = extract_object_ids(
        "Handle CS-0000042 / case ID 42, article 100, USER_045, GROUP_002, "
        "team_techcorp_001 and channel_shared_001."
    )

    assert ids == [
        "case/42",
        "knowledge/100",
        "USER_045",
        "GROUP_002",
        "team_techcorp_001",
        "channel_shared_001",
    ]


def test_actual_tool_call_parses_wrapped_arguments_json() -> None:
    event = {
        "agent_name": "collaboration_ops_specialist",
        "event": "tool_call",
        "ts": 1.0,
        "data": {
            "tool_name": "mcp_teams_call_tool",
            "arguments": {
                "tool_name": "send_channel_message",
                "arguments_json": '{"teamId": "team_techcorp_001", "importance": "high"}',
            },
        },
    }

    call = actual_tool_call(event)

    assert call is not None
    assert call["server"] == "teams"
    assert call["tool_name"] == "send_channel_message"
    assert call["tool_args"]["importance"] == "high"


def test_tool_diagnostic_flags_dataset_schema_alias_miss() -> None:
    gt = [
        {
            "mcp_server_name": "teams",
            "tool_name": "send_channel_message",
            "agent": "collaboration_ops_specialist",
            "arguments": {"teamId": "team_techcorp_001", "body": {"content": "hello"}},
        }
    ]
    events = [
        {
            "agent_name": "collaboration_ops_specialist",
            "event": "tool_call",
            "ts": 1.0,
            "data": {
                "tool_name": "mcp_teams_call_tool",
                "arguments": {
                    "tool_name": "send_channel_message",
                    "arguments_json": '{"teamId": "team_techcorp_001", "content": "hello"}',
                },
            },
        }
    ]

    flags, records = diagnose_tool_calls("case", gt, events, "passed")

    assert records[0]["missing_required_args"] == ["body"]
    assert flags[0].guardrail == "tool_state"
    assert flags[0].verdict == "repair"
    assert flags[0].false_block_risk == "high"


def test_closure_diagnostic_blocks_missing_required_action() -> None:
    gt = [
        {
            "mcp_server_name": "csm",
            "tool_name": "update_case",
            "agent": "customer_support_specialist",
            "arguments": {"case_id": 42},
        }
    ]

    flags, closure = diagnose_closure("case", gt, [], "timeout", [])

    assert closure["missing_actions"] == ["customer_support_specialist.update_case"]
    assert flags[0].guardrail == "closure"
    assert flags[0].helps_real_failure == "likely"
