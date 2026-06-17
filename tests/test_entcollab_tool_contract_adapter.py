from integrations.entcollabbench.tool_contract_adapter import (
    normalize_tool_args,
    tool_schema_to_contract_fields,
)


def test_normalize_teams_args_adds_wrapper_aliases() -> None:
    args = normalize_tool_args(
        "teams",
        "send_channel_message",
        {
            "team_id": "team_techcorp_001",
            "channel_id": "channel_shared_001",
            "content": "hello",
            "content_type": "text",
        },
    )

    assert args["teamId"] == "team_techcorp_001"
    assert args["channelId"] == "channel_shared_001"
    assert args["contentType"] == "text"
    assert args["body"] == {"content": "hello", "contentType": "text"}


def test_normalize_teams_args_preserves_body_and_adds_flat_content() -> None:
    args = normalize_tool_args(
        "teams",
        "send_channel_message",
        {
            "teamId": "team_techcorp_001",
            "channelId": "channel_shared_001",
            "body": {"content": "hello", "contentType": "html"},
        },
    )

    assert args["team_id"] == "team_techcorp_001"
    assert args["channel_id"] == "channel_shared_001"
    assert args["content"] == "hello"
    assert args["content_type"] == "html"


def test_live_input_schema_to_tool_call_contract_fields_uses_base_context_uri() -> None:
    fields = tool_schema_to_contract_fields(
        "teams",
        {
            "name": "send_channel_message",
            "description": "Send a Teams channel message",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "teamId": {"type": "string"},
                    "channelId": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["teamId", "channelId", "content"],
            },
        },
        required_role="collaboration_ops_specialist",
        mutation_intent="create",
    )

    assert fields["tool_name"] == "send_channel_message"
    assert fields["required_role"] == "collaboration_ops_specialist"
    assert fields["arg_schema"]["required"] == ["teamId", "channelId", "content"]
    assert fields["mutation_intent"] == "create"
    assert fields["depends_on_uris"] == ["ctx://entcollab/tool_schema/teams"]
    assert all("@v" not in uri for uri in fields["depends_on_uris"])
