from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ToolImplementation(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    CONTRACT_ONLY = "contract_only"


class ToolContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    purpose: str
    input_schema: dict[str, str]
    success_schema: dict[str, str]
    error_codes: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    retryable: bool
    mutates_project: bool
    approval_required: bool
    affected_scope: tuple[str, ...] = Field(min_length=1)
    implementation: ToolImplementation


def _contract(
    name: str,
    purpose: str,
    *,
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
    errors: tuple[str, ...] = ("process_failed", "timeout"),
    timeout: int = 300,
    retryable: bool = False,
    mutates: bool = False,
    approval: bool = False,
    scope: tuple[str, ...] = ("unity/ArenaTemplate",),
    implementation: ToolImplementation = ToolImplementation.CONTRACT_ONLY,
) -> ToolContract:
    return ToolContract(
        name=name,
        purpose=purpose,
        input_schema=inputs or {},
        success_schema=outputs or {"status": "literal[success]"},
        error_codes=errors,
        timeout_seconds=timeout,
        retryable=retryable,
        mutates_project=mutates,
        approval_required=approval,
        affected_scope=scope,
        implementation=implementation,
    )


_CONTRACTS = (
    _contract(
        "health_check",
        "Verify the pinned Unity editor and bridge package compile and respond.",
        outputs={"status": "healthy", "unity_version": "string", "bridge_version": "string"},
        retryable=True,
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "inspect_project",
        (
            "Read bounded public project metadata, the project-wide readable text index, and "
            "the separately controlled writable scope."
        ),
        outputs={"project": "object", "status": "object"},
        retryable=True,
        implementation=ToolImplementation.PARTIAL,
    ),
    _contract(
        "list_project_files",
        (
            "Page through bounded metadata for public project text files without granting "
            "write access. Follow next_cursor until null for complete indexed coverage."
        ),
        inputs={
            "cursor": "optional non-negative integer offset",
            "limit": "optional integer page size from 1 to 512",
            "path_prefix": "optional safe project-relative prefix",
        },
        outputs={
            "files": "array[{path, suffix, bytes}]",
            "next_cursor": "integer|null",
            "coverage_complete": "boolean",
            "truncated": "boolean",
        },
        retryable=True,
        scope=("public project text, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "search_project_text",
        (
            "Search the complete indexed public project text for a literal query without "
            "granting write access, with explicit index-coverage metadata."
        ),
        inputs={"query": "string literal query, 1-256 characters"},
        outputs={
            "matches": "array[{path, line, excerpt}]",
            "coverage_complete": "boolean",
            "truncated": "boolean",
        },
        retryable=True,
        scope=("public project text, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "declare_output_manifest",
        (
            "Request an auditable extension of the write scope when task policy permits it. "
            "Declare every newly required output path before authoring it."
        ),
        inputs={
            "paths": "array[1,64] of safe project-relative ordinary file paths",
            "reason": "string rationale",
        },
        outputs={"added_paths": "string[]", "writable_paths": "string[]"},
        errors=("policy_violation", "path_unsafe", "scope_limit"),
        retryable=False,
        scope=("runtime write-scope declaration only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "review_project_changes",
        (
            "Review a bounded Host-generated diff from the initial public project to the current "
            "revision, including declared outputs, before proposing completion."
        ),
        outputs={
            "changed_paths": "string[]",
            "files": "array[{path, change, diff}]",
            "truncated": "boolean",
        },
        retryable=True,
        scope=("public project text diff, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "inspect_scene",
        "Read one public project scene hierarchy and component inventory without write access.",
        inputs={"scene": "public project scene path"},
        outputs={"roots": "game object[]"},
        retryable=True,
        implementation=ToolImplementation.PARTIAL,
    ),
    _contract(
        "inspect_resource",
        (
            "Read one public engine-native text scene/resource as structured sections "
            "and assertions."
        ),
        inputs={"path": "public .tscn or .tres path"},
        outputs={
            "path": "string",
            "sections": "section[]",
            "assertions": "structural assertion[]",
        },
        retryable=True,
        scope=("public project text, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "inspect_engine_schema",
        (
            "Query the pinned engine runtime for public ClassDB property, method, and signal "
            "metadata. Use this to discover exact API names and types instead of relying on "
            "memory; an optional name filter keeps results bounded."
        ),
        inputs={
            "class_name": "engine ClassDB class name",
            "name_filter": "optional case-insensitive property, method, or signal name substring",
            "include_inherited": "optional boolean; defaults to true",
        },
        outputs={
            "class_name": "string",
            "parent_class": "string",
            "properties": "array[engine property metadata]",
            "methods": "array[engine method metadata]",
            "signals": "array[engine signal metadata]",
            "truncated": "boolean metadata per category",
        },
        retryable=True,
        timeout=60,
        scope=("pinned engine ClassDB, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "read_text_file",
        (
            "Read one bounded page of a UTF-8 script, shader, configuration, or other text "
            "asset from the public project. Follow next_cursor until null when full text is "
            "needed; project coverage scales while each observation remains bounded. Use "
            "inspect_resource for semantic .tscn/.tres inspection. Reading does not grant "
            "write access."
        ),
        inputs={
            "path": "safe public project-relative text path",
            "cursor": "optional integer non-negative UTF-8 byte offset",
            "limit": "optional integer byte page size from 4 to 65536",
        },
        outputs={
            "path": "string",
            "content": "UTF-8 text page",
            "sha256": "string",
            "requested_cursor": "integer",
            "cursor": "integer",
            "cursor_clamped": "boolean",
            "next_cursor": "integer|null",
            "total_bytes": "integer",
            "coverage_complete": "boolean",
        },
        errors=(
            "policy_violation",
            "path_unsafe",
            "file_missing",
            "cursor_invalid",
            "invalid_utf8",
        ),
        retryable=True,
        scope=("public project text, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "read_text_files",
        (
            "Read up to 16 bounded public UTF-8 project files in one observation. Reading does "
            "not grant write access."
        ),
        inputs={"paths": "array[1,16] of safe public project-relative text paths"},
        outputs={"files": "array[{path, content, sha256}]", "total_bytes": "integer"},
        retryable=True,
        scope=("public project text, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "inspect_resources",
        "Inspect up to 16 public .tscn/.tres resources as structured sections in one call.",
        inputs={"paths": "array[1,16] of public .tscn/.tres paths"},
        outputs={"resources": "array[structured resource]"},
        retryable=True,
        scope=("public project text, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "inspect_game_object",
        "Read one named object and its components from the approved scene.",
        inputs={"name": "bounded object name"},
        outputs={"object": "game object"},
        retryable=True,
        implementation=ToolImplementation.PARTIAL,
    ),
    _contract(
        "resolve_scene_node_path",
        (
            "Resolve an exact Godot NodePath as evaluated by the node that owns the NodePath "
            "property. Pass the exact property-owning node, including a planned child path when "
            "the property will live on that child; never substitute its parent. This avoids "
            "guessed parent depth for engine references and signal wiring."
        ),
        inputs={
            "scene": "public .tscn scene path",
            "property_owner_node": (
                "exact scene-rooted node that stores/evaluates the NodePath; planned child allowed"
            ),
            "to_node": "exact scene-rooted target node path; planned child allowed",
        },
        outputs={
            "property_owner_node": "string",
            "node_path": "string",
            "common_ancestor": "string",
        },
        retryable=True,
        scope=("public project scene, read-only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "create_game_object",
        "Create one allowlisted primitive in the approved scene.",
        inputs={"name": "bounded name", "primitive": "allowlisted primitive"},
        outputs={"object_path": "string"},
        mutates=True,
        scope=("unity/ArenaTemplate/Assets/Scenes/Arena.unity",),
    ),
    _contract(
        "set_component_property",
        "Set one allowlisted serialized component property.",
        inputs={"object_path": "string", "component": "string", "property": "typed value"},
        outputs={"previous": "typed value", "current": "typed value"},
        mutates=True,
        scope=("unity/ArenaTemplate/Assets/Scenes/Arena.unity",),
    ),
    _contract(
        "create_script",
        "Create one C# source file under an approved source root.",
        inputs={"path": "safe relative .cs path", "content": "C# source"},
        outputs={"path": "string", "sha256": "string"},
        mutates=True,
        scope=("unity/ArenaTemplate/Assets/Scripts",),
    ),
    _contract(
        "apply_code_patch",
        "Replace one exact text anchor in one approved C# source file.",
        inputs={"path": "safe relative .cs path", "expected": "string", "replacement": "string"},
        outputs={"path": "string", "sha256_before": "string", "sha256_after": "string"},
        errors=("policy_violation", "anchor_missing", "anchor_ambiguous", "write_failed"),
        mutates=True,
        scope=("unity/ArenaTemplate/Assets/Scripts",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "write_text_file",
        "Create or replace one UTF-8 text asset at an explicitly approved project path.",
        inputs={"path": "safe approved relative path", "content": "UTF-8 text"},
        outputs={"path": "string", "sha256_before": "string|null", "sha256_after": "string"},
        errors=("policy_violation", "path_unsafe", "write_failed"),
        mutates=True,
        scope=("explicit approved paths only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "write_text_files",
        "Create or replace up to 16 UTF-8 text assets at explicitly approved project paths.",
        inputs={"files": "array[1,16] of {path: safe approved relative path, content: UTF-8 text}"},
        outputs={"files": "array[{path, sha256_before, sha256_after}]"},
        errors=("policy_violation", "path_unsafe", "duplicate_path", "write_failed"),
        mutates=True,
        scope=("explicit approved paths only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "replace_text",
        "Replace one exact UTF-8 text anchor in one explicitly approved project asset.",
        inputs={
            "path": "safe approved relative path",
            "expected": "unique existing UTF-8 text",
            "replacement": "UTF-8 text",
        },
        outputs={"path": "string", "sha256_before": "string", "sha256_after": "string"},
        errors=("policy_violation", "anchor_missing", "anchor_ambiguous", "write_failed"),
        mutates=True,
        scope=("explicit approved paths only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "mutate_resource_objects",
        (
            "Apply atomic object-level changes to an approved Godot scene/resource while "
            "preserving unrelated sections."
        ),
        inputs={
            "path": "safe approved .tscn or .tres path",
            "operations": (
                "array[1,64] of {kind:set_properties, selector:{section:string, "
                "attributes:object}, properties:primitive object}; {kind:set_attributes, "
                "selector:{section:string, attributes:object}, attributes:primitive object}; "
                "or {kind:append_section, section:string, attributes:primitive object, "
                "properties:primitive object}. String property values are raw Godot "
                "expressions; numeric and boolean primitives are encoded directly."
            ),
        },
        outputs={
            "path": "string",
            "sha256_before": "string",
            "sha256_after": "string",
            "changes": "object mutation[]",
            "assertions": "structural assertion[]",
        },
        errors=(
            "policy_violation",
            "selector_missing",
            "selector_ambiguous",
            "duplicate_object",
            "invalid_resource",
            "write_failed",
        ),
        mutates=True,
        scope=("explicit approved paths only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "mutate_engine_resource_properties",
        (
            "Use the pinned game engine to atomically update allowlisted properties on an "
            "existing engine-native resource that is not safely editable as UTF-8 text."
        ),
        inputs={
            "path": "safe approved existing .material path",
            "operations": (
                "array[1,32] of {property:string, value:JSON scalar or typed Vector2, "
                "Vector2i, Vector3, Vector3i, Vector4, Color, or Rect2 object}"
            ),
        },
        outputs={
            "path": "string",
            "sha256_before": "string",
            "sha256_after": "string",
            "properties": "string[]",
        },
        errors=(
            "policy_violation",
            "unsupported_resource",
            "invalid_variant",
            "engine_save_failed",
        ),
        timeout=120,
        mutates=True,
        scope=("explicit approved existing engine resource only",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "import_project",
        "Run the engine's headless import/parser pass without exposing hidden benchmark tests.",
        outputs={"status": "success", "return_code": "integer", "log_path": "string"},
        timeout=600,
        retryable=True,
        scope=("isolated project workspace",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "review_visual_change",
        (
            "Capture a fixed public camera frame and return object-grounded advisory visual "
            "diagnostics with repair progress."
        ),
        outputs={
            "status": "success|failed|inconclusive",
            "verdict": "pass|fail|uncertain",
            "progress_status": "improved|unchanged|regressed|not_applicable",
            "grounded_objects": "string[]",
            "structured_assertions": "assertion[]",
            "summary": "string",
            "evidence_path": "string",
        },
        timeout=360,
        retryable=False,
        scope=("isolated project workspace", "run evidence directory"),
        implementation=ToolImplementation.PARTIAL,
    ),
    _contract(
        "capture_scene_evidence",
        (
            "Capture the current public scene after importing the same isolated project copy. "
            "Return the frame directly to the primary model together with viewport, transform, "
            "texture/resource provenance, animation-frame, and geometry metadata. The evidence "
            "is explicitly runtime state after scripts and does not certify serialized resource "
            "identity or unshown frames. This capability supplies evidence only and makes no "
            "pass/fail judgment."
        ),
        outputs={
            "status": "success|failed",
            "screenshot_path": "string|null",
            "capture_evidence": "bounded fidelity and coordinate metadata",
            "evidence_contract": "runtime-versus-serialized interpretation metadata",
            "public_visual_asset_paths": "string[]",
        },
        timeout=180,
        retryable=False,
        scope=("isolated project workspace", "run evidence directory"),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "read_console",
        "Read a bounded set of Unity errors and warnings without clearing the Console.",
        inputs={"maximum": "integer[1,200]"},
        outputs={"entries": "console entry[]"},
        retryable=True,
        implementation=ToolImplementation.PARTIAL,
    ),
    _contract(
        "wait_for_compilation",
        "Run Unity batch compilation and return stable diagnostics.",
        outputs={"compiled": "boolean", "log_path": "string"},
        retryable=True,
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "run_edit_mode_tests",
        "Run the approved Edit Mode suite and write NUnit XML.",
        outputs={"result": "pass|fail", "xml_path": "string"},
        timeout=900,
        retryable=True,
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "enter_play_mode",
        "Enter Play Mode for a bounded probe session.",
        outputs={"session_id": "string"},
        mutates=True,
    ),
    _contract(
        "exit_play_mode",
        "Exit the current bounded Play Mode probe session.",
        inputs={"session_id": "string"},
        retryable=True,
        mutates=True,
    ),
    _contract(
        "send_test_command",
        "Send one allowlisted deterministic gameplay command.",
        inputs={"session_id": "string", "command": "allowlisted tagged union"},
        outputs={"accepted": "boolean"},
        mutates=True,
        implementation=ToolImplementation.PARTIAL,
    ),
    _contract(
        "read_game_state",
        "Capture the Runtime State Probe from a real running player.",
        outputs={"snapshot": "RuntimeSnapshot", "json_path": "string"},
        retryable=True,
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "capture_screenshot",
        "Capture the fixed game camera to a PNG artifact.",
        outputs={"png_path": "string", "width": "integer", "height": "integer"},
        retryable=True,
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "run_play_mode_tests",
        "Run deterministic gameplay acceptance tests and write NUnit XML.",
        outputs={"result": "pass|fail", "xml_path": "string"},
        timeout=900,
        retryable=True,
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "build_player",
        "Build the approved macOS standalone application.",
        outputs={"application_path": "string", "bytes": "integer"},
        timeout=1200,
        mutates=True,
        scope=("runs/<run-id>/build",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "launch_build_smoke_test",
        "Launch the built app and capture state, screenshot, and player log.",
        outputs={"state_path": "string", "screenshot_path": "string", "log_path": "string"},
        timeout=120,
        mutates=True,
        scope=("runs/<run-id>", "/private/tmp/gameforge-smoke-*"),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "create_checkpoint",
        "Record the exact Git commit and dirty status before mutation.",
        outputs={"commit": "sha|string[UNBORN]", "dirty": "boolean"},
        mutates=False,
        scope=(".git",),
        implementation=ToolImplementation.VERIFIED,
    ),
    _contract(
        "restore_checkpoint",
        "Restore only the explicitly approved files from a recorded checkpoint.",
        inputs={"checkpoint": "sha", "paths": "approved path[]"},
        outputs={"restored_paths": "string[]"},
        errors=("approval_required", "dirty_scope_conflict", "restore_failed"),
        mutates=True,
        approval=True,
        scope=("explicit approved paths only",),
    ),
)

TOOL_CONTRACTS: dict[str, ToolContract] = {contract.name: contract for contract in _CONTRACTS}
