from gameforge.orchestrator.policy import TOOLS_BY_STATE
from gameforge.plugins.legacy import _descriptor
from gameforge.tooling.contracts import TOOL_CONTRACTS

REQUIRED_TOOLS = {
    "health_check",
    "inspect_project",
    "list_project_files",
    "search_project_text",
    "declare_output_manifest",
    "review_project_changes",
    "inspect_scene",
    "inspect_resource",
    "inspect_engine_schema",
    "resolve_scene_node_path",
    "read_text_file",
    "read_text_files",
    "inspect_resources",
    "inspect_game_object",
    "create_game_object",
    "set_component_property",
    "create_script",
    "apply_code_patch",
    "write_text_file",
    "write_text_files",
    "replace_text",
    "mutate_resource_objects",
    "mutate_engine_resource_properties",
    "import_project",
    "capture_scene_evidence",
    "review_visual_change",
    "read_console",
    "wait_for_compilation",
    "run_edit_mode_tests",
    "enter_play_mode",
    "exit_play_mode",
    "send_test_command",
    "read_game_state",
    "capture_screenshot",
    "run_play_mode_tests",
    "build_player",
    "launch_build_smoke_test",
    "create_checkpoint",
    "restore_checkpoint",
}


def test_every_required_tool_has_a_complete_contract() -> None:
    assert set(TOOL_CONTRACTS) == REQUIRED_TOOLS
    for contract in TOOL_CONTRACTS.values():
        assert contract.error_codes
        assert contract.affected_scope
        assert contract.timeout_seconds > 0


def test_state_policy_references_only_registered_tools() -> None:
    policy_tools = set().union(*TOOLS_BY_STATE.values())

    assert policy_tools <= set(TOOL_CONTRACTS)


def test_legacy_descriptor_preserves_resource_operation_schema() -> None:
    descriptor = _descriptor(TOOL_CONTRACTS["mutate_resource_objects"])
    operations = descriptor.input_schema["properties"]["operations"]

    assert "set_properties" in operations["description"]
    assert operations["minItems"] == 1
    assert operations["maxItems"] == 64
    variants = operations["items"]["oneOf"]
    assert {variant["properties"]["kind"]["const"] for variant in variants} == {
        "set_properties",
        "set_attributes",
        "append_section",
    }
    set_attributes = next(
        variant
        for variant in variants
        if variant["properties"]["kind"]["const"] == "set_attributes"
    )
    assert set_attributes["properties"]["attributes"]["minProperties"] == 1
    append = next(
        variant
        for variant in variants
        if variant["properties"]["kind"]["const"] == "append_section"
    )
    assert "minProperties" not in append["properties"]["properties"]


def test_legacy_descriptor_keeps_human_parameter_descriptions() -> None:
    descriptor = _descriptor(TOOL_CONTRACTS["inspect_resource"])

    assert descriptor.input_schema["properties"]["path"]["description"] == (
        "public .tscn or .tres path"
    )


def test_output_manifest_descriptor_requires_bounded_string_paths() -> None:
    descriptor = _descriptor(TOOL_CONTRACTS["declare_output_manifest"])
    paths = descriptor.input_schema["properties"]["paths"]

    assert paths["type"] == "array"
    assert paths["minItems"] == 1
    assert paths["maxItems"] == 64
    assert paths["items"] == {"type": "string"}


def test_batch_read_descriptors_require_bounded_string_paths() -> None:
    for name in ("read_text_files", "inspect_resources"):
        descriptor = _descriptor(TOOL_CONTRACTS[name])
        paths = descriptor.input_schema["properties"]["paths"]

        assert paths["type"] == "array"
        assert paths["minItems"] == 1
        assert paths["maxItems"] == 16
        assert paths["items"] == {"type": "string"}


def test_list_project_files_descriptor_has_typed_optional_paging_inputs() -> None:
    descriptor = _descriptor(TOOL_CONTRACTS["list_project_files"])

    assert descriptor.input_schema["required"] == []
    assert descriptor.input_schema["properties"]["cursor"]["type"] == "integer"
    assert descriptor.input_schema["properties"]["limit"]["type"] == "integer"
    assert descriptor.input_schema["properties"]["path_prefix"]["type"] == "string"


def test_read_text_file_descriptor_has_typed_optional_paging_inputs() -> None:
    descriptor = _descriptor(TOOL_CONTRACTS["read_text_file"])

    assert descriptor.input_schema["required"] == ["path"]
    assert descriptor.input_schema["properties"]["cursor"]["type"] == "integer"
    assert descriptor.input_schema["properties"]["limit"]["type"] == "integer"


def test_engine_resource_property_mutation_has_bounded_typed_operations() -> None:
    descriptor = _descriptor(TOOL_CONTRACTS["mutate_engine_resource_properties"])
    operations = descriptor.input_schema["properties"]["operations"]

    assert operations["minItems"] == 1
    assert operations["maxItems"] == 32
    assert operations["items"]["required"] == ["property", "value"]
