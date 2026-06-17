from integrations.entcollabbench.mapping import (
    object_uri,
    policy_uri,
    resolve_version_tag,
    role_to_owner_space,
    role_uri,
    to_tool_contract_fields,
    tool_schema_uri,
)


def test_entcollab_uri_builders_are_deterministic() -> None:
    assert role_uri("hr_service_specialist") == "ctx://entcollab/role/hr_service_specialist"
    assert tool_schema_uri("itsm") == "ctx://entcollab/tool_schema/itsm"
    assert policy_uri("LEGAL-PRIV-0001") == "ctx://entcollab/policy/LEGAL-PRIV-0001"
    assert object_uri("incident/INC001") == "ctx://entcollab/object/incident/INC001"


def test_tool_schema_base_uri_and_runtime_ref_are_distinct() -> None:
    assert "@v" not in tool_schema_uri("itsm")
    assert tool_schema_uri("itsm", 3) == "ctx://entcollab/tool_schema/itsm@v3"
    assert resolve_version_tag("tool_schema:itsm@v3") == tool_schema_uri("itsm", 3)


def test_resolve_version_tag_supports_policy_and_object_tags() -> None:
    assert resolve_version_tag("policy:PROC-AC-0002@v2") == (
        "ctx://entcollab/policy/PROC-AC-0002@v2"
    )
    assert resolve_version_tag("object:hr_case/57") == "ctx://entcollab/object/hr_case/57"


def test_role_to_owner_space_matches_department_mapping() -> None:
    assert role_to_owner_space("developer_engineer") == "engineering"
    assert role_to_owner_space("finance_approval_specialist") == "approval_center"


def test_to_tool_contract_fields_from_ground_truth_step() -> None:
    fields = to_tool_contract_fields(
        {
            "mcp_server_name": "hr",
            "tool_name": "update_hr_case",
            "agent": "hr_service_specialist",
            "arguments": {"hr_case_id": "57", "status": "work_in_progress"},
        }
    )

    assert fields["tool_name"] == "update_hr_case"
    assert fields["required_role"] == "hr_service_specialist"
    assert fields["arg_schema"]["required"] == ["hr_case_id", "status"]
    assert fields["depends_on_uris"] == ["ctx://entcollab/tool_schema/hr"]
