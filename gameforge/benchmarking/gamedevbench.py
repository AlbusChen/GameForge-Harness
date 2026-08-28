from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

try:
    import fcntl
except ImportError:  # pragma: no cover - GameDevBench currently targets macOS/POSIX.
    fcntl = None  # type: ignore[assignment]

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from gameforge.adapters.godot_resource import (
    GodotResourceDocument,
    ResourceMutationRequest,
    resource_snapshot,
)
from gameforge.adapters.llm import AgentCliLanguageModel, ModelError
from gameforge.harness.contracts import (
    EngineName,
    EvaluationSpec,
    GateOutcome,
    GateStatus,
    HarnessProfile,
    ModelRunSpec,
    NormalizedTask,
    RunBudgets,
    RunStatus,
    TaskSourceKind,
)
from gameforge.harness.errors import InfrastructureFailure
from gameforge.harness.execution_profiles import ExecutionProfile, codex_sandbox_for
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    AssetPolicy,
    EngineEnvironment,
    EvidencePolicy,
    GameTaskSpec,
    RequirementEnforcement,
    RuntimeProtocol,
)
from gameforge.harness.minimal_workspace import (
    MinimalWorkspaceHarnessRunner,
    NativeWorkspaceModel,
)
from gameforge.harness.model_profiles import ModelProfile, ModelProvider
from gameforge.harness.preservation import (
    IGNORED_DIRECTORY_NAMES,
    IGNORED_FILE_SUFFIXES,
    snapshot_project,
)
from gameforge.harness.project_context import DEFAULT_ASSET_SUFFIXES, build_project_context
from gameforge.harness.project_scope import ProjectAccessScope, ProjectScopeError
from gameforge.harness.run_factory import create_run_spec, create_run_spec_v2
from gameforge.harness.runner import HarnessRun, HarnessRunner
from gameforge.harness.shell_environment import login_shell_tool_environment
from gameforge.orchestrator.policy import PolicyViolation

GAMEDEVBENCH_COMMIT = "e3868bccbb88e86a3eb2d62154f1e9e0f3fd489a"
GAMEDEVBENCH_VERSION = f"git-{GAMEDEVBENCH_COMMIT[:12]}"
GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS = 900
_TEXT_SUFFIXES = frozenset(
    {".cfg", ".gd", ".gdshader", ".glsl", ".godot", ".json", ".shader", ".tscn", ".tres"}
)
_PUBLIC_TEXT_SUFFIXES = _TEXT_SUFFIXES | frozenset(
    {".comp", ".gdshaderinc", ".inc", ".rast", ".txt"}
)
_ENGINE_RESOURCE_SUFFIXES = frozenset({".material"})
_CONTEXT_MAXIMUM_FILES = 32
_CONTEXT_MAXIMUM_FILE_BYTES = 64 * 1024
_CONTEXT_MAXIMUM_TOTAL_BYTES = 256 * 1024
_BATCH_TEXT_MAXIMUM_FILES = 16
_BATCH_TEXT_MAXIMUM_BYTES = 512 * 1024
_PROJECT_DIFF_MAXIMUM_BYTES = 256 * 1024
_PROJECT_INDEX_MAXIMUM_FILES = 32_768
_PROJECT_LIST_MAXIMUM_FILES = 512
_PROJECT_SEARCH_MAXIMUM_MATCHES = 64
_PROJECT_SEARCH_MAXIMUM_QUERY_CHARACTERS = 256
_BASELINE_REPLAY_MAXIMUM_FILE_BYTES = 16 * 1024 * 1024
_BASELINE_REPLAY_MAXIMUM_TOTAL_BYTES = 64 * 1024 * 1024
_PUBLIC_VALIDATION_TIMEOUT_SECONDS = 120
_PUBLIC_STARTUP_TIMEOUT_SECONDS = 45
_GODOT_PROCESS_ATTEMPTS = 3
_GODOT_GRACEFUL_TERMINATION_SECONDS = 3
_GODOT_PROCESS_QUEUE_TIMEOUT_SECONDS = 1_800
_GODOT_PROCESS_INVOCATIONS = itertools.count(1)
_GODOT_PROCESS_LOCK = Path(tempfile.gettempdir()) / "gameforge-godot-process-v1.lock"
_VISUAL_SUFFIXES = frozenset({".jpeg", ".jpg", ".png", ".webp"})
_VISUAL_MAXIMUM_FILES = 4
_VISUAL_MAXIMUM_FILE_BYTES = 8 * 1024 * 1024
_MODEL_VISUAL_MAXIMUM_FILES = 6
_ATLAS_GRID_CONTEXT_VERSION = "atlas-grid-v1"
_VISUAL_CATALOG_CONTEXT_VERSION = "visual-catalog-v1"
_VISUAL_CATALOG_MAXIMUM_FAMILIES = 6
_VISUAL_CATALOG_MAXIMUM_FILES = 48
_VISUAL_CATALOG_SHEET_CAPACITY = 24
_VISUAL_ASSET_MANIFEST_MAXIMUM_FILES = 512
_PUBLIC_ERROR_PATTERNS = (
    "ERROR:",
    "SCRIPT ERROR:",
    "Parse Error:",
    "Failed loading resource:",
    "Error loading resource:",
    "Resource file not found:",
    "Cannot open file",
    "Cannot load source code",
    "GAMEFORGE_RESOURCE_LOAD_FAILED:",
    "GAMEFORGE_RESOURCE_INSTANTIATE_FAILED:",
    "Invalid scene:",
)
_EXT_RESOURCE_VALUE = re.compile(r'^ExtResource\(\s*"(?P<id>[^"\r\n]+)"\s*\)$')
_GDSCRIPT_EXPORTED_TYPE = re.compile(
    r"(?m)^[ \t]*@export(?:_[A-Za-z0-9_]+(?:\([^\n)]*\))?)?[^\n]*?"
    r"\bvar\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
    r"(?P<type>[A-Za-z_][A-Za-z0-9_.]*)"
)
_RESOURCE_VALIDATION_SCRIPT = """extends SceneTree

func _initialize():
    var failed := false
    for resource_path in OS.get_cmdline_user_args():
        var resource = ResourceLoader.load(resource_path)
        if resource == null:
            push_error("GAMEFORGE_RESOURCE_LOAD_FAILED: %s" % resource_path)
            failed = true
        else:
            print("GAMEFORGE_RESOURCE_LOADED: %s" % resource_path)
            if resource is PackedScene:
                var instance = resource.instantiate()
                if instance == null:
                    push_error("GAMEFORGE_RESOURCE_INSTANTIATE_FAILED: %s" % resource_path)
                    failed = true
                else:
                    instance.free()
    quit(1 if failed else 0)
"""
_ENGINE_RESOURCE_MUTATION_SCRIPT = """extends SceneTree

func _initialize():
    var arguments = OS.get_cmdline_user_args()
    if arguments.size() != 1:
        _fail("expected one mutation request path")
        return
    var request_text = FileAccess.get_file_as_string(arguments[0])
    var request = JSON.parse_string(request_text)
    if typeof(request) != TYPE_DICTIONARY:
        _fail("mutation request is not an object")
        return
    var resource_path = "res://" + str(request["path"])
    var resource = ResourceLoader.load(
        resource_path,
        "",
        ResourceLoader.CACHE_MODE_IGNORE
    )
    if resource == null:
        _fail("resource could not be loaded: " + resource_path)
        return
    for operation in request["operations"]:
        resource.set(str(operation["property"]), _decode_variant(operation["value"]))
    var save_error = ResourceSaver.save(resource, resource_path)
    if save_error != OK:
        _fail("ResourceSaver failed with code %s" % save_error)
        return
    print("GAMEFORGE_ENGINE_RESOURCE_MUTATION_OK")
    quit(0)

func _decode_variant(value):
    if typeof(value) != TYPE_DICTIONARY or not value.has("type"):
        return value
    match str(value["type"]):
        "Vector2":
            return Vector2(float(value["x"]), float(value["y"]))
        "Vector2i":
            return Vector2i(int(value["x"]), int(value["y"]))
        "Vector3":
            return Vector3(float(value["x"]), float(value["y"]), float(value["z"]))
        "Vector3i":
            return Vector3i(int(value["x"]), int(value["y"]), int(value["z"]))
        "Vector4":
            return Vector4(
                float(value["x"]),
                float(value["y"]),
                float(value["z"]),
                float(value["w"])
            )
        "Color":
            return Color(
                float(value["r"]),
                float(value["g"]),
                float(value["b"]),
                float(value["a"])
            )
        "Rect2":
            return Rect2(
                float(value["x"]),
                float(value["y"]),
                float(value["width"]),
                float(value["height"])
            )
    return value

func _fail(message: String):
    push_error("GAMEFORGE_ENGINE_RESOURCE_MUTATION_FAILED: " + message)
    quit(2)
"""
_OFFICIAL_EXPLICIT_FAILURE = "VALIDATION_FAILED"
_OFFICIAL_EXPLICIT_SUCCESS = "VALIDATION_PASSED"
_OFFICIAL_FALLBACK_FAILURE_PATTERNS = ("SCRIPT ERROR:",)
_GODOT_CRASH_PATTERNS = (
    "handle_crash: Program crashed",
    "Abort trap:",
    "SIGABRT",
    "SIGSEGV",
    "Segmentation fault",
)
_GODOT_ENVIRONMENT_FAILURE_PATTERNS = (
    "Error attempting to create data dir:",
    "Error saving editor settings to ",
)
_VISUAL_REASONING_TERMS = (
    "visually",
    "image",
    "render",
    "looks right",
    "screen",
    "sprite",
    "particle",
    "tile set",
    "tileset",
    "tight circle",
)


class VisualReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["pass", "fail", "uncertain"]
    progress: Literal["improved", "unchanged", "regressed", "not_applicable"]
    summary: str = Field(min_length=1, max_length=2000)
    grounded_objects: tuple[str, ...] = Field(max_length=32)
    structured_evidence_acknowledged: bool
    measurement_basis: Literal["none", "qualitative", "quantitative"] = "none"
    uncertainties: tuple[str, ...] = Field(default=(), max_length=16)


class PublicAssetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str
    bytes: int = Field(ge=0)
    referenced_by: tuple[str, ...] = ()
    relevance: tuple[str, ...] = ()
    attached: bool = False


@dataclass(frozen=True)
class GameDevBenchTaskRoute:
    name: Literal["simple", "interactive", "visual"]
    visual_paths: tuple[Path, ...] = ()
    visual_relative_paths: tuple[str, ...] = ()
    asset_manifest: tuple[PublicAssetRecord, ...] = ()
    catalog_paths: tuple[Path, ...] = ()
    catalog_relative_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelVisualContext:
    paths: tuple[Path, ...]
    records: tuple[dict[str, object], ...] = ()


class GodotEngineCrash(InfrastructureFailure):
    """A retryable engine-process failure, distinct from project validation failure."""


class GodotEnvironmentError(InfrastructureFailure):
    """A host filesystem/runtime failure that must not be scored as a model failure."""


@dataclass(frozen=True)
class GodotRuntimeSandbox:
    executable: Path
    source_executable: Path
    root: Path
    mode: str


@dataclass(frozen=True)
class GodotProjectSandbox:
    project: Path


@dataclass(frozen=True)
class GameDevBenchSource:
    benchmark_root: Path
    task_id: str
    editable_paths: tuple[str, ...]

    def validate(self) -> None:
        root = self.benchmark_root.resolve(strict=True)
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit = completed.stdout.strip()
        if completed.returncode != 0 or commit != GAMEDEVBENCH_COMMIT:
            raise ValueError(
                "GameDevBench checkout must be pinned to "
                f"{GAMEDEVBENCH_COMMIT}; found {commit or 'unknown'}"
            )
        for path in (self.task_archive, self.ground_truth_archive):
            if not path.is_file():
                raise ValueError(f"GameDevBench archive is missing: {path}")

    @property
    def task_archive(self) -> Path:
        return self.benchmark_root.resolve() / "tasks" / f"{self.task_id}.zip"

    @property
    def ground_truth_archive(self) -> Path:
        return self.benchmark_root.resolve() / "tasks_gt" / f"{self.task_id}.zip"

    def task_config(self) -> dict[str, object]:
        self.validate()
        member = f"tasks/{self.task_id}/task_config.json"
        try:
            with zipfile.ZipFile(self.task_archive) as archive:
                payload = json.loads(archive.read(member).decode("utf-8"))
        except (
            KeyError,
            OSError,
            UnicodeDecodeError,
            zipfile.BadZipFile,
            json.JSONDecodeError,
        ) as error:
            raise ValueError(f"cannot read official task config for {self.task_id}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("instruction"), str):
            raise ValueError(f"official task config is invalid: {self.task_id}")
        return payload

    def public_file_bytes(self, relative_path: str) -> bytes | None:
        """Read one safe public project file from the pinned task archive."""

        relative = PurePosixPath(relative_path)
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or _is_hidden_benchmark_file(relative)
        ):
            return None
        member = (PurePosixPath("tasks") / self.task_id / relative).as_posix()
        try:
            with zipfile.ZipFile(self.task_archive) as archive:
                return archive.read(member)
        except KeyError:
            return None

    def normalized_task(self, project_path: Path) -> NormalizedTask:
        config = self.task_config()
        public_payload = {
            "task_id": self.task_id,
            "instruction": config["instruction"],
            "editable_paths": self.editable_paths,
            "benchmark_commit": GAMEDEVBENCH_COMMIT,
        }
        return NormalizedTask(
            id=f"gamedevbench-{self.task_id}",
            source=TaskSourceKind.BENCHMARK,
            instruction=str(config["instruction"]),
            project_path=project_path.resolve(),
            acceptance=("pass the pinned official evaluator",),
            editable_paths=self.editable_paths,
            benchmark_name="GameDevBench",
            benchmark_version=GAMEDEVBENCH_VERSION,
            source_sha256=NormalizedTask.source_hash(public_payload),
            metadata={
                "official_task_id": self.task_id,
                "official_commit": GAMEDEVBENCH_COMMIT,
            },
        )

    def suggested_editable_paths(
        self, *, allow_deferred_declaration: bool = False
    ) -> tuple[str, ...]:
        """Derive a bounded mutation scope from public task files and instructions only."""
        config = self.task_config()
        instruction = str(config["instruction"])
        explicit = self._resolve_public_instruction_paths(
            _public_instruction_paths(instruction),
            defer_ambiguous_new_files=allow_deferred_declaration,
        )
        existing = self._public_text_members()
        existing_sizes = dict(existing)
        broad = tuple(dict.fromkeys((*existing_sizes, *explicit)))
        if _context_fits(broad, existing_sizes):
            return broad
        if not explicit:
            if allow_deferred_declaration:
                return ()
            raise ValueError(
                f"public task context is too large and names no bounded text paths: {self.task_id}"
            )
        if not _context_fits(explicit, existing_sizes):
            raise ValueError(f"publicly named task context exceeds safety limits: {self.task_id}")
        return explicit

    def _resolve_public_instruction_paths(
        self,
        paths: tuple[str, ...],
        *,
        defer_ambiguous_new_files: bool = False,
    ) -> tuple[str, ...]:
        members = self._public_mutable_members()
        resolved: list[str] = []
        for path in paths:
            if path in members:
                resolved.append(path)
                continue
            candidates = tuple(member for member in members if PurePosixPath(member).name == path)
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif defer_ambiguous_new_files and len(PurePosixPath(path).parts) == 1:
                continue
            else:
                resolved.append(path)
        return tuple(dict.fromkeys(resolved))

    def _public_mutable_members(self) -> tuple[str, ...]:
        prefix = PurePosixPath("tasks") / self.task_id
        members: list[str] = []
        with zipfile.ZipFile(self.task_archive) as archive:
            for member in archive.infolist():
                relative = _member_relative(member.filename, prefix)
                if (
                    relative is None
                    or member.is_dir()
                    or _is_hidden_benchmark_file(relative)
                    or relative.suffix.lower()
                    not in (_TEXT_SUFFIXES | _ENGINE_RESOURCE_SUFFIXES | DEFAULT_ASSET_SUFFIXES)
                ):
                    continue
                members.append(relative.as_posix())
        return tuple(sorted(members))

    def _public_text_members(self) -> tuple[tuple[str, int], ...]:
        prefix = PurePosixPath("tasks") / self.task_id
        members: list[tuple[str, int]] = []
        with zipfile.ZipFile(self.task_archive) as archive:
            for member in archive.infolist():
                relative = _member_relative(member.filename, prefix)
                if (
                    relative is None
                    or member.is_dir()
                    or _is_hidden_benchmark_file(relative)
                    or relative.suffix.lower() not in _TEXT_SUFFIXES
                ):
                    continue
                members.append((relative.as_posix(), member.file_size))
        return tuple(sorted(members))

    def prepare_workspace(self, destination: Path) -> Path:
        self.validate()
        if destination.exists():
            raise ValueError(f"GameDevBench workspace already exists: {destination}")
        destination.mkdir(parents=True)
        prefix = PurePosixPath("tasks") / self.task_id
        with zipfile.ZipFile(self.task_archive) as archive:
            for member in archive.infolist():
                relative = _member_relative(member.filename, prefix)
                if relative is None or member.is_dir() or _is_hidden_benchmark_file(relative):
                    continue
                target = destination / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
        project_file = destination / "project.godot"
        if not project_file.is_file():
            raise ValueError(f"official task does not contain project.godot: {self.task_id}")
        return destination

    def add_hidden_tests(self, validation_directory: Path) -> None:
        prefix = PurePosixPath("tasks") / self.task_id
        allowed = {
            PurePosixPath("scripts/test.gd"),
            PurePosixPath("scripts/test.gd.uid"),
            PurePosixPath("scenes/test.tscn"),
        }
        with zipfile.ZipFile(self.task_archive) as archive:
            for member in archive.infolist():
                relative = _member_relative(member.filename, prefix)
                if relative not in allowed:
                    continue
                target = validation_directory / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))


@dataclass
class GodotHarnessEngineAdapter:
    project: Path
    run_directory: Path
    editable_paths: tuple[str, ...]
    godot: Path
    task_instruction: str = ""
    visual_paths: tuple[Path, ...] = ()
    visual_relative_paths: tuple[str, ...] = ()
    visual_asset_manifest: tuple[PublicAssetRecord, ...] = ()
    agent_executable: Path | None = None
    model_name: str | None = None
    reasoning_effort: str | None = None
    input_usd_per_million: float = 0.0
    cached_input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    name: str = "godot"
    version: str = "headless-v8"
    project_scope: ProjectAccessScope | None = field(default=None, repr=False)
    _baseline_text: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _baseline_mutable_files: dict[str, bytes | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _baseline_replay_unavailable_paths: set[str] = field(
        default_factory=set, init=False, repr=False
    )
    _baseline_replay_bytes: int = field(default=0, init=False, repr=False)
    _baseline_replay_initialized: bool = field(default=False, init=False, repr=False)

    def bind_project_scope(self, scope: ProjectAccessScope) -> None:
        self.project_scope = scope
        self._capture_baseline_mutable_paths(scope.writable_paths)
        self._baseline_replay_initialized = True
        if not self._baseline_text:
            for path in self._readable_text_paths()[0]:
                target = self.project / path
                if target.stat().st_size > _CONTEXT_MAXIMUM_FILE_BYTES:
                    continue
                try:
                    self._baseline_text[path] = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue

    def reconcile_project_changes(
        self,
        changed_paths: tuple[str, ...],
    ) -> dict[str, object]:
        """Canonicalize engine serialization details after any approved write path."""
        resources: list[dict[str, object]] = []
        skipped: list[dict[str, str]] = []
        writable = frozenset(self._writable_paths())
        for raw_path in changed_paths:
            relative = _safe_relative(raw_path)
            normalized = relative.as_posix()
            if relative.suffix.lower() != ".tscn":
                continue
            if normalized not in writable:
                raise PolicyViolation(
                    f"host reconciliation target is outside the approved scope: {normalized}"
                )
            target = self.project / relative
            if not target.is_file() or target.is_symlink():
                skipped.append({"path": normalized, "reason": "not_regular_file"})
                continue
            try:
                document = GodotResourceDocument.parse(target.read_text(encoding="utf-8"))
                changes = _synchronize_script_node_references(document, self.project)
            except (OSError, UnicodeDecodeError, ValueError) as error:
                skipped.append({"path": normalized, "reason": type(error).__name__})
                continue
            if not changes:
                continue
            committed = self._commit_text_write(
                self._validate_text_write(normalized, document.render())
            )
            resources.append(
                {
                    **committed,
                    "rule": "godot-script-export-node-reference-metadata-v1",
                    "changes": changes,
                }
            )
        return {
            "schema_version": 1,
            "status": "success",
            "changed_resource_count": len(resources),
            "metadata_change_count": sum(len(resource["changes"]) for resource in resources),
            "resources": resources,
            "skipped": skipped,
        }

    def _writable_paths(self) -> tuple[str, ...]:
        return self.project_scope.writable_paths if self.project_scope else self.editable_paths

    def available_tools(self) -> tuple[str, ...]:
        return (
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
            "mutate_resource_objects",
            "mutate_engine_resource_properties",
            "write_text_file",
            "write_text_files",
            "replace_text",
            "import_project",
            "capture_scene_evidence",
            "review_visual_change",
        )

    def automatic_validation_tool(self) -> str | None:
        return "import_project"

    def completion_feedback_tools(self) -> tuple[str, ...]:
        return ("review_visual_change",) if self._visual_feedback_configured() else ()

    def workspace_text_suffixes(self) -> tuple[str, ...]:
        return tuple(sorted(_TEXT_SUFFIXES))

    def invoke(self, tool_name: str, arguments: Mapping[str, object]) -> object:
        if tool_name == "inspect_project":
            context = build_project_context(self.project, self.editable_paths)
            readable_paths, readable_total = self._readable_text_paths()
            return {
                "status": "success",
                "project": self.project.name,
                "read_scope": "public-project-text",
                "readable_text_paths": list(readable_paths[:_PROJECT_LIST_MAXIMUM_FILES]),
                "readable_text_count": readable_total,
                "readable_text_paths_truncated": (readable_total > _PROJECT_LIST_MAXIMUM_FILES),
                "writable_paths": list(self._writable_paths()),
                "scope_expansion_enabled": bool(
                    self.project_scope and self.project_scope.allow_text_scope_expansion
                ),
                "text_files": [document.path for document in context.files],
                "assets": [asset.model_dump(mode="json") for asset in context.assets],
                "asset_count_total": context.asset_count_total,
                "assets_truncated": context.assets_truncated,
            }
        if tool_name == "list_project_files":
            return self._list_project_files(arguments)
        if tool_name == "search_project_text":
            return self._search_project_text(arguments)
        if tool_name == "declare_output_manifest":
            return self._declare_output_manifest(arguments)
        if tool_name == "review_project_changes":
            return self._review_project_changes()
        if tool_name == "inspect_scene":
            return self._inspect_scene(arguments)
        if tool_name == "inspect_resource":
            return self._inspect_resource(arguments)
        if tool_name == "inspect_engine_schema":
            return self._inspect_engine_schema(arguments)
        if tool_name == "resolve_scene_node_path":
            return self._resolve_scene_node_path(arguments)
        if tool_name == "read_text_file":
            return self._read_text_file(arguments)
        if tool_name == "read_text_files":
            return self._read_text_files(arguments)
        if tool_name == "inspect_resources":
            return self._inspect_resources(arguments)
        if tool_name == "mutate_resource_objects":
            return self._mutate_resource_objects(arguments)
        if tool_name == "mutate_engine_resource_properties":
            return self._mutate_engine_resource_properties(arguments)
        if tool_name == "write_text_file":
            return self._write_text_file(arguments)
        if tool_name == "write_text_files":
            return self._write_text_files(arguments)
        if tool_name == "replace_text":
            return self._replace_text(arguments)
        if tool_name == "import_project":
            return self._import_project()
        if tool_name == "capture_scene_evidence":
            return self._capture_scene_evidence()
        if tool_name == "review_visual_change":
            return self._review_visual_change()
        raise ValueError(f"unsupported Godot harness tool: {tool_name}")

    def _list_project_files(self, arguments: Mapping[str, object]) -> dict[str, object]:
        paths, total = self._readable_text_paths()
        cursor = arguments.get("cursor", 0)
        limit = arguments.get("limit", _PROJECT_LIST_MAXIMUM_FILES)
        prefix = arguments.get("path_prefix")
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("list_project_files cursor must be a non-negative integer")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _PROJECT_LIST_MAXIMUM_FILES
        ):
            raise ValueError(
                f"list_project_files limit must be between 1 and {_PROJECT_LIST_MAXIMUM_FILES}"
            )
        if prefix is not None and not isinstance(prefix, str):
            raise ValueError("list_project_files path_prefix must be a string")
        if isinstance(prefix, str) and not prefix.strip():
            prefix = None
        normalized_prefix = (
            PurePosixPath(str(prefix).strip()).as_posix().rstrip("/")
            if prefix is not None
            else None
        )
        if normalized_prefix is not None and (
            normalized_prefix.startswith("/") or ".." in PurePosixPath(normalized_prefix).parts
        ):
            raise ValueError("list_project_files path_prefix must be project-relative")
        filtered = tuple(
            path
            for path in paths
            if normalized_prefix is None
            or path == normalized_prefix
            or path.startswith(f"{normalized_prefix}/")
        )
        page = filtered[cursor : cursor + limit]
        files = []
        for path in page:
            target = self.project / path
            files.append(
                {
                    "path": path,
                    "suffix": Path(path).suffix.lower(),
                    "bytes": target.stat().st_size,
                    "writable": path in self._writable_paths(),
                }
            )
        return {
            "status": "success",
            "files": files,
            "file_count_total": total,
            "file_count_indexed": len(paths),
            "filtered_file_count": len(filtered),
            "cursor": cursor,
            "next_cursor": (cursor + len(page) if cursor + len(page) < len(filtered) else None),
            "coverage_complete": total == len(paths),
            "index_limit": _PROJECT_INDEX_MAXIMUM_FILES,
            "truncated": cursor + len(page) < len(filtered) or total > len(paths),
        }

    def _readable_text_paths(self) -> tuple[tuple[str, ...], int]:
        paths: list[str] = []
        total = 0
        root = self.project.resolve(strict=True)
        for directory, names, files in os.walk(root, topdown=True, followlinks=False):
            names[:] = sorted(
                name
                for name in names
                if name not in IGNORED_DIRECTORY_NAMES and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(files):
                target = Path(directory) / name
                if target.is_symlink() or target.suffix.lower() not in _PUBLIC_TEXT_SUFFIXES:
                    continue
                total += 1
                if len(paths) < _PROJECT_INDEX_MAXIMUM_FILES:
                    paths.append(target.relative_to(root).as_posix())
        return tuple(paths), total

    def _search_project_text(self, arguments: Mapping[str, object]) -> dict[str, object]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search_project_text requires a non-empty string query")
        if len(query) > _PROJECT_SEARCH_MAXIMUM_QUERY_CHARACTERS:
            raise ValueError(
                "search_project_text query exceeds "
                f"{_PROJECT_SEARCH_MAXIMUM_QUERY_CHARACTERS} characters"
            )
        paths, total = self._readable_text_paths()
        folded_query = query.casefold()
        matches: list[dict[str, object]] = []
        scanned = 0
        skipped_large = 0
        skipped_non_text = 0
        for path in paths:
            target = self.project / path
            if target.stat().st_size > _CONTEXT_MAXIMUM_FILE_BYTES:
                skipped_large += 1
                continue
            try:
                content, _ = _decode_public_text(target.read_bytes())
            except ValueError:
                skipped_non_text += 1
                continue
            scanned += 1
            lines = content.splitlines()
            for line_number, line in enumerate(lines, start=1):
                if folded_query not in line.casefold():
                    continue
                matches.append(
                    {
                        "path": path,
                        "line": line_number,
                        "excerpt": line.strip()[:500],
                    }
                )
                if len(matches) >= _PROJECT_SEARCH_MAXIMUM_MATCHES:
                    return {
                        "status": "success",
                        "query": query,
                        "matches": matches,
                        "truncated": True,
                        "files_scanned": scanned,
                        "files_indexed": len(paths),
                        "file_count_total": total,
                        "coverage_complete": (
                            total == len(paths) and skipped_large == 0 and skipped_non_text == 0
                        ),
                        "index_limit": _PROJECT_INDEX_MAXIMUM_FILES,
                        "match_limit": _PROJECT_SEARCH_MAXIMUM_MATCHES,
                        "large_files_skipped": skipped_large,
                        "non_text_files_skipped": skipped_non_text,
                    }
        return {
            "status": "success",
            "query": query,
            "matches": matches,
            "truncated": (total > len(paths) or skipped_large > 0 or skipped_non_text > 0),
            "files_scanned": scanned,
            "files_indexed": len(paths),
            "file_count_total": total,
            "coverage_complete": (
                total == len(paths) and skipped_large == 0 and skipped_non_text == 0
            ),
            "index_limit": _PROJECT_INDEX_MAXIMUM_FILES,
            "match_limit": _PROJECT_SEARCH_MAXIMUM_MATCHES,
            "large_files_skipped": skipped_large,
            "non_text_files_skipped": skipped_non_text,
        }

    def _declare_output_manifest(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_paths = arguments.get("paths")
        reason = arguments.get("reason")
        if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
            raise ValueError("declare_output_manifest requires a list of string paths")
        if not isinstance(reason, str):
            raise ValueError("declare_output_manifest requires a string reason")
        if self.project_scope is None:
            raise PolicyViolation("runtime project scope is not bound")
        normalized: list[str] = []
        for raw_path in raw_paths:
            relative = _safe_relative(raw_path)
            if (
                any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts)
                or relative.suffix.lower() in IGNORED_FILE_SUFFIXES
            ):
                raise PolicyViolation(
                    "output manifest cannot target project caches or generated metadata: "
                    f"{relative.as_posix()}"
                )
            target = self.project / relative
            if target.is_symlink() or not target.resolve(strict=False).is_relative_to(
                self.project.resolve(strict=True)
            ):
                raise PolicyViolation(
                    f"output manifest path is not a safe project target: {relative.as_posix()}"
                )
            normalized.append(relative.as_posix())
        self._capture_baseline_mutable_paths(tuple(normalized))
        try:
            declaration = self.project_scope.declare_output_manifest(
                tuple(normalized), reason=reason
            )
        except ProjectScopeError as error:
            raise PolicyViolation(str(error)) from error
        return {"status": "success", **declaration}

    def _review_project_changes(self) -> dict[str, object]:
        paths, _ = self._readable_text_paths()
        current: dict[str, str] = {}
        for path in paths:
            target = self.project / path
            if target.stat().st_size > _CONTEXT_MAXIMUM_FILE_BYTES:
                continue
            try:
                current[path] = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        changed_paths = tuple(
            sorted(
                path
                for path in set(self._baseline_text) | set(current)
                if self._baseline_text.get(path) != current.get(path)
            )
        )
        files: list[dict[str, object]] = []
        used_bytes = 0
        truncated = False
        for path in changed_paths:
            before = self._baseline_text.get(path)
            after = current.get(path)
            diff = "".join(
                difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    (after or "").splitlines(keepends=True),
                    fromfile=f"before/{path}",
                    tofile=f"after/{path}",
                    n=3,
                )
            )
            encoded = diff.encode("utf-8")
            if used_bytes + len(encoded) > _PROJECT_DIFF_MAXIMUM_BYTES:
                remaining = max(0, _PROJECT_DIFF_MAXIMUM_BYTES - used_bytes)
                diff = encoded[:remaining].decode("utf-8", errors="ignore")
                truncated = True
            used_bytes += len(diff.encode("utf-8"))
            files.append(
                {
                    "path": path,
                    "change": (
                        "created" if before is None else "deleted" if after is None else "modified"
                    ),
                    "diff": diff,
                }
            )
            if truncated:
                break
        return {
            "status": "success",
            "changed_paths": list(changed_paths),
            "files": files,
            "truncated": truncated,
            "writable_paths": list(self._writable_paths()),
            "output_declarations": (
                list(self.project_scope.declarations) if self.project_scope else []
            ),
            "guidance": (
                "Review every public task requirement against these actual changes and current "
                "structured resources before proposing finish."
            ),
        }

    def _visual_feedback_configured(self) -> bool:
        return bool(
            self.visual_paths
            and self.visual_relative_paths
            and self.agent_executable is not None
            and self.model_name
        )

    def _inspect_scene(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_scene = arguments.get("scene")
        if not isinstance(raw_scene, str):
            raise ValueError("inspect_scene requires a string scene path")
        result = self._structured_resource(raw_scene, required_suffixes={".tscn"})
        return {"status": "success", "scene": result["path"], **result}

    def _inspect_resource(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            raise ValueError("inspect_resource requires a string path")
        return {
            "status": "success",
            **self._structured_resource(raw_path, required_suffixes={".tscn", ".tres"}),
        }

    def _inspect_engine_schema(self, arguments: Mapping[str, object]) -> dict[str, object]:
        class_name = arguments.get("class_name")
        name_filter = arguments.get("name_filter", "")
        include_inherited = arguments.get("include_inherited", True)
        if (
            not isinstance(class_name, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", class_name) is None
        ):
            raise ValueError("inspect_engine_schema requires a valid ClassDB class_name")
        if not isinstance(name_filter, str) or len(name_filter) > 128:
            raise ValueError("inspect_engine_schema name_filter must be at most 128 characters")
        if not isinstance(include_inherited, bool):
            raise ValueError("inspect_engine_schema include_inherited must be boolean")
        script = Path(__file__).parents[1] / "resources" / "godot" / "engine_schema_probe.gd"
        if not script.is_file():
            raise ValueError("engine schema probe script is unavailable")
        request = {
            "class_name": class_name,
            "name_filter": name_filter,
            "include_inherited": include_inherited,
        }
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()[
            :12
        ]
        self.run_directory.mkdir(parents=True, exist_ok=True)
        request_path = self.run_directory / f"engine-schema-{digest}-request.json"
        output_path = self.run_directory / f"engine-schema-{digest}.json"
        log_path = self.run_directory / f"engine-schema-{digest}.log"
        request_path.write_text(json.dumps(request, sort_keys=True) + "\n", encoding="utf-8")
        output_path.unlink(missing_ok=True)
        with _isolated_godot_project(self.project) as sandbox:
            completed = _run_bounded_process(
                [
                    str(self.godot),
                    "--headless",
                    "--path",
                    str(sandbox.project),
                    "--script",
                    str(script),
                    "--log-file",
                    str(log_path),
                    "--",
                    str(request_path),
                    str(output_path),
                ],
                timeout_seconds=60,
            )
        process_output = (completed.stdout or "") + (completed.stderr or "")
        log_path.write_text(process_output, encoding="utf-8")
        if (
            completed.returncode != 0
            or "GAMEFORGE_ENGINE_SCHEMA_OK" not in process_output
            or not output_path.is_file()
        ):
            raise ValueError("engine schema probe failed: " + process_output.strip()[-2000:])
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("engine schema probe returned invalid JSON") from error
        if not isinstance(result, dict):
            raise ValueError("engine schema probe returned a non-object")
        return {
            "status": "success",
            **result,
            "evidence_path": output_path.name,
            "log_path": log_path.name,
        }

    def _resolve_scene_node_path(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_scene = arguments.get("scene")
        property_owner_node = arguments.get("property_owner_node")
        to_node = arguments.get("to_node")
        if not all(isinstance(value, str) for value in (raw_scene, property_owner_node, to_node)):
            raise ValueError(
                "resolve_scene_node_path requires string scene, property_owner_node, and to_node"
            )
        assert isinstance(raw_scene, str)
        assert isinstance(property_owner_node, str)
        assert isinstance(to_node, str)
        snapshot = self._structured_resource(raw_scene, required_suffixes={".tscn"})
        scene_target = self.project / _safe_relative(raw_scene)
        document = GodotResourceDocument.parse(scene_target.read_text(encoding="utf-8"))
        complete_snapshot = document.snapshot(
            path=str(snapshot["path"]),
            maximum_sections=max(1, len(document.sections)),
        )
        sections = complete_snapshot["sections"]
        assert isinstance(sections, list)
        node_paths = _scene_rooted_node_paths(sections)
        source_planned = property_owner_node not in node_paths
        if source_planned:
            source_parent = PurePosixPath(property_owner_node).parent.as_posix()
            if source_parent not in node_paths:
                raise ValueError(
                    "property owner node and its planned parent do not exist in scene: "
                    f"{property_owner_node}"
                )
        target_planned = to_node not in node_paths
        if target_planned:
            target_parent = PurePosixPath(to_node).parent.as_posix()
            if target_parent not in node_paths:
                raise ValueError(
                    f"target node and its planned parent do not exist in scene: {to_node}"
                )
        source_parts = PurePosixPath(property_owner_node).parts
        target_parts = PurePosixPath(to_node).parts
        common = 0
        for source_part, target_part in zip(source_parts, target_parts, strict=False):
            if source_part != target_part:
                break
            common += 1
        relative_parts = (*(("..",) * (len(source_parts) - common)), *target_parts[common:])
        node_path = "/".join(relative_parts) or "."
        return {
            "status": "success",
            "scene": snapshot["path"],
            "property_owner_node": property_owner_node,
            "to_node": to_node,
            "node_path": node_path,
            "common_ancestor": "/".join(source_parts[:common]),
            "source_planned": source_planned,
            "target_planned": target_planned,
        }

    def _read_text_file(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            raise ValueError("read_text_file requires a string path")
        raw_cursor = arguments.get("cursor", 0)
        raw_limit = arguments.get("limit", _CONTEXT_MAXIMUM_FILE_BYTES)
        if not isinstance(raw_cursor, int) or isinstance(raw_cursor, bool) or raw_cursor < 0:
            raise ValueError("read_text_file cursor must be a non-negative byte offset")
        if (
            not isinstance(raw_limit, int)
            or isinstance(raw_limit, bool)
            or not 4 <= raw_limit <= _CONTEXT_MAXIMUM_FILE_BYTES
        ):
            raise ValueError(
                f"read_text_file limit must be between 4 and {_CONTEXT_MAXIMUM_FILE_BYTES} bytes"
            )
        relative = _safe_relative(raw_path)
        normalized = relative.as_posix()
        if relative.suffix.lower() not in _PUBLIC_TEXT_SUFFIXES:
            raise PolicyViolation(f"unsupported text asset suffix: {relative.suffix}")
        target = self.project / relative
        project = self.project.resolve(strict=True)
        if (
            not target.is_file()
            or target.is_symlink()
            or not target.resolve(strict=True).is_relative_to(project)
        ):
            raise ValueError(f"public text target does not exist: {normalized}")
        payload = target.read_bytes()
        try:
            content, source_encoding = _decode_public_text(payload)
        except ValueError as error:
            raise ValueError(f"public text target cannot be decoded: {normalized}") from error
        text_payload = content.encode("utf-8")
        cursor_clamped = raw_cursor > len(text_payload)
        effective_cursor = min(raw_cursor, len(text_payload))
        try:
            text_payload[:effective_cursor].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("read_text_file cursor must align to a UTF-8 boundary") from error
        end = min(len(text_payload), effective_cursor + raw_limit)
        while end > effective_cursor:
            try:
                page_content = text_payload[effective_cursor:end].decode("utf-8")
                break
            except UnicodeDecodeError as error:
                end = effective_cursor + error.start
        else:
            page_content = ""
        next_cursor = end if end < len(text_payload) else None
        return {
            "status": "success",
            "path": normalized,
            "content": page_content,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "source_encoding": source_encoding,
            "source_bytes": len(payload),
            "requested_cursor": raw_cursor,
            "cursor": effective_cursor,
            "cursor_clamped": cursor_clamped,
            "next_cursor": next_cursor,
            "total_bytes": len(text_payload),
            "coverage_complete": next_cursor is None,
        }

    def _read_text_files(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_paths = arguments.get("paths")
        if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= _BATCH_TEXT_MAXIMUM_FILES:
            raise ValueError(
                f"read_text_files requires between 1 and {_BATCH_TEXT_MAXIMUM_FILES} paths"
            )
        if not all(isinstance(path, str) for path in raw_paths):
            raise ValueError("read_text_files paths must be strings")
        per_file_limit = min(
            _CONTEXT_MAXIMUM_FILE_BYTES,
            _BATCH_TEXT_MAXIMUM_BYTES // len(raw_paths),
        )
        files = [
            self._read_text_file({"path": path, "limit": per_file_limit}) for path in raw_paths
        ]
        total_bytes = sum(len(str(item["content"]).encode("utf-8")) for item in files)
        if total_bytes > _BATCH_TEXT_MAXIMUM_BYTES:
            raise ValueError(f"read_text_files content exceeds {_BATCH_TEXT_MAXIMUM_BYTES} bytes")
        return {"status": "success", "files": files, "total_bytes": total_bytes}

    def _inspect_resources(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_paths = arguments.get("paths")
        if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= _BATCH_TEXT_MAXIMUM_FILES:
            raise ValueError(
                f"inspect_resources requires between 1 and {_BATCH_TEXT_MAXIMUM_FILES} paths"
            )
        if not all(isinstance(path, str) for path in raw_paths):
            raise ValueError("inspect_resources paths must be strings")
        resources = [
            {
                "status": "success",
                **self._structured_resource(path, required_suffixes={".tscn", ".tres"}),
            }
            for path in raw_paths
        ]
        return {"status": "success", "resources": resources}

    def _structured_resource(
        self,
        raw_path: str,
        *,
        required_suffixes: set[str],
    ) -> dict[str, object]:
        relative = _safe_relative(raw_path)
        normalized = relative.as_posix()
        if relative.suffix.lower() not in required_suffixes:
            raise PolicyViolation(
                "structured resource inspection only accepts public .tscn/.tres paths"
            )
        target = self.project / relative
        project = self.project.resolve(strict=True)
        if (
            not target.is_file()
            or target.is_symlink()
            or not target.resolve(strict=True).is_relative_to(project)
        ):
            raise ValueError(f"structured resource target does not exist: {normalized}")
        return resource_snapshot(target.read_text(encoding="utf-8"), path=normalized)

    def _mutate_resource_objects(
        self,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        raw_path = arguments.get("path")
        raw_operations = arguments.get("operations")
        if not isinstance(raw_path, str) or not isinstance(raw_operations, list):
            raise ValueError("mutate_resource_objects requires path and operations")
        relative = _safe_relative(raw_path)
        if relative.suffix.lower() not in {".tscn", ".tres"}:
            raise PolicyViolation("object mutations require an approved .tscn or .tres path")
        normalized = relative.as_posix()
        target = self.project / relative
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"object mutation target does not exist: {normalized}")
        document = GodotResourceDocument.parse(target.read_text(encoding="utf-8"))
        request = ResourceMutationRequest.model_validate({"operations": raw_operations})
        changes = document.mutate(request)
        changes.extend(_synchronize_script_node_references(document, self.project))
        rendered = document.render()
        write = self._validate_text_write(raw_path, rendered)
        committed = self._commit_text_write(write)
        snapshot = document.snapshot(path=normalized)
        return {
            "status": "success",
            **committed,
            "changes": changes,
            "assertions": snapshot["assertions"],
            "section_count": snapshot["section_count"],
            "resource_sha256": snapshot["sha256"],
        }

    def _mutate_engine_resource_properties(
        self,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        raw_path = arguments.get("path")
        raw_operations = arguments.get("operations")
        if not isinstance(raw_path, str) or not isinstance(raw_operations, list):
            raise ValueError("mutate_engine_resource_properties requires path and operations")
        if not 1 <= len(raw_operations) <= 32:
            raise ValueError("engine resource mutation requires between 1 and 32 operations")
        relative = _safe_relative(raw_path)
        normalized = relative.as_posix()
        if relative.suffix.lower() not in _ENGINE_RESOURCE_SUFFIXES:
            raise PolicyViolation(
                "engine resource property mutation only accepts supported native resources"
            )
        if normalized not in self._writable_paths():
            raise PolicyViolation(f"path is outside the approved editable set: {normalized}")
        target = self.project / relative
        project = self.project.resolve(strict=True)
        if (
            not target.is_file()
            or target.is_symlink()
            or not target.resolve(strict=True).is_relative_to(project)
        ):
            raise ValueError(f"engine resource target does not exist: {normalized}")
        operations: list[dict[str, object]] = []
        for index, operation in enumerate(raw_operations):
            if not isinstance(operation, dict) or set(operation) != {"property", "value"}:
                raise ValueError(f"engine resource operation {index} requires property and value")
            property_name = operation["property"]
            if (
                not isinstance(property_name, str)
                or re.fullmatch(r"[A-Za-z0-9_./:-]+", property_name) is None
            ):
                raise ValueError(f"engine resource operation {index} has invalid property")
            _validate_engine_variant(operation["value"])
            operations.append({"property": property_name, "value": operation["value"]})

        request = {"path": normalized, "operations": operations}
        encoded_request = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(encoded_request.encode("utf-8")).hexdigest()[:12]
        self.run_directory.mkdir(parents=True, exist_ok=True)
        script_path = self.run_directory / "godot-engine-resource-mutation.gd"
        request_path = self.run_directory / f"engine-resource-mutation-{digest}.json"
        log_path = self.run_directory / f"engine-resource-mutation-{digest}.log"
        script_path.write_text(_ENGINE_RESOURCE_MUTATION_SCRIPT, encoding="utf-8")
        request_path.write_text(encoded_request + "\n", encoding="utf-8")
        before = target.read_bytes()
        before_sha256 = hashlib.sha256(before).hexdigest()
        try:
            completed = _run_bounded_process(
                [
                    str(self.godot),
                    "--headless",
                    "--path",
                    str(self.project),
                    "--log-file",
                    str(log_path),
                    "--script",
                    str(script_path),
                    "--",
                    str(request_path),
                ],
                timeout_seconds=120,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            if (
                completed.returncode != 0
                or "GAMEFORGE_ENGINE_RESOURCE_MUTATION_OK" not in output
                or not target.is_file()
            ):
                raise ValueError("engine resource mutation failed: " + output.strip()[-2000:])
        except Exception:
            target.write_bytes(before)
            raise
        after = target.read_bytes()
        return {
            "status": "success",
            "path": normalized,
            "properties": [operation["property"] for operation in operations],
            "sha256_before": before_sha256,
            "sha256_after": hashlib.sha256(after).hexdigest(),
            "log_path": log_path.name,
        }

    def _write_text_file(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise ValueError("write_text_file requires string path and content")
        write = self._validate_text_write(raw_path, content)
        return {"status": "success", **self._commit_text_write(write)}

    def _write_text_files(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_files = arguments.get("files")
        if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= _BATCH_TEXT_MAXIMUM_FILES:
            raise ValueError(
                f"write_text_files requires between 1 and {_BATCH_TEXT_MAXIMUM_FILES} files"
            )
        writes: list[tuple[str, Path, bytes, str | None]] = []
        seen: set[str] = set()
        total_bytes = 0
        for entry in raw_files:
            if not isinstance(entry, dict) or set(entry) != {"path", "content"}:
                raise ValueError("write_text_files entries require exactly path and content")
            raw_path = entry["path"]
            content = entry["content"]
            if not isinstance(raw_path, str) or not isinstance(content, str):
                raise ValueError("write_text_files path and content values must be strings")
            write = self._validate_text_write(raw_path, content)
            if write[0] in seen:
                raise ValueError(f"write_text_files contains duplicate path: {write[0]}")
            seen.add(write[0])
            total_bytes += len(write[2])
            if total_bytes > _BATCH_TEXT_MAXIMUM_BYTES:
                raise ValueError(
                    f"write_text_files content exceeds {_BATCH_TEXT_MAXIMUM_BYTES} bytes"
                )
            writes.append(write)
        return {
            "status": "success",
            "files": [self._commit_text_write(write) for write in writes],
        }

    def _replace_text(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_path = arguments.get("path")
        expected = arguments.get("expected")
        replacement = arguments.get("replacement")
        if not all(isinstance(value, str) for value in (raw_path, expected, replacement)):
            raise ValueError("replace_text requires string path, expected, and replacement")
        assert isinstance(raw_path, str)
        assert isinstance(expected, str)
        assert isinstance(replacement, str)
        if not expected:
            raise ValueError("replace_text expected anchor must not be empty")
        relative = _safe_relative(raw_path)
        target = self.project / relative
        if not target.is_file():
            raise ValueError(f"replace_text target does not exist: {raw_path}")
        try:
            current = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"replace_text target is not UTF-8: {raw_path}") from error
        occurrences = current.count(expected)
        if occurrences != 1:
            reason = "missing" if occurrences == 0 else "ambiguous"
            raise ValueError(f"replace_text anchor is {reason}: {raw_path}")
        write = self._validate_text_write(raw_path, current.replace(expected, replacement, 1))
        return {"status": "success", **self._commit_text_write(write)}

    def _validate_text_write(
        self, raw_path: str, content: str
    ) -> tuple[str, Path, bytes, str | None]:
        relative = _safe_relative(raw_path)
        normalized = relative.as_posix()
        if normalized not in self._writable_paths():
            raise PolicyViolation(f"path is not in the approved editable set: {normalized}")
        if relative.suffix.lower() not in _TEXT_SUFFIXES:
            raise PolicyViolation(f"unsupported Godot text asset suffix: {relative.suffix}")
        payload = content.encode("utf-8")
        if len(payload) > 128 * 1024:
            raise ValueError("write_text_file content exceeds 128 KiB")
        target = self.project / relative
        if target.is_symlink():
            raise PolicyViolation(f"write_text_file refuses symbolic links: {normalized}")
        project = self.project.resolve(strict=True)
        resolved_target = target.resolve(strict=False)
        if not resolved_target.is_relative_to(project):
            raise PolicyViolation(f"write_text_file path escapes project: {normalized}")
        before = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        return normalized, target, payload, before

    @staticmethod
    def _commit_text_write(
        write: tuple[str, Path, bytes, str | None],
    ) -> dict[str, object]:
        normalized, target, payload, before = write
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, target)
        after = hashlib.sha256(payload).hexdigest()
        return {
            "path": normalized,
            "sha256_before": before,
            "sha256_after": after,
        }

    def _capture_baseline_mutable_paths(self, paths: tuple[str, ...]) -> None:
        for raw_path in paths:
            normalized = _safe_relative(raw_path).as_posix()
            if (
                normalized in self._baseline_mutable_files
                or normalized in self._baseline_replay_unavailable_paths
            ):
                continue
            target = self.project / normalized
            if not target.exists():
                self._baseline_mutable_files[normalized] = None
                continue
            if not target.is_file() or target.is_symlink():
                self._baseline_replay_unavailable_paths.add(normalized)
                continue
            size = target.stat().st_size
            if (
                size > _BASELINE_REPLAY_MAXIMUM_FILE_BYTES
                or self._baseline_replay_bytes + size > _BASELINE_REPLAY_MAXIMUM_TOTAL_BYTES
            ):
                self._baseline_replay_unavailable_paths.add(normalized)
                continue
            payload = target.read_bytes()
            self._baseline_mutable_files[normalized] = payload
            self._baseline_replay_bytes += len(payload)

    def _restore_validation_baseline(self, validation: Path) -> None:
        if self._baseline_replay_unavailable_paths:
            raise ValueError("baseline replay is unavailable for one or more writable paths")
        for path, payload in self._baseline_mutable_files.items():
            target = validation / path
            if payload is None:
                if target.is_file() or target.is_symlink():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        _disable_editor_plugins(validation / "project.godot")

    def _import_project(self) -> dict[str, object]:
        current = self._run_public_validation(log_prefix="godot-public")
        current["validation_basis"] = "absolute"
        baseline_replay_available = (
            self._baseline_replay_initialized and not self._baseline_replay_unavailable_paths
        )
        current["baseline_replay_available"] = baseline_replay_available
        if current["status"] == "success" or not baseline_replay_available:
            return current
        baseline = self._run_public_validation(
            log_prefix="godot-public-baseline",
            restore_baseline=True,
        )
        regressions, new_diagnostics = _public_validation_regressions(current, baseline)
        current["raw_status"] = current["status"]
        current["baseline_status"] = baseline["status"]
        current["baseline_diagnostics"] = baseline["diagnostics"]
        current["baseline_log_paths"] = {
            key: baseline.get(key)
            for key in (
                "import_log_path",
                "resource_log_path",
                "startup_log_path",
            )
        }
        current["new_diagnostics"] = new_diagnostics
        current["regressions"] = regressions
        if not regressions:
            current["status"] = "success"
            current["diagnostics"] = []
            current["validation_basis"] = "relative_to_preexisting_baseline"
        else:
            current["validation_basis"] = "regression_against_preexisting_baseline"
        return current

    def _run_public_validation(
        self,
        *,
        log_prefix: str,
        restore_baseline: bool = False,
    ) -> dict[str, object]:
        self.run_directory.mkdir(parents=True, exist_ok=True)
        import_log = self.run_directory / f"{log_prefix}-import.log"
        resource_log = self.run_directory / f"{log_prefix}-resource-load.log"
        resource_script = self.run_directory / f"{log_prefix}-resource-load.gd"
        startup_log = self.run_directory / f"{log_prefix}-startup.log"
        with _isolated_godot_project(self.project) as sandbox:
            validation = sandbox.project
            if restore_baseline:
                self._restore_validation_baseline(validation)
            resource_paths = tuple(
                f"res://{path}"
                for path in self._writable_paths()
                if Path(path).suffix.lower() in {".tscn", ".tres"} and (validation / path).is_file()
            )
            imported = _run_bounded_process(
                [
                    str(self.godot),
                    "--headless",
                    "--import",
                    "--quit",
                    "--path",
                    str(validation),
                    "--log-file",
                    str(import_log),
                ],
                timeout_seconds=_PUBLIC_VALIDATION_TIMEOUT_SECONDS,
            )
            import_output = imported.stdout + imported.stderr
            import_log.write_text(import_output, encoding="utf-8")
            diagnostics = _public_diagnostics(import_output)
            resource_return_code: int | None = None
            if imported.returncode == 0 and not diagnostics and resource_paths:
                resource_script.write_text(_RESOURCE_VALIDATION_SCRIPT, encoding="utf-8")
                loaded = _run_bounded_process(
                    [
                        str(self.godot),
                        "--headless",
                        "--path",
                        str(validation),
                        "--script",
                        str(resource_script),
                        "--log-file",
                        str(resource_log),
                        "--",
                        *resource_paths,
                    ],
                    timeout_seconds=_PUBLIC_STARTUP_TIMEOUT_SECONDS,
                )
                resource_return_code = loaded.returncode
                resource_output = loaded.stdout + loaded.stderr
                resource_log.write_text(resource_output, encoding="utf-8")
                diagnostics.extend(_public_diagnostics(resource_output))
            startup_return_code: int | None = None
            startup_required = _project_has_main_scene(validation / "project.godot")
            resources_passed = resource_return_code in {None, 0}
            if (
                imported.returncode == 0
                and not diagnostics
                and resources_passed
                and startup_required
            ):
                started = _run_bounded_process(
                    [
                        str(self.godot),
                        "--headless",
                        "--path",
                        str(validation),
                        "--quit-after",
                        "3",
                        "--log-file",
                        str(startup_log),
                    ],
                    timeout_seconds=_PUBLIC_STARTUP_TIMEOUT_SECONDS,
                )
                startup_return_code = started.returncode
                startup_output = started.stdout + started.stderr
                startup_log.write_text(startup_output, encoding="utf-8")
                diagnostics.extend(_public_diagnostics(startup_output))
        startup_inconclusive = startup_return_code == 124 and not diagnostics
        passed = (
            imported.returncode == 0
            and not diagnostics
            and resources_passed
            and (not startup_required or startup_return_code == 0 or startup_inconclusive)
        )
        return {
            "status": "success" if passed else "failed",
            "isolated_validation_copy": True,
            "editor_plugins_disabled_in_validation_copy": True,
            "user_data_policy": "host-path-failures-are-infrastructure",
            "import_return_code": imported.returncode,
            "resource_return_code": resource_return_code,
            "resource_paths": list(resource_paths),
            "startup_return_code": startup_return_code,
            "startup_required": startup_required,
            "startup_status": (
                "pass"
                if startup_return_code == 0
                else "inconclusive_timeout"
                if startup_inconclusive
                else "not_applicable_no_main_scene"
                if not startup_required
                else "fail"
            ),
            "diagnostics": diagnostics[:40],
            "import_log_path": str(import_log),
            "resource_log_path": str(resource_log) if resource_log.exists() else None,
            "resource_script_path": (str(resource_script) if resource_script.exists() else None),
            "startup_log_path": str(startup_log) if startup_log.exists() else None,
        }

    def _review_visual_change(self) -> dict[str, object]:
        if not self._visual_feedback_configured():
            return {
                "status": "not_applicable",
                "verdict": "uncertain",
                "summary": "no bounded visual feedback configuration is available",
            }
        attempt = len(tuple(self.run_directory.glob("visual-feedback-*.json"))) + 1
        stem = f"visual-feedback-{attempt:02d}"
        screenshot_path = self.run_directory / f"{stem}.png"
        capture_log = self.run_directory / f"{stem}-capture.log"
        capture = _capture_fixed_camera(self, screenshot_path, capture_log)
        prior_evidence_path = (
            self.run_directory / f"visual-feedback-{attempt - 1:02d}.json" if attempt > 1 else None
        )
        prior_evidence = (
            json.loads(prior_evidence_path.read_text(encoding="utf-8"))
            if prior_evidence_path is not None and prior_evidence_path.is_file()
            else None
        )
        prior_screenshot_path = (
            self.run_directory / f"visual-feedback-{attempt - 1:02d}.png"
            if prior_evidence is not None
            else None
        )
        structured_state = self._public_structured_state()
        evidence: dict[str, object] = {
            "schema_version": 1,
            "phase": "pre-evaluation-completion-feedback",
            "advisory_only": True,
            "can_replace_structured_gates": False,
            "source_images": list(self.visual_relative_paths),
            "public_asset_manifest": [
                asset.model_dump(mode="json") for asset in self.visual_asset_manifest
            ],
            "candidate_screenshot": screenshot_path.name if screenshot_path.is_file() else None,
            "previous_candidate_screenshot": (
                prior_screenshot_path.name
                if prior_screenshot_path is not None and prior_screenshot_path.is_file()
                else None
            ),
            "capture": capture,
            "structured_state": structured_state,
            "verdict": "uncertain",
            "progress": "not_applicable" if prior_evidence is None else "unchanged",
            "grounded_objects": [],
            "summary": "fixed-camera capture was unavailable",
        }
        response = None
        if capture["status"] == "success" and screenshot_path.is_file():
            system = (
                "You are a bounded public visual feedback tool inside a game-engine Harness. "
                "Compare the supplied public source assets and fixed-camera candidate frame "
                "against the visible requirements in the task. Ground every claim in the supplied "
                "public node/resource state, coordinate evidence, capture fidelity diagnostics, "
                "and structural assertions. Treat a degraded capture as uncertain rather than as "
                "evidence that a candidate asset is absent. If a previous candidate is "
                "supplied, compare it with the current frame and report whether visible "
                "requirement coverage improved, stayed unchanged, or regressed. Never infer "
                "gameplay state or "
                "claim final correctness. Return only the requested JSON object."
            )
            image_labels = [*self.visual_relative_paths]
            reviewer_images = [*self.visual_paths]
            if prior_screenshot_path is not None and prior_screenshot_path.is_file():
                image_labels.append(f"{prior_screenshot_path.name} (previous candidate)")
                reviewer_images.append(prior_screenshot_path)
            image_labels.append(f"{screenshot_path.name} (current candidate)")
            reviewer_images.append(screenshot_path)
            prompt = json.dumps(
                {
                    "task_instruction": self.task_instruction,
                    "images_in_order": image_labels,
                    "public_asset_manifest": [
                        asset.model_dump(mode="json") for asset in self.visual_asset_manifest
                    ],
                    "structured_candidate_state": structured_state,
                    "previous_review": (
                        {
                            "verdict": prior_evidence.get("verdict"),
                            "summary": prior_evidence.get("summary"),
                            "grounded_objects": prior_evidence.get("grounded_objects", []),
                        }
                        if prior_evidence is not None
                        else None
                    ),
                    "decision_schema": {
                        "verdict": "pass|fail|uncertain",
                        "progress": (
                            "not_applicable for the first candidate; otherwise "
                            "improved|unchanged|regressed"
                        ),
                        "summary": "actionable visual evidence only",
                        "grounded_objects": "public scene node/resource identities used",
                        "structured_evidence_acknowledged": True,
                        "measurement_basis": (
                            "none|qualitative|quantitative; quantitative requires supplied numeric "
                            "target/comparison evidence, not visual confidence alone"
                        ),
                        "uncertainties": "remaining evidence limitations",
                    },
                },
                sort_keys=True,
            )
            assert self.agent_executable is not None
            assert self.model_name is not None
            reviewer = AgentCliLanguageModel(
                self.agent_executable,
                self.model_name,
                timeout_seconds=300,
                input_usd_per_million=self.input_usd_per_million,
                cached_input_usd_per_million=self.cached_input_usd_per_million,
                output_usd_per_million=self.output_usd_per_million,
                reasoning_effort=self.reasoning_effort,
                image_paths=tuple(reviewer_images),
            )
            try:
                response = reviewer.complete(system=system, prompt=prompt)
                decision = VisualReviewDecision.model_validate_json(response.text)
                verdict = (
                    decision.verdict if decision.structured_evidence_acknowledged else "uncertain"
                )
                if (
                    verdict == "pass"
                    and _requires_quantitative_visual_evidence(self.task_instruction)
                    and decision.measurement_basis != "quantitative"
                ):
                    verdict = "uncertain"
                progress = (
                    "not_applicable"
                    if prior_evidence is None
                    else decision.progress
                    if decision.progress != "not_applicable"
                    else "unchanged"
                )
                evidence.update(
                    {
                        "verdict": verdict,
                        "progress": progress,
                        "summary": decision.summary,
                        "grounded_objects": list(decision.grounded_objects),
                        "measurement_basis": decision.measurement_basis,
                        "uncertainties": list(decision.uncertainties),
                        "model_trace": response.trace_record(system=system, prompt=prompt),
                    }
                )
            except (ModelError, ValueError) as error:
                evidence.update(
                    {
                        "verdict": "uncertain",
                        "progress": "not_applicable" if prior_evidence is None else "unchanged",
                        "summary": "visual feedback did not produce a valid bounded decision",
                        "failure_category": type(error).__name__,
                    }
                )
        evidence_path = self.run_directory / f"{stem}.json"
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verdict = str(evidence["verdict"])
        result: dict[str, object] = {
            "status": {
                "pass": "success",
                "fail": "failed",
                "uncertain": "inconclusive",
            }[verdict],
            "verdict": verdict,
            "progress_status": str(evidence["progress"]),
            "grounded_objects": list(evidence["grounded_objects"]),
            "structured_assertions": structured_state["assertions"],
            "summary": str(evidence["summary"]),
            "diagnostics": [str(evidence["summary"])] if verdict == "fail" else [],
            "evidence_path": evidence_path.name,
            "screenshot_path": screenshot_path.name if screenshot_path.is_file() else None,
            "capture_evidence": _bounded_capture_evidence(capture),
            "_model_image_paths": (
                [str(screenshot_path.resolve(strict=True))] if screenshot_path.is_file() else []
            ),
        }
        if response is not None:
            result["_harness_usage"] = {
                "provider": response.provider,
                "model": response.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cached_input_tokens": response.usage.cached_input_tokens,
                "reasoning_output_tokens": response.usage.reasoning_output_tokens,
                "cost_usd": response.usage.cost_usd,
            }
        return result

    def _capture_scene_evidence(self) -> dict[str, object]:
        attempt = len(tuple(self.run_directory.glob("scene-evidence-*.json"))) + 1
        stem = f"scene-evidence-{attempt:02d}"
        screenshot_path = self.run_directory / f"{stem}.png"
        log_path = self.run_directory / f"{stem}.log"
        capture = _capture_fixed_camera(self, screenshot_path, log_path)
        bounded = _bounded_capture_evidence(capture)
        evidence_contract = {
            "state_phase": "runtime_after_scene_scripts_and_settle_frames",
            "certifies_serialized_resource_identity": False,
            "certifies_all_animation_frames": False,
            "interpretation": (
                "The screenshot and node metadata describe live runtime state after scene "
                "scripts have run. Texture and animation provenance is reported when the "
                "engine exposes it, but runtime appearance cannot by itself prove that named "
                "assets, resource wrappers, or every animation frame were serialized as "
                "requested."
            ),
        }
        evidence = {
            "schema_version": 1,
            "phase": "primary-model-current-revision-evidence",
            "advisory_only": True,
            "contains_verdict": False,
            "screenshot_path": screenshot_path.name if screenshot_path.is_file() else None,
            "capture_evidence": bounded,
            "evidence_contract": evidence_contract,
            "public_visual_asset_paths": list(self.visual_relative_paths),
        }
        evidence_path = self.run_directory / f"{stem}.json"
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "status": str(capture.get("status", "failed")),
            "screenshot_path": screenshot_path.name if screenshot_path.is_file() else None,
            "evidence_path": evidence_path.name,
            "capture_evidence": bounded,
            "evidence_contract": evidence_contract,
            "public_visual_asset_paths": list(self.visual_relative_paths),
            "guidance": (
                "The current frame is attached directly to the next primary-model turn. "
                "Interpret it with the supplied fidelity, runtime-state, coordinate, texture, "
                "and animation-frame provenance metadata. A live frame is not proof of persisted "
                "resource identity or unshown animation frames; inspect serialized state or "
                "construct a validator when those public requirements matter."
            ),
            "_model_image_paths": (
                [str(screenshot_path.resolve(strict=True))] if screenshot_path.is_file() else []
            ),
        }

    def _public_structured_state(self) -> dict[str, object]:
        resources: list[dict[str, object]] = []
        assertions: list[dict[str, object]] = []
        for raw_path in self.editable_paths:
            relative = _safe_relative(raw_path)
            if relative.suffix.lower() not in {".tscn", ".tres"}:
                continue
            target = self.project / relative
            if not target.is_file() or target.is_symlink():
                continue
            try:
                snapshot = GodotResourceDocument.parse(target.read_text(encoding="utf-8")).snapshot(
                    path=relative.as_posix(), maximum_sections=64
                )
            except (OSError, UnicodeDecodeError, ValueError) as error:
                assertions.append(
                    {
                        "id": f"{relative.as_posix()}:parse",
                        "passed": False,
                        "detail": type(error).__name__,
                    }
                )
                continue
            objects = [
                {
                    "section": section["section"],
                    "attributes": section["attributes"],
                    "properties": dict(list(section["properties"].items())[:20]),
                }
                for section in snapshot["sections"][:32]
            ]
            resources.append(
                {
                    "path": snapshot["path"],
                    "sha256": snapshot["sha256"],
                    "section_count": snapshot["section_count"],
                    "objects": objects,
                }
            )
            for assertion in snapshot["assertions"]:
                assertions.append(
                    {
                        **assertion,
                        "id": f"{relative.as_posix()}:{assertion['id']}",
                    }
                )
            if len(resources) == 8:
                break
        canonical = json.dumps(resources, sort_keys=True, separators=(",", ":"))
        return {
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "resources": resources,
            "assertions": assertions,
            "all_assertions_passed": all(bool(assertion.get("passed")) for assertion in assertions),
        }


@dataclass(frozen=True)
class GameDevBenchOfficialEvaluator:
    source: GameDevBenchSource
    godot: Path
    run_directory: Path
    timeout_seconds: float = 600
    name: str = "gamedevbench-official"
    version: str = GAMEDEVBENCH_VERSION

    def evaluate(self, run_spec: object, workspace: Path) -> tuple[GateOutcome, ...]:
        required = run_spec.evaluation.required_gates  # type: ignore[attr-defined]
        outcomes: list[GateOutcome] = []
        for gate in required:
            if gate == "specification":
                outcomes.append(
                    GateOutcome(
                        gate=gate,
                        status=GateStatus.PASS,
                        detail="normalized task and pinned benchmark source are valid",
                    )
                )
            elif gate == "official":
                outcomes.append(self._official(workspace))
            else:
                outcomes.append(
                    GateOutcome(
                        gate=gate,
                        status=GateStatus.NOT_RUN,
                        detail="unsupported GameDevBench Gate",
                    )
                )
        return tuple(outcomes)

    def _official(self, workspace: Path) -> GateOutcome:
        self.run_directory.mkdir(parents=True, exist_ok=True)
        log_path = self.run_directory / "official-evaluator.log"
        import_engine_log = self.run_directory / "official-import-engine.log"
        test_engine_log = self.run_directory / "official-test-engine.log"
        with _isolated_godot_project(workspace) as sandbox:
            validation = sandbox.project
            self.source.add_hidden_tests(validation)
            imported = _run_bounded_process(
                [
                    str(self.godot),
                    "--headless",
                    "--import",
                    "--quit",
                    "--path",
                    str(validation),
                    "--log-file",
                    str(import_engine_log),
                ],
                timeout_seconds=self.timeout_seconds,
            )
            import_output = imported.stdout + imported.stderr
            if _candidate_introduced_fatal_import_error(
                self.source,
                workspace,
                import_output,
            ):
                skipped = (
                    "VALIDATION_FAILED: candidate introduced a fatal Godot import "
                    "diagnostic; hidden test was not started\n"
                )
                test_engine_log.write_text(skipped, encoding="utf-8")
                tested = subprocess.CompletedProcess(
                    [str(self.godot), "res://scenes/test.tscn"],
                    1,
                    skipped,
                    "",
                )
            else:
                tested = _run_bounded_process(
                    [
                        str(self.godot),
                        "--headless",
                        "--path",
                        str(validation),
                        "--log-file",
                        str(test_engine_log),
                        "res://scenes/test.tscn",
                    ],
                    timeout_seconds=self.timeout_seconds,
                )
        output = (
            "[import]\n"
            + imported.stdout
            + imported.stderr
            + "\n[official-test]\n"
            + tested.stdout
            + tested.stderr
        )
        log_path.write_text(output, encoding="utf-8")
        timed_out = imported.returncode == 124 or tested.returncode == 124
        status = _official_gate_status(output, timed_out=timed_out)
        passed = status is GateStatus.PASS
        return GateOutcome(
            gate="official",
            status=status,
            detail=(
                "pinned official GameDevBench validator passed"
                if passed
                else "pinned official validator timed out without a verdict or error"
                if status is GateStatus.NOT_RUN
                else "pinned official GameDevBench validator did not pass"
            ),
            artifacts=(str(log_path),),
        )


def _official_gate_status(output: str, *, timed_out: bool) -> GateStatus:
    """Retain conclusive validator evidence even when its process later times out."""
    if _OFFICIAL_EXPLICIT_FAILURE in output:
        return GateStatus.FAIL
    if _OFFICIAL_EXPLICIT_SUCCESS in output:
        return GateStatus.PASS
    if any(pattern in output for pattern in _OFFICIAL_FALLBACK_FAILURE_PATTERNS):
        return GateStatus.FAIL
    return GateStatus.NOT_RUN if timed_out else GateStatus.FAIL


_RES_PATH_IN_DIAGNOSTIC = re.compile(r"res://(?P<path>[^\s:()]+)")
_FATAL_IMPORT_DIAGNOSTICS = (
    "SCRIPT ERROR: Parse Error:",
    "Failed to load script",
    "Failed loading resource:",
    "Error loading resource:",
    "Resource file not found:",
    "Invalid scene:",
)


def _candidate_introduced_fatal_import_error(
    source: GameDevBenchSource,
    workspace: Path,
    output: str,
) -> bool:
    """Stop before a hanging hidden test only for fatal errors in changed public files."""

    if not any(marker in output for marker in _FATAL_IMPORT_DIAGNOSTICS):
        return False
    paths = {
        match.group("path")
        for match in _RES_PATH_IN_DIAGNOSTIC.finditer(output)
        if not _is_hidden_benchmark_file(PurePosixPath(match.group("path")))
    }
    for relative in paths:
        candidate = workspace / Path(relative)
        candidate_bytes = candidate.read_bytes() if candidate.is_file() else None
        if candidate_bytes != source.public_file_bytes(relative):
            return True
    return False


@dataclass(frozen=True)
class GameDevBenchHarnessRun:
    directory: Path
    workspace: Path
    harness_run: HarnessRun


def run_gamedevbench_minimal_harness(
    *,
    root: Path,
    benchmark_root: Path,
    task_id: str,
    agent_executable: Path,
    model_name: str,
    reasoning_effort: str | None = None,
    input_usd_per_million: float = 0.0,
    cached_input_usd_per_million: float = 0.0,
    output_usd_per_million: float = 0.0,
    execution_profile: ExecutionProfile = ExecutionProfile.NATIVE_OPEN,
) -> GameDevBenchHarnessRun:
    """Give one native agent the complete public project, then run only Official."""

    fingerprint_payload = {
        "provider": "agent-cli-workspace",
        "model": model_name,
        "benchmark_commit": GAMEDEVBENCH_COMMIT,
        "harness_protocol": RuntimeProtocol.MINIMAL_OPEN_V1.value,
        "workspace_authority": "all-public-project-files",
        "intermediate_harness_tools": False,
        "intermediate_validation": False,
        "visual_attachments": False,
        "sampling": "provider-default",
        "reasoning_effort": reasoning_effort,
        "timeout_seconds": GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
        "execution_profile": execution_profile.value,
    }

    def model_factory(
        runtime: GodotRuntimeSandbox,
        tools: Path,
    ) -> NativeWorkspaceModel:
        del runtime, tools
        return AgentCliLanguageModel(
            agent_executable,
            model_name,
            timeout_seconds=GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
            input_usd_per_million=input_usd_per_million,
            cached_input_usd_per_million=cached_input_usd_per_million,
            output_usd_per_million=output_usd_per_million,
            reasoning_effort=reasoning_effort,
            workspace_sandbox=codex_sandbox_for(execution_profile),
        )

    return _run_gamedevbench_minimal_harness(
        root=root,
        benchmark_root=benchmark_root,
        task_id=task_id,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        solver_provider="agent-cli-workspace",
        solver_profile="minimal-open",
        fingerprint_payload=fingerprint_payload,
        model_factory=model_factory,
        execution_profile=execution_profile,
    )


def run_gamedevbench_direct_api_minimal_harness(
    *,
    root: Path,
    benchmark_root: Path,
    task_id: str,
    model_profile: ModelProfile,
) -> GameDevBenchHarnessRun:
    """Run the same minimal-open contract through the host-owned Responses API loop."""

    if model_profile.provider not in {
        ModelProvider.OPENAI_RESPONSES_WORKSPACE,
        ModelProvider.OPENAI_RESPONSES_API,
    }:
        raise ValueError("Direct API GameDevBench runs require provider=openai-responses-api")
    return _run_gamedevbench_model_profile_minimal_harness(
        root=root,
        benchmark_root=benchmark_root,
        task_id=task_id,
        model_profile=model_profile,
        solver_profile="direct-api-minimal-open",
    )


def run_gamedevbench_profile_minimal_harness(
    *,
    root: Path,
    benchmark_root: Path,
    task_id: str,
    model_profile: ModelProfile,
) -> GameDevBenchHarnessRun:
    """Run one minimal-open task with either supported release solver backend."""

    if model_profile.provider not in {
        ModelProvider.AGENT_CLI,
        ModelProvider.CODEX_SUBSCRIPTION,
        ModelProvider.OPENAI_RESPONSES_WORKSPACE,
        ModelProvider.OPENAI_RESPONSES_API,
    }:
        raise ValueError(
            "minimal workspace solver requires codex-subscription or openai-responses-api"
        )
    return _run_gamedevbench_model_profile_minimal_harness(
        root=root,
        benchmark_root=benchmark_root,
        task_id=task_id,
        model_profile=model_profile,
        solver_profile=(f"{model_profile.provider.value}-{model_profile.execution_profile.value}"),
    )


def _run_gamedevbench_model_profile_minimal_harness(
    *,
    root: Path,
    benchmark_root: Path,
    task_id: str,
    model_profile: ModelProfile,
    solver_profile: str,
) -> GameDevBenchHarnessRun:
    fingerprint_payload = {
        "provider": model_profile.provider.value,
        "model": model_profile.model,
        "benchmark_commit": GAMEDEVBENCH_COMMIT,
        "harness_protocol": RuntimeProtocol.MINIMAL_OPEN_V1.value,
        "workspace_authority": "all-public-project-files",
        "intermediate_harness_tools": False,
        "intermediate_validation": False,
        "visual_attachments": False,
        "generic_tools": (
            ["shell", "view_image"]
            if model_profile.provider
            in {
                ModelProvider.OPENAI_RESPONSES_WORKSPACE,
                ModelProvider.OPENAI_RESPONSES_API,
            }
            else ["codex-native-tools"]
        ),
        "tool_network": (
            "host-default"
            if model_profile.execution_profile is ExecutionProfile.NATIVE_OPEN
            else "denied"
        ),
        "tool_write_scope": (
            "host-native"
            if model_profile.execution_profile is ExecutionProfile.NATIVE_OPEN
            else "workspace-and-private-scratch"
        ),
        "sampling": "provider-default",
        "reasoning_effort": model_profile.reasoning_effort,
        "request_timeout_seconds": model_profile.timeout_seconds,
        "wall_timeout_seconds": GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
        "max_output_tokens": model_profile.max_output_tokens,
        "profile_sha256": model_profile.fingerprint(),
        "execution_profile": model_profile.execution_profile.value,
    }

    def model_factory(
        runtime: GodotRuntimeSandbox,
        tools: Path,
    ) -> NativeWorkspaceModel:
        adapter = model_profile.build_adapter(
            root,
            trusted_read_roots=(
                Path(__file__).resolve().parents[2],
                Path(sys.executable).resolve(strict=True).parents[1],
            ),
            host_runtime_roots=(runtime.root, tools),
            host_runtime_files=(_GODOT_PROCESS_LOCK,),
        )
        return cast(NativeWorkspaceModel, adapter)

    return _run_gamedevbench_minimal_harness(
        root=root,
        benchmark_root=benchmark_root,
        task_id=task_id,
        model_name=model_profile.model,
        reasoning_effort=model_profile.reasoning_effort,
        solver_provider=model_profile.provider.value,
        solver_profile=solver_profile,
        fingerprint_payload=fingerprint_payload,
        model_factory=model_factory,
        execution_profile=model_profile.execution_profile,
    )


def _run_gamedevbench_minimal_harness(
    *,
    root: Path,
    benchmark_root: Path,
    task_id: str,
    model_name: str,
    reasoning_effort: str | None,
    solver_provider: str,
    solver_profile: str,
    fingerprint_payload: dict[str, object],
    model_factory: Callable[[GodotRuntimeSandbox, Path], NativeWorkspaceModel],
    execution_profile: ExecutionProfile,
) -> GameDevBenchHarnessRun:

    source = GameDevBenchSource(benchmark_root, task_id, ())
    source.validate()
    placeholder = benchmark_root.resolve() / "tasks" / task_id
    task = source.normalized_task(placeholder)
    engine_version = _godot_version()
    evaluation = EvaluationSpec(
        required_gates=("specification", "official"),
        official_evaluator=True,
        evaluator_version=GAMEDEVBENCH_VERSION,
    )
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="agent-cli-placeholder"),
        engine=EngineName.GODOT,
        engine_version=engine_version,
        profile=HarnessProfile.OFFICIAL_COMPATIBLE,
        game_task=_minimal_gamedevbench_game_task(),
        engine_environment=EngineEnvironment(
            engine_version=engine_version,
            target_platform="macos-headless",
            graphics_backend="headless",
        ),
        runtime_protocol=RuntimeProtocol.MINIMAL_OPEN_V1,
        budgets=RunBudgets(
            wall_seconds=1800,
            max_turns=1,
            max_tool_calls=1000,
            max_repairs=0,
            max_cost_usd=20,
        ),
        evaluation=evaluation,
    )
    output_root = root / "runs" / "gamedevbench-minimal"
    workspace = root / "runs" / "gamedevbench-workspaces" / run_spec.run_id
    source.prepare_workspace(workspace)
    initial_snapshot = snapshot_project(workspace)
    isolated_task = run_spec.task.model_copy(
        update={
            "project_path": workspace,
            "editable_paths": tuple(sorted(initial_snapshot.files)),
            "metadata": {
                **run_spec.task.metadata,
                "workspace_authority": "all-public-project-files",
                "workflow": "one-open-native-session",
            },
        }
    )
    canonical = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"))
    isolated_spec = run_spec.model_copy(
        update={
            "task": isolated_task,
            "model": ModelRunSpec(
                provider=solver_provider,
                model=model_name,
                reasoning_effort=reasoning_effort,
                parameters=fingerprint_payload,
                profile_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            ),
        }
    )
    run_directory = output_root / isolated_spec.run_id
    godot = Path(shutil.which("godot") or "")
    if not godot.is_file():
        raise ValueError("godot executable is not installed")
    prompt = _minimal_open_prompt(isolated_task.instruction)
    with (
        _isolated_godot_runtime(godot) as runtime,
        tempfile.TemporaryDirectory(prefix="gameforge-minimal-tools-") as tools_directory,
    ):
        tools = Path(tools_directory)
        private_home = tools / "home"
        private_home.mkdir()
        _GODOT_PROCESS_LOCK.touch(exist_ok=True)
        if _GODOT_PROCESS_LOCK.is_symlink() or not _GODOT_PROCESS_LOCK.is_file():
            raise OSError("Godot process lock must be a regular host-managed file")
        wrapper = tools / "godot"
        process_mutex_script = Path(__file__).resolve().parents[1] / "harness" / "process_mutex.py"
        if solver_provider in {
            ModelProvider.OPENAI_RESPONSES_WORKSPACE.value,
            ModelProvider.OPENAI_RESPONSES_API.value,
        }:
            mutex_invocation = (
                f"{shlex.quote(str(Path(sys.executable).resolve(strict=True)))} "
                f"{shlex.quote(str(process_mutex_script))}"
            )
        else:
            # Preserve the frozen baseline's native-agent transport byte for byte.
            mutex_invocation = f"{shlex.quote(sys.executable)} -m gameforge.harness.process_mutex"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"export HOME={shlex.quote(str(private_home))}\n"
            f"export CFFIXED_USER_HOME={shlex.quote(str(private_home))}\n"
            f"exec {mutex_invocation} "
            f"--lock-file {shlex.quote(str(_GODOT_PROCESS_LOCK))} "
            f"--executable {shlex.quote(str(runtime.executable))} "
            f"--crash-attempts {_GODOT_PROCESS_ATTEMPTS} "
            f'--private-home-root {shlex.quote(str(private_home))} -- "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        model = model_factory(runtime, tools)
        evaluator = GameDevBenchOfficialEvaluator(source, runtime.executable, run_directory)
        model_environment = login_shell_tool_environment(
            tools,
            exported_variables={
                "GODOT": str(wrapper),
                "GAMEFORGE_MINIMAL_OPEN": "1",
                "GIT_CEILING_DIRECTORIES": str(workspace.parent),
            },
        )
        harness_run = MinimalWorkspaceHarnessRunner(
            model=model,
            evaluator=evaluator,
            prompt=prompt,
            timeout_seconds=GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
            environment_overrides=model_environment,
        ).execute(isolated_spec, output_root)
        (harness_run.directory / "godot-runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": runtime.mode,
                    "source_executable": str(runtime.source_executable),
                    "engine_version": engine_version,
                    "self_contained": runtime.mode != "system",
                    "editor_data_isolated": runtime.mode != "system",
                    "model_engine_access": (
                        "unrestricted-arguments-through-cross-task-process-mutex"
                    ),
                    "model_command_resolution": ("task-owned-login-shell-environment"),
                    "model_execution_profile": execution_profile.value,
                    "model_wrapper": str(wrapper),
                    "macos_state_restoration": "private-CFFIXED_USER_HOME",
                    "project_validation_copy": "per-invocation-temporary",
                    "validation_editor_plugins": "disabled-in-temporary-copy",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    metadata = {
        "schema_version": 1,
        "benchmark": "GameDevBench",
        "benchmark_commit": GAMEDEVBENCH_COMMIT,
        "task_id": task_id,
        "profile": solver_profile,
        "solver_provider": solver_provider,
        "runtime_protocol": RuntimeProtocol.MINIMAL_OPEN_V1.value,
        "model": model_name,
        "reasoning_effort": reasoning_effort,
        "workspace_authority": "all-public-project-files",
        "intermediate_harness_tools": False,
        "intermediate_validation": False,
        "visual_attachments": False,
        "status": harness_run.result.status.value,
        "official_score": harness_run.result.status is RunStatus.PASS,
        "input_tokens": harness_run.result.input_tokens,
        "output_tokens": harness_run.result.output_tokens,
        "cached_input_tokens": harness_run.result.cached_input_tokens,
        "reasoning_output_tokens": harness_run.result.reasoning_output_tokens,
        "cost_usd": harness_run.result.cost_usd,
        "created_at": datetime.now(UTC).isoformat(),
    }
    (harness_run.directory / "benchmark-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    updated_result = harness_run.result.model_copy(
        update={"artifacts": HarnessRunner._artifact_manifest(harness_run.directory)}
    )
    HarnessRunner._write_json(
        harness_run.directory / "result.json", updated_result.model_dump(mode="json")
    )
    return GameDevBenchHarnessRun(
        harness_run.directory,
        workspace,
        HarnessRun(harness_run.directory, updated_result),
    )


def _minimal_open_prompt(instruction: str) -> str:
    return (
        "Implement the following request in the current disposable copy of the complete public "
        "Godot project. Work freely and use any available local tools as you see fit; an isolated "
        "Godot executable is available as `godot`.\n\n"
        f"REQUEST:\n{instruction}"
    )


def _minimal_gamedevbench_game_task() -> GameTaskSpec:
    return GameTaskSpec(
        engine_profile="godot-headless-gamedevbench-minimal-open",
        entry_points=("project.godot",),
        target_platforms=("macos-headless",),
        requirements=(
            AcceptanceRequirement(
                id="pinned-source",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="specification",
                model_visible=False,
            ),
            AcceptanceRequirement(
                id="official-behavior",
                dimension=AcceptanceDimension.BEHAVIOR,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="official",
                model_visible=False,
            ),
        ),
        evidence_policy=EvidencePolicy(maximum_observation_bytes=64 * 1024),
        asset_policy=AssetPolicy(
            allow_binary_mutation=True,
            require_provenance=False,
            allow_text_scope_expansion=True,
            allow_native_workspace_agent=True,
            maximum_writable_paths=10_000,
        ),
    )


def run_gamedevbench_harness(
    *,
    root: Path,
    benchmark_root: Path,
    task_id: str,
    editable_paths: tuple[str, ...],
    agent_executable: Path,
    model_name: str,
    reasoning_effort: str | None = None,
    input_usd_per_million: float = 0.0,
    cached_input_usd_per_million: float = 0.0,
    output_usd_per_million: float = 0.0,
    runtime_protocol: RuntimeProtocol = RuntimeProtocol.LEGACY_TOOL_V1,
) -> GameDevBenchHarnessRun:
    candidate_source = GameDevBenchSource(benchmark_root, task_id, editable_paths)
    candidate_source.validate()
    scope_strategy = "explicit" if editable_paths else "public-auto"
    resolved_editable_paths = editable_paths or candidate_source.suggested_editable_paths(
        allow_deferred_declaration=(runtime_protocol is RuntimeProtocol.PROGRAMMABLE_V1)
    )
    source = GameDevBenchSource(benchmark_root, task_id, resolved_editable_paths)
    placeholder = benchmark_root.resolve() / "tasks" / task_id
    task = source.normalized_task(placeholder)
    profile = ModelProfile(provider="mock", model="agent-cli-placeholder")
    engine_version = _godot_version()
    initial_budgets = RunBudgets(
        wall_seconds=1800,
        max_turns=8,
        max_tool_calls=16,
        max_repairs=1,
        max_cost_usd=20,
    )
    evaluation = EvaluationSpec(
        required_gates=("specification", "official", "preservation"),
        official_evaluator=True,
        evaluator_version=GAMEDEVBENCH_VERSION,
    )
    if runtime_protocol is RuntimeProtocol.PROGRAMMABLE_V1:
        run_spec = create_run_spec_v2(
            task,
            profile,
            engine=EngineName.GODOT,
            engine_version=engine_version,
            profile=HarnessProfile.AUGMENTED,
            game_task=_gamedevbench_game_task(),
            engine_environment=EngineEnvironment(
                engine_version=engine_version,
                target_platform="macos-headless",
                graphics_backend="headless",
            ),
            runtime_protocol=runtime_protocol,
            budgets=initial_budgets,
            evaluation=evaluation,
        )
    else:
        run_spec = create_run_spec(
            task,
            profile,
            engine=EngineName.GODOT,
            engine_version=engine_version,
            profile=HarnessProfile.AUGMENTED,
            budgets=initial_budgets,
            evaluation=evaluation,
        )
    output_root = root / "runs" / "gamedevbench-harness"
    workspace = root / "runs" / "gamedevbench-workspaces" / run_spec.run_id
    source.prepare_workspace(workspace)
    route = _route_task(
        workspace,
        task.instruction,
        resolved_editable_paths,
    )
    route_budgets = _route_budgets(route.name, runtime_protocol=runtime_protocol)
    maximum_repairs = route_budgets.max_repairs
    fingerprint_payload = json.dumps(
        {
            "provider": "agent-cli",
            "model": model_name,
            "benchmark_commit": GAMEDEVBENCH_COMMIT,
            "harness_profile": "augmented",
            "runtime_protocol": runtime_protocol.value,
            "scope_strategy": scope_strategy,
            "editable_paths": list(resolved_editable_paths),
            "complexity_route": route.name,
            "visual_asset_paths": list(route.visual_relative_paths),
            "public_asset_paths": [asset.path for asset in route.asset_manifest],
            "visual_context": _ATLAS_GRID_CONTEXT_VERSION,
            "public_validation": "godot-import-resource-load-startup-v7-relative-baseline",
            "completion_feedback": (
                "fixed-camera-object-grounded-progress-v4" if route.name == "visual" else "none"
            ),
            "max_validation_repairs": maximum_repairs,
            "sampling": "provider-default",
            "reasoning_effort": reasoning_effort,
            "timeout_seconds": GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
        },
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    run_spec = run_spec.model_copy(
        update={
            "model": ModelRunSpec(
                provider="agent-cli",
                model=model_name,
                parameters={
                    "complexity_route": route.name,
                    "max_validation_repairs": maximum_repairs,
                    "public_validation": (
                        "godot-import-resource-load-startup-v6-relative-baseline"
                    ),
                    "sampling": "provider-default",
                    "reasoning_effort": reasoning_effort,
                    "timeout_seconds": GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
                    "tool_protocol": "typed-json-native-objects-v4",
                    "runtime_protocol": runtime_protocol.value,
                    "scope_strategy": scope_strategy,
                    "visual_asset_count": len(route.visual_paths),
                    "visual_context": _ATLAS_GRID_CONTEXT_VERSION,
                    "visual_gate": (
                        "fixed-camera-object-grounded-advisory-v4"
                        if route.name == "visual"
                        else "not-applicable"
                    ),
                },
                profile_sha256=fingerprint,
            ),
            "budgets": route_budgets,
        }
    )

    isolated_task = run_spec.task.model_copy(
        update={
            "project_path": workspace,
            "metadata": {
                **run_spec.task.metadata,
                "complexity_route": route.name,
                "visual_asset_count": len(route.visual_paths),
                "visual_asset_paths": ",".join(route.visual_relative_paths),
                "public_asset_count": len(route.asset_manifest),
            },
        }
    )
    isolated_spec = run_spec.model_copy(update={"task": isolated_task})
    run_directory = output_root / isolated_spec.run_id
    visual_context_staging = (
        output_root.parent / ".gamedevbench-visual-context" / isolated_spec.run_id
    )
    model_visual_context = _build_model_visual_context(
        workspace,
        route,
        resolved_editable_paths,
        visual_context_staging,
    )
    isolated_task = isolated_spec.task.model_copy(
        update={
            "metadata": {
                **isolated_spec.task.metadata,
                "model_visual_context": json.dumps(
                    model_visual_context.records,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        }
    )
    isolated_spec = isolated_spec.model_copy(update={"task": isolated_task})
    godot = Path(shutil.which("godot") or "")
    if not godot.is_file():
        raise ValueError("godot executable is not installed")
    with _isolated_godot_runtime(godot) as runtime:
        engine = GodotHarnessEngineAdapter(
            workspace,
            run_directory,
            resolved_editable_paths,
            runtime.executable,
            task_instruction=isolated_task.instruction,
            visual_paths=route.visual_paths,
            visual_relative_paths=route.visual_relative_paths,
            visual_asset_manifest=route.asset_manifest,
            agent_executable=agent_executable if route.name == "visual" else None,
            model_name=model_name if route.name == "visual" else None,
            reasoning_effort=reasoning_effort,
            input_usd_per_million=input_usd_per_million,
            cached_input_usd_per_million=cached_input_usd_per_million,
            output_usd_per_million=output_usd_per_million,
        )
        evaluator = GameDevBenchOfficialEvaluator(
            source,
            runtime.executable,
            run_directory,
        )
        model = AgentCliLanguageModel(
            agent_executable,
            model_name,
            timeout_seconds=GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
            input_usd_per_million=input_usd_per_million,
            cached_input_usd_per_million=cached_input_usd_per_million,
            output_usd_per_million=output_usd_per_million,
            reasoning_effort=reasoning_effort,
            image_paths=model_visual_context.paths,
        )
        harness_run = HarnessRunner(model, engine, evaluator).execute(
            isolated_spec,
            output_root,
        )
        _retain_model_visual_context(
            model_visual_context,
            visual_context_staging,
            harness_run.directory,
        )
        if visual_context_staging.is_dir():
            shutil.rmtree(visual_context_staging)
        if route.name == "visual":
            harness_run = _add_visual_review(
                harness_run=harness_run,
                engine=engine,
                task=isolated_task,
                route=route,
                agent_executable=agent_executable,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                input_usd_per_million=input_usd_per_million,
                cached_input_usd_per_million=cached_input_usd_per_million,
                output_usd_per_million=output_usd_per_million,
            )
        (harness_run.directory / "godot-runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": runtime.mode,
                    "source_executable": str(runtime.source_executable),
                    "engine_version": engine_version,
                    "self_contained": runtime.mode != "system",
                    "editor_data_isolated": runtime.mode != "system",
                    "project_validation_copy": "per-invocation-temporary",
                    "validation_editor_plugins": "disabled-in-temporary-copy",
                    "project_user_data": "host-path-failures-are-infrastructure",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    comparison_metadata = {
        "benchmark": "GameDevBench",
        "benchmark_commit": GAMEDEVBENCH_COMMIT,
        "task_id": task_id,
        "profile": "augmented",
        "runtime_protocol": runtime_protocol.value,
        "model": model_name,
        "reasoning_effort": reasoning_effort,
        "scope_strategy": scope_strategy,
        "editable_paths": list(resolved_editable_paths),
        "complexity_route": route.name,
        "public_validation": "godot-import-resource-load-startup-v7-relative-baseline",
        "completion_feedback": (
            "fixed-camera-object-grounded-progress-v4" if route.name == "visual" else "none"
        ),
        "max_validation_repairs": maximum_repairs,
        "visual_asset_paths": list(route.visual_relative_paths),
        "model_visual_context": list(model_visual_context.records),
        "public_asset_count": len(route.asset_manifest),
        "visual_gate": next(
            (
                gate.status.value
                for gate in harness_run.result.gates
                if gate.gate == "visual_auxiliary"
            ),
            "NOT_APPLICABLE",
        ),
        "status": harness_run.result.status.value,
        "official_score": harness_run.result.status is RunStatus.PASS,
        "input_tokens": harness_run.result.input_tokens,
        "output_tokens": harness_run.result.output_tokens,
        "cached_input_tokens": harness_run.result.cached_input_tokens,
        "reasoning_output_tokens": harness_run.result.reasoning_output_tokens,
        "cost_usd": harness_run.result.cost_usd,
        "created_at": datetime.now(UTC).isoformat(),
    }
    (harness_run.directory / "benchmark-metadata.json").write_text(
        json.dumps(comparison_metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (harness_run.directory / "public-asset-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": [asset.model_dump(mode="json") for asset in route.asset_manifest],
                "derived_context": list(model_visual_context.records),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    updated_result = harness_run.result.model_copy(
        update={"artifacts": HarnessRunner._artifact_manifest(harness_run.directory)}
    )
    (harness_run.directory / "result.json").write_text(
        json.dumps(updated_result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    harness_run = HarnessRun(harness_run.directory, updated_result)
    return GameDevBenchHarnessRun(harness_run.directory, workspace, harness_run)


def _gamedevbench_game_task() -> GameTaskSpec:
    """Describe public authoring checks and hidden official evidence independently."""
    return GameTaskSpec(
        engine_profile="godot-headless-gamedevbench",
        entry_points=("project.godot",),
        target_platforms=("macos-headless",),
        requirements=(
            AcceptanceRequirement(
                id="public-import",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="import_project",
                model_visible=True,
            ),
            AcceptanceRequirement(
                id="pinned-source",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="specification",
                model_visible=False,
            ),
            AcceptanceRequirement(
                id="official-behavior",
                dimension=AcceptanceDimension.BEHAVIOR,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="official",
                model_visible=False,
            ),
            AcceptanceRequirement(
                id="project-preservation",
                dimension=AcceptanceDimension.PRESERVATION,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="preservation",
                model_visible=False,
            ),
        ),
        evidence_policy=EvidencePolicy(maximum_observation_bytes=256 * 1024),
        asset_policy=AssetPolicy(
            allow_binary_mutation=True,
            allow_text_scope_expansion=True,
            allow_native_workspace_agent=True,
            maximum_writable_paths=64,
        ),
    )


@contextmanager
def _isolated_godot_runtime(godot: Path) -> Iterator[GodotRuntimeSandbox]:
    """Give macOS Godot a private writable editor/cache hierarchy.

    Godot's self-contained marker must live next to the app bundle on macOS. The
    signed bundle itself remains unchanged, while every Harness run receives an
    independent ``editor_data`` directory. Project caches are isolated separately;
    failures involving the OS-managed ``user://`` root are quarantined as host errors.
    """

    source = godot.resolve(strict=True)
    if sys.platform != "darwin":
        yield GodotRuntimeSandbox(source, source, source.parent, "system")
        return

    with tempfile.TemporaryDirectory(prefix="gameforge-godot-runtime-") as directory:
        root = Path(directory)
        source_app = next((parent for parent in source.parents if parent.suffix == ".app"), None)
        if source_app is not None:
            destination_app = root / source_app.name
            cloned = subprocess.run(
                ["cp", "-cR", str(source_app), str(destination_app)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if cloned.returncode != 0:
                if destination_app.exists():
                    shutil.rmtree(destination_app)
                copied = subprocess.run(
                    ["cp", "-R", str(source_app), str(destination_app)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if copied.returncode != 0:
                    detail = (copied.stderr or cloned.stderr).strip()
                    raise OSError(f"cannot prepare isolated Godot app bundle: {detail}")
            relative_executable = source.relative_to(source_app)
            executable = destination_app / relative_executable
            mode = "macos-self-contained-app-clone"
        else:
            executable = root / source.name
            shutil.copy2(source, executable)
            mode = "macos-self-contained-binary-copy"

        (root / "._sc_").touch()
        (root / "editor_data").mkdir()
        (root / "home").mkdir()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise OSError(f"isolated Godot executable is unavailable: {executable}")
        yield GodotRuntimeSandbox(executable, source, root, mode)


@contextmanager
def _isolated_godot_project(source: Path) -> Iterator[GodotProjectSandbox]:
    """Run engine checks without mutating the candidate or sharing project state."""

    with tempfile.TemporaryDirectory(prefix="gameforge-godot-project-") as directory:
        root = Path(directory)
        project = root / "project"
        _copy_without_engine_cache(source, project)
        project_file = project / "project.godot"
        if not project_file.is_file():
            raise ValueError(f"Godot validation copy has no project.godot: {source}")
        _disable_editor_plugins(project_file)
        yield GodotProjectSandbox(project)


def _disable_editor_plugins(project_file: Path) -> None:
    """Disable EditorPlugins only in an ephemeral validation project copy."""

    try:
        content = project_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Godot project file is not UTF-8: {project_file}") from error
    newline = "\r\n" if "\r\n" in content else "\n"
    disabled = f"enabled=PackedStringArray(){newline}"
    output: list[str] = []
    in_editor_plugins = False
    found_editor_plugins = False
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_editor_plugins = stripped == "[editor_plugins]"
            output.append(line)
            if in_editor_plugins:
                found_editor_plugins = True
                output.append(disabled)
            continue
        if in_editor_plugins and re.match(r"^\s*enabled\s*=", line):
            continue
        output.append(line)
    if not found_editor_plugins:
        if output and not output[-1].endswith(("\n", "\r")):
            output.append(newline)
        if output and output[-1].strip():
            output.append(newline)
        output.extend((f"[editor_plugins]{newline}", disabled))
    project_file.write_text("".join(output), encoding="utf-8", newline="")


def _godot_version(godot: Path | None = None) -> str:
    executable = godot or Path(shutil.which("godot") or "")
    if not executable.is_file():
        raise ValueError("godot executable is not installed")
    completed = _run_bounded_process(
        [str(executable), "--version"],
        timeout_seconds=10,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version:
        raise ValueError("cannot determine Godot version")
    return version


def _route_budgets(
    route: str,
    *,
    runtime_protocol: RuntimeProtocol = RuntimeProtocol.LEGACY_TOOL_V1,
) -> RunBudgets:
    if runtime_protocol is RuntimeProtocol.PROGRAMMABLE_V1:
        return RunBudgets(
            wall_seconds=1800,
            max_turns=40,
            max_tool_calls=80,
            max_repairs=2,
            max_cost_usd=20,
        )
    if route == "simple":
        return RunBudgets(
            wall_seconds=1800,
            max_turns=6,
            max_tool_calls=12,
            max_repairs=1,
            max_cost_usd=20,
        )
    if route == "visual":
        return RunBudgets(
            wall_seconds=1800,
            max_turns=12,
            max_tool_calls=28,
            max_repairs=2,
            max_cost_usd=20,
        )
    return RunBudgets(
        wall_seconds=1800,
        max_turns=12,
        max_tool_calls=24,
        max_repairs=2,
        max_cost_usd=20,
    )


def _route_task(
    workspace: Path,
    instruction: str,
    editable_paths: tuple[str, ...],
) -> GameDevBenchTaskRoute:
    (
        visual_paths,
        visual_relative_paths,
        asset_manifest,
        catalog_paths,
        catalog_relative_paths,
    ) = _discover_visual_assets(
        workspace,
        instruction,
        editable_paths,
    )
    lowered = instruction.lower()
    if visual_paths and any(term in lowered for term in _VISUAL_REASONING_TERMS):
        return GameDevBenchTaskRoute(
            "visual",
            visual_paths,
            visual_relative_paths,
            asset_manifest,
            catalog_paths,
            catalog_relative_paths,
        )
    interactive_terms = ("animation", "atlas", "state machine", "shader", "tile set", "tileset")
    if (
        len(editable_paths) >= 4
        or len(instruction) >= 1600
        or any(term in lowered for term in interactive_terms)
    ):
        return GameDevBenchTaskRoute(
            "interactive",
            visual_paths,
            visual_relative_paths,
            asset_manifest,
            catalog_paths,
            catalog_relative_paths,
        )
    return GameDevBenchTaskRoute(
        "simple",
        visual_paths,
        visual_relative_paths,
        asset_manifest,
        catalog_paths,
        catalog_relative_paths,
    )


def _discover_visual_assets(
    workspace: Path,
    instruction: str,
    editable_paths: tuple[str, ...],
) -> tuple[
    tuple[Path, ...],
    tuple[str, ...],
    tuple[PublicAssetRecord, ...],
    tuple[Path, ...],
    tuple[str, ...],
]:
    explicit_references: list[str] = []
    image_suffixes = "|".join(suffix.removeprefix(".") for suffix in sorted(_VISUAL_SUFFIXES))
    pattern = re.compile(rf"res://[A-Za-z0-9_./ -]+\.(?:{image_suffixes})", re.IGNORECASE)
    explicit_references.extend(
        match.removeprefix("res://") for match in pattern.findall(instruction)
    )
    context = build_project_context(workspace, editable_paths)
    context_assets = {asset.path: asset for asset in context.assets}
    lowered_instruction = instruction.lower()
    instruction_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", lowered_instruction)
        if len(token) >= 4
        and token
        not in {
            "asset",
            "assets",
            "image",
            "node",
            "scene",
            "sprite",
            "sprites",
            "texture",
        }
    }
    explicit_set = set(explicit_references)
    explicit_directories = {Path(path).parent.as_posix() for path in explicit_set}
    scored: list[tuple[int, str, Path, tuple[str, ...], tuple[str, ...]]] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in _VISUAL_SUFFIXES:
            continue
        relative = path.relative_to(workspace).as_posix()
        if any(part in IGNORED_DIRECTORY_NAMES for part in Path(relative).parts):
            continue
        if path.stat().st_size > _VISUAL_MAXIMUM_FILE_BYTES:
            continue
        context_asset = context_assets.get(relative)
        referenced_by = context_asset.referenced_by if context_asset is not None else ()
        relevance: list[str] = []
        score = 0
        if relative in explicit_set:
            score += 100
            relevance.append("explicit_res_path")
        file_name = path.name.lower()
        stem = path.stem.lower()
        if file_name in lowered_instruction or stem in lowered_instruction:
            score += 90
            relevance.append("named_in_instruction")
        if referenced_by:
            score += 60
            relevance.append("referenced_by_editable_text")
        path_tokens = {
            token for token in re.split(r"[^a-z0-9]+", relative.lower()) if len(token) >= 4
        }
        matched_tokens = sorted(path_tokens & instruction_tokens)
        if matched_tokens:
            score += 25 + 8 * len(matched_tokens)
            relevance.append("instruction_path_keyword:" + ",".join(matched_tokens))
        if path.parent.relative_to(workspace).as_posix() in explicit_directories:
            score += 10
            relevance.append("sibling_of_explicit_asset")
        if "shadow" not in instruction_tokens and "shadow" in path_tokens:
            score -= 35
            relevance.append("deprioritized_unrequested_shadow")
        scored.append(
            (
                score,
                relative,
                path,
                referenced_by,
                tuple(relevance),
            )
        )

    visual_task = any(term in lowered_instruction for term in _VISUAL_REASONING_TERMS)
    candidates = sorted(scored, key=lambda item: (-item[0], item[1]))
    families: dict[str, list[tuple[int, str, Path, tuple[str, ...], tuple[str, ...]]]] = {}
    for candidate in candidates:
        families.setdefault(_visual_family_key(candidate[1]), []).append(candidate)
    ranked_families = sorted(
        families.values(),
        key=lambda family: (-family[0][0], _visual_family_key(family[0][1])),
    )
    selected: list[tuple[str, Path]] = []
    for family in ranked_families:
        score, relative, path, _, _ = family[0]
        if score <= 0 and not visual_task:
            continue
        selected.append((relative, path))
        if len(selected) == _VISUAL_MAXIMUM_FILES:
            break
    catalog: list[tuple[str, Path]] = []
    for family in ranked_families[:_VISUAL_CATALOG_MAXIMUM_FAMILIES]:
        if family[0][0] <= 0 and not visual_task:
            continue
        for _, relative, path, _, _ in _visual_sequence_representatives(family):
            catalog.append((relative, path))
            if len(catalog) == _VISUAL_CATALOG_MAXIMUM_FILES:
                break
        if len(catalog) == _VISUAL_CATALOG_MAXIMUM_FILES:
            break
    selected_paths = {relative for relative, _ in selected}
    asset_manifest = tuple(
        PublicAssetRecord(
            path=relative,
            kind="image",
            bytes=path.stat().st_size,
            referenced_by=referenced_by,
            relevance=relevance,
            attached=relative in selected_paths,
        )
        for _, relative, path, referenced_by, relevance in candidates[
            :_VISUAL_ASSET_MANIFEST_MAXIMUM_FILES
        ]
    )
    return (
        tuple(path for _, path in selected),
        tuple(relative for relative, _ in selected),
        asset_manifest,
        tuple(path for _, path in catalog),
        tuple(relative for relative, _ in catalog),
    )


def _visual_family_key(relative_path: str) -> str:
    path = Path(relative_path)
    normalized_stem = re.sub(r"\d+$", "<sequence>", path.stem.lower())
    return f"{path.parent.as_posix().lower()}/{normalized_stem}{path.suffix.lower()}"


def _visual_sequence_representatives(
    family: list[tuple[int, str, Path, tuple[str, ...], tuple[str, ...]]],
) -> tuple[tuple[int, str, Path, tuple[str, ...], tuple[str, ...]], ...]:
    """Choose bounded representatives across fixed-width sequence/direction buckets."""
    by_bucket: dict[str, tuple[int, str, Path, tuple[str, ...], tuple[str, ...]]] = {}
    for candidate in family:
        match = re.search(r"(\d+)$", Path(candidate[1]).stem)
        digits = match.group(1) if match else ""
        bucket = digits[:-4] if len(digits) >= 5 else "sequence"
        by_bucket.setdefault(bucket, candidate)
    representatives = tuple(by_bucket[key] for key in sorted(by_bucket))
    if len(representatives) > 1:
        return representatives[:16]
    if len(family) <= 8:
        return tuple(family)
    indexes = {round(index * (len(family) - 1) / 7) for index in range(8)}
    return tuple(family[index] for index in sorted(indexes))


def _build_model_visual_context(
    workspace: Path,
    route: GameDevBenchTaskRoute,
    editable_paths: tuple[str, ...],
    run_directory: Path,
) -> ModelVisualContext:
    selected: list[Path] = []
    records: list[dict[str, object]] = []
    for image_path, relative_path in zip(
        route.visual_paths,
        route.visual_relative_paths,
        strict=True,
    ):
        geometry = _atlas_geometry(workspace, relative_path, editable_paths)
        selected_path = image_path
        if geometry is not None:
            context_directory = run_directory / "visual-context"
            digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:10]
            output = context_directory / f"{Path(relative_path).stem}-{digest}-atlas-grid.png"
            _render_atlas_grid(image_path, output, geometry)
            selected_path = output
            records.append(
                {
                    "kind": _ATLAS_GRID_CONTEXT_VERSION,
                    "source_path": relative_path,
                    "artifact_path": output.relative_to(run_directory).as_posix(),
                    "region_size": [geometry[0], geometry[1]],
                    "separation": [geometry[2], geometry[3]],
                    "margin": [geometry[4], geometry[5]],
                    "source_resource": geometry[6],
                }
            )
        selected.append(selected_path)
        if len(selected) == _MODEL_VISUAL_MAXIMUM_FILES:
            break
    remaining = _MODEL_VISUAL_MAXIMUM_FILES - len(selected)
    if remaining and len(route.catalog_paths) > 1:
        catalog_directory = run_directory / "visual-context"
        pairs = list(zip(route.catalog_paths, route.catalog_relative_paths, strict=True))
        for sheet_number, offset in enumerate(
            range(0, len(pairs), _VISUAL_CATALOG_SHEET_CAPACITY),
            start=1,
        ):
            if sheet_number > remaining:
                break
            chunk = pairs[offset : offset + _VISUAL_CATALOG_SHEET_CAPACITY]
            output = catalog_directory / f"visual-catalog-{sheet_number:02d}.png"
            _render_visual_catalog(
                tuple(path for path, _ in chunk),
                tuple(relative for _, relative in chunk),
                output,
            )
            selected.append(output)
            records.append(
                {
                    "kind": _VISUAL_CATALOG_CONTEXT_VERSION,
                    "artifact_path": output.relative_to(run_directory).as_posix(),
                    "source_count": len(chunk),
                    "source_paths": [relative for _, relative in chunk],
                }
            )
    return ModelVisualContext(tuple(selected), tuple(records))


def _retain_model_visual_context(
    context: ModelVisualContext,
    staging_directory: Path,
    run_directory: Path,
) -> None:
    for record in context.records:
        artifact_path = record.get("artifact_path")
        if not isinstance(artifact_path, str):
            continue
        source = staging_directory / artifact_path
        destination = run_directory / artifact_path
        if not source.is_file() or not source.is_relative_to(staging_directory):
            raise ValueError(f"derived visual context is missing or unsafe: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _atlas_geometry(
    workspace: Path,
    image_relative_path: str,
    editable_paths: tuple[str, ...],
) -> tuple[int, int, int, int, int, int, str] | None:
    reference = f"res://{image_relative_path}"
    for relative_path in sorted(editable_paths):
        resource = workspace / relative_path
        if resource.suffix.lower() not in {".tres", ".tscn"} or not resource.is_file():
            continue
        try:
            content = resource.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if reference not in content:
            continue
        region = re.search(
            r"^texture_region_size\s*=\s*Vector2i\((\d+)\s*,\s*(\d+)\)",
            content,
            re.MULTILINE,
        )
        if region is None:
            continue
        separation = re.search(
            r"^separation\s*=\s*Vector2i\((\d+)\s*,\s*(\d+)\)",
            content,
            re.MULTILINE,
        )
        margin = re.search(
            r"^margins?\s*=\s*Vector2i\((\d+)\s*,\s*(\d+)\)",
            content,
            re.MULTILINE,
        )
        region_width, region_height = (int(value) for value in region.groups())
        separation_x, separation_y = (
            (int(value) for value in separation.groups()) if separation else (0, 0)
        )
        margin_x, margin_y = (int(value) for value in margin.groups()) if margin else (0, 0)
        if region_width <= 0 or region_height <= 0:
            continue
        return (
            region_width,
            region_height,
            separation_x,
            separation_y,
            margin_x,
            margin_y,
            relative_path,
        )
    return None


def _render_atlas_grid(
    source_path: Path,
    output_path: Path,
    geometry: tuple[int, int, int, int, int, int, str],
) -> None:
    region_width, region_height, separation_x, separation_y, margin_x, margin_y, _ = geometry
    with Image.open(source_path) as opened:
        source = opened.convert("RGBA")
    scale = max(1, min(4, 1400 // max(source.width, source.height)))
    left = 54
    top = 52
    scaled = source.resize((source.width * scale, source.height * scale), Image.Resampling.NEAREST)
    canvas = Image.new(
        "RGBA",
        (left + scaled.width + 2, top + scaled.height + 2),
        (22, 25, 31, 255),
    )
    canvas.paste(scaled, (left, top))
    draw = ImageDraw.Draw(canvas, "RGBA")
    step_x = region_width + separation_x
    step_y = region_height + separation_y
    columns = max(0, (source.width - margin_x + separation_x) // step_x)
    rows = max(0, (source.height - margin_y + separation_y) // step_y)
    line = (255, 45, 120, 210)
    label = (245, 247, 250, 255)
    for x in range(columns + 1):
        position = left + (margin_x + x * step_x) * scale
        draw.line((position, top, position, top + scaled.height), fill=line, width=2)
        if x < columns:
            draw.text((position + 3, 31), str(x), fill=label)
    for y in range(rows + 1):
        position = top + (margin_y + y * step_y) * scale
        draw.line((left, position, left + scaled.width, position), fill=line, width=2)
        if y < rows:
            draw.text((24, position + 3), str(y), fill=label)
    draw.text(
        (8, 8),
        (
            f"Atlas coordinates (0-based): region {region_width}x{region_height}px, "
            f"separation {separation_x}x{separation_y}px"
        ),
        fill=label,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="PNG", optimize=True)


def _render_visual_catalog(
    source_paths: tuple[Path, ...],
    relative_paths: tuple[str, ...],
    output_path: Path,
) -> None:
    """Render a bounded, labeled overview of public sprite/image families."""
    if not source_paths or len(source_paths) != len(relative_paths):
        raise ValueError("visual catalog requires matching non-empty source paths and labels")
    columns = 4
    cell_width = 300
    cell_height = 190
    header_height = 42
    rows = math.ceil(len(source_paths) / columns)
    canvas = Image.new(
        "RGBA",
        (columns * cell_width, header_height + rows * cell_height),
        (22, 25, 31, 255),
    )
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.text(
        (12, 12),
        "Public visual asset overview; labels are project-relative paths",
        fill=(245, 247, 250, 255),
    )
    for index, (source_path, relative_path) in enumerate(
        zip(source_paths, relative_paths, strict=True)
    ):
        column = index % columns
        row = index // columns
        left = column * cell_width
        top = header_height + row * cell_height
        checker = Image.new("RGBA", (128, 128), (232, 235, 239, 255))
        checker_draw = ImageDraw.Draw(checker, "RGBA")
        for y in range(0, 128, 16):
            for x in range(0, 128, 16):
                if (x // 16 + y // 16) % 2:
                    checker_draw.rectangle((x, y, x + 15, y + 15), fill=(202, 207, 214, 255))
        try:
            with Image.open(source_path) as opened:
                thumbnail = opened.convert("RGBA")
                original_size = thumbnail.size
                thumbnail.thumbnail((128, 128), Image.Resampling.NEAREST)
        except (OSError, ValueError):
            original_size = (0, 0)
            thumbnail = Image.new("RGBA", (1, 1), (255, 0, 255, 255))
        image_left = left + (cell_width - 128) // 2
        canvas.alpha_composite(checker, (image_left, top + 6))
        canvas.alpha_composite(
            thumbnail,
            (
                image_left + (128 - thumbnail.width) // 2,
                top + 6 + (128 - thumbnail.height) // 2,
            ),
        )
        short_label = relative_path
        if len(short_label) > 46:
            short_label = "…" + short_label[-45:]
        draw.text((left + 8, top + 140), short_label, fill=(245, 247, 250, 255))
        draw.text(
            (left + 8, top + 158),
            f"{original_size[0]}×{original_size[1]}",
            fill=(170, 178, 190, 255),
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="PNG", optimize=True)


def _add_visual_review(
    *,
    harness_run: HarnessRun,
    engine: GodotHarnessEngineAdapter,
    task: NormalizedTask,
    route: GameDevBenchTaskRoute,
    agent_executable: Path,
    model_name: str,
    reasoning_effort: str | None,
    input_usd_per_million: float,
    cached_input_usd_per_million: float,
    output_usd_per_million: float,
) -> HarnessRun:
    retained_feedback = sorted(harness_run.directory.glob("visual-feedback-*.json"))
    if retained_feedback:
        feedback_path = retained_feedback[-1]
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        verdict = str(feedback.get("verdict", "uncertain"))
        summary = str(feedback.get("summary", "visual feedback was inconclusive"))
        canonical_evidence = {
            **feedback,
            "reused_pre_evaluation_feedback": True,
            "structured_gates": {gate.gate: gate.status.value for gate in harness_run.result.gates},
        }
        evidence_path = harness_run.directory / "visual-evidence.json"
        evidence_path.write_text(
            json.dumps(canonical_evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        screenshot_name = feedback.get("candidate_screenshot")
        screenshot_exists = (
            isinstance(screenshot_name, str) and (harness_run.directory / screenshot_name).is_file()
        )
        gate = GateOutcome(
            gate="visual_auxiliary",
            status={
                "pass": GateStatus.PASS,
                "fail": GateStatus.FAIL,
                "uncertain": GateStatus.NOT_RUN,
            }.get(verdict, GateStatus.NOT_RUN),
            detail=summary,
            artifacts=(
                *((str(screenshot_name),) if screenshot_exists else ()),
                feedback_path.name,
                "visual-evidence.json",
            ),
        )
        updated = harness_run.result.model_copy(
            update={
                "gates": (*harness_run.result.gates, gate),
                "ended_at": datetime.now(UTC),
            }
        )
        updated = updated.model_copy(
            update={"artifacts": HarnessRunner._artifact_manifest(harness_run.directory)}
        )
        (harness_run.directory / "result.json").write_text(
            json.dumps(updated.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return HarnessRun(harness_run.directory, updated)

    screenshot_path = harness_run.directory / "fixed-camera.png"
    capture_log = harness_run.directory / "fixed-camera.log"
    capture = _capture_fixed_camera(engine, screenshot_path, capture_log)
    structured_gates = {gate.gate: gate.status.value for gate in harness_run.result.gates}
    structured_state = engine._public_structured_state()
    evidence: dict[str, object] = {
        "schema_version": 1,
        "advisory_only": True,
        "can_replace_structured_gates": False,
        "camera_rule": {
            "viewport": "1280x720",
            "scene": "project main scene with deterministic fallback",
            "settle_frames": 3,
            "input": "none",
        },
        "source_images": list(route.visual_relative_paths),
        "candidate_screenshot": "fixed-camera.png" if screenshot_path.is_file() else None,
        "capture": capture,
        "structured_gates": structured_gates,
        "structured_state": structured_state,
        "verdict": "uncertain",
        "progress": "not_applicable",
        "grounded_objects": [],
        "summary": "fixed-camera capture was unavailable",
    }
    review_response = None
    if capture["status"] == "success" and screenshot_path.is_file():
        system = (
            "You are an auxiliary visual reviewer, not the gameplay evaluator. Compare the public "
            "source images with the final fixed-camera screenshot for the requested visible "
            "change. Ground visible claims in the supplied public scene/resource objects and "
            "structural assertions. "
            "Never infer gameplay state or override structured test results. Return only JSON with "
            "the requested fields."
        )
        prompt = json.dumps(
            {
                "task_instruction": task.instruction,
                "images_in_order": [
                    *route.visual_relative_paths,
                    "fixed-camera.png (candidate result)",
                ],
                "structured_gates": structured_gates,
                "structured_candidate_state": structured_state,
                "decision_schema": {
                    "verdict": "pass|fail|uncertain",
                    "progress": "not_applicable",
                    "summary": "visual evidence only",
                    "grounded_objects": "public scene node/resource identities used",
                    "structured_evidence_acknowledged": True,
                },
            },
            sort_keys=True,
        )
        reviewer = AgentCliLanguageModel(
            agent_executable,
            model_name,
            timeout_seconds=300,
            input_usd_per_million=input_usd_per_million,
            cached_input_usd_per_million=cached_input_usd_per_million,
            output_usd_per_million=output_usd_per_million,
            reasoning_effort=reasoning_effort,
            image_paths=(*route.visual_paths, screenshot_path),
        )
        try:
            review_response = reviewer.complete(system=system, prompt=prompt)
            decision = VisualReviewDecision.model_validate_json(review_response.text)
            verdict = decision.verdict if decision.structured_evidence_acknowledged else "uncertain"
            evidence.update(
                {
                    "verdict": verdict,
                    "progress": "not_applicable",
                    "summary": decision.summary,
                    "grounded_objects": list(decision.grounded_objects),
                    "model_trace": review_response.trace_record(
                        system=system,
                        prompt=prompt,
                    ),
                }
            )
        except (ModelError, ValueError) as error:
            evidence.update(
                {
                    "verdict": "uncertain",
                    "progress": "not_applicable",
                    "summary": "visual review did not produce a valid bounded decision",
                    "failure_category": type(error).__name__,
                }
            )

    evidence_path = harness_run.directory / "visual-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verdict = str(evidence["verdict"])
    gate = GateOutcome(
        gate="visual_auxiliary",
        status={
            "pass": GateStatus.PASS,
            "fail": GateStatus.FAIL,
            "uncertain": GateStatus.NOT_RUN,
        }[verdict],
        detail=str(evidence["summary"]),
        artifacts=(
            *(("fixed-camera.png",) if screenshot_path.is_file() else ()),
            "visual-evidence.json",
        ),
    )
    update: dict[str, object] = {
        "gates": (*harness_run.result.gates, gate),
        "ended_at": datetime.now(UTC),
    }
    if review_response is not None:
        update.update(
            {
                "input_tokens": (
                    harness_run.result.input_tokens + review_response.usage.input_tokens
                ),
                "cached_input_tokens": (
                    harness_run.result.cached_input_tokens
                    + review_response.usage.cached_input_tokens
                ),
                "output_tokens": (
                    harness_run.result.output_tokens + review_response.usage.output_tokens
                ),
                "reasoning_output_tokens": (
                    harness_run.result.reasoning_output_tokens
                    + review_response.usage.reasoning_output_tokens
                ),
                "cost_usd": harness_run.result.cost_usd + review_response.usage.cost_usd,
            }
        )
    updated = harness_run.result.model_copy(update=update)
    updated = updated.model_copy(
        update={"artifacts": HarnessRunner._artifact_manifest(harness_run.directory)}
    )
    (harness_run.directory / "result.json").write_text(
        json.dumps(updated.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return HarnessRun(harness_run.directory, updated)


def _capture_fixed_camera(
    engine: GodotHarnessEngineAdapter,
    screenshot_path: Path,
    log_path: Path,
) -> dict[str, object]:
    if sys.platform != "darwin":
        return {"status": "not_available", "reason": "fixed capture requires macOS display"}
    script = Path(__file__).parents[1] / "resources" / "godot" / "fixed_camera_capture.gd"
    if not script.is_file():
        return {"status": "not_available", "reason": "fixed capture script is missing"}
    screenshot_path.unlink(missing_ok=True)
    metadata_path = screenshot_path.with_suffix(".capture.json")
    metadata_path.unlink(missing_ok=True)
    import_log_path = log_path.with_name(f"{log_path.stem}-import{log_path.suffix}")
    with _isolated_godot_project(engine.project) as sandbox:
        imported = _run_bounded_process(
            [
                str(engine.godot),
                "--headless",
                "--import",
                "--quit",
                "--path",
                str(sandbox.project),
                "--log-file",
                str(import_log_path),
            ],
            timeout_seconds=_PUBLIC_VALIDATION_TIMEOUT_SECONDS,
        )
        import_output = (imported.stdout or "") + (imported.stderr or "")
        import_log_path.write_text(import_output, encoding="utf-8")
        if imported.returncode != 0:
            return {
                "status": "failed",
                "reason": "capture project import failed",
                "import_return_code": imported.returncode,
                "import_diagnostics": _public_diagnostics(import_output)[:40],
                "import_log_path": import_log_path.name,
                "log_path": log_path.name,
                "coordinate_evidence_path": None,
            }
        completed = _run_bounded_process(
            [
                str(engine.godot),
                "--path",
                str(sandbox.project),
                "--script",
                str(script),
                "--display-driver",
                "macos",
                "--rendering-method",
                "gl_compatibility",
                "--audio-driver",
                "Dummy",
                "--windowed",
                "--resolution",
                "1280x720",
                "--position",
                "0,0",
                "--log-file",
                str(log_path),
                "--",
                str(screenshot_path),
                str(metadata_path),
            ],
            timeout_seconds=45,
        )
    output = completed.stdout + completed.stderr
    log_path.write_text(output, encoding="utf-8")
    import_diagnostics = _public_diagnostics(import_output)
    capture_diagnostics = _public_diagnostics(output)
    metadata: object | None = None
    if metadata_path.is_file() and metadata_path.stat().st_size <= 1024 * 1024:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            metadata = None
    passed = (
        completed.returncode == 0
        and screenshot_path.is_file()
        and screenshot_path.stat().st_size > 0
        and "FIXED_CAPTURE_RESULT=0" in output
        and "FIXED_CAPTURE_METADATA_RESULT=0" in output
        and isinstance(metadata, dict)
    )
    return {
        "status": "success" if passed else "failed",
        "return_code": completed.returncode,
        "import_return_code": imported.returncode,
        "import_diagnostics": import_diagnostics[:40],
        "capture_diagnostics": capture_diagnostics[:40],
        "fidelity_status": (
            "reliable"
            if passed and not import_diagnostics and not capture_diagnostics
            else "degraded"
        ),
        "same_imported_copy_for_capture": True,
        "log_path": log_path.name,
        "import_log_path": import_log_path.name,
        "coordinate_evidence_path": metadata_path.name if metadata_path.is_file() else None,
        "coordinate_evidence": metadata,
    }


def _bounded_capture_evidence(capture: Mapping[str, object]) -> dict[str, object]:
    """Keep coordinate/fidelity facts visible without overflowing one observation."""
    bounded = {
        key: capture.get(key)
        for key in (
            "status",
            "return_code",
            "import_return_code",
            "import_diagnostics",
            "capture_diagnostics",
            "fidelity_status",
            "same_imported_copy_for_capture",
            "log_path",
            "import_log_path",
            "coordinate_evidence_path",
        )
        if key in capture
    }
    metadata = capture.get("coordinate_evidence")
    if not isinstance(metadata, dict):
        bounded["coordinate_evidence"] = None
        return bounded
    nodes = metadata.get("nodes")
    selected: list[object] = []
    if isinstance(nodes, list):
        ranked = sorted(
            enumerate(nodes),
            key=lambda item: (
                isinstance(item[1], dict)
                and any(
                    key in item[1]
                    for key in (
                        "sprite_2d",
                        "animated_sprite_2d",
                        "texture_control",
                        "polygon_2d",
                        "camera_2d",
                        "control",
                    )
                ),
                -item[0],
            ),
            reverse=True,
        )
        selected = [value for _index, value in ranked[:128]]
    bounded["coordinate_evidence"] = {
        key: metadata.get(key)
        for key in (
            "schema_version",
            "scene_path",
            "capture_size",
            "viewport",
            "settle_frames",
            "state_phase",
            "node_count_total",
            "nodes_truncated",
            "coordinate_contract",
        )
    }
    coordinate_evidence = bounded["coordinate_evidence"]
    assert isinstance(coordinate_evidence, dict)
    coordinate_evidence["nodes"] = selected
    coordinate_evidence["nodes_returned"] = len(selected)
    while len(json.dumps(bounded, sort_keys=True).encode("utf-8")) > 96 * 1024 and selected:
        selected.pop()
    return bounded


def _requires_quantitative_visual_evidence(instruction: str) -> bool:
    """Recognize precision claims that a qualitative reviewer must not certify as exact."""
    normalized = instruction.casefold()
    return any(
        phrase in normalized
        for phrase in (
            "as closely as possible",
            "minimal spill",
            "tight fit",
            "tightly cover",
            "pixel-perfect",
            "pixel perfect",
            "precisely align",
            "exactly align",
            "overlap above",
            "intersection over union",
            "iou",
            "尽可能贴合",
            "最小溢出",
            "像素级",
            "精确对齐",
        )
    )


def _public_instruction_paths(instruction: str) -> tuple[str, ...]:
    normalized_suffixes = (
        suffix.removeprefix(".")
        for suffix in (_TEXT_SUFFIXES | _ENGINE_RESOURCE_SUFFIXES | DEFAULT_ASSET_SUFFIXES)
    )
    suffixes = "|".join(sorted(normalized_suffixes, key=len, reverse=True))
    pattern = re.compile(rf"(?:res://)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:{suffixes})")
    paths = (match.removeprefix("res://") for match in pattern.findall(instruction))
    return tuple(dict.fromkeys(paths))


def _validate_engine_variant(value: object) -> None:
    if value is None or isinstance(value, str | bool):
        return
    if isinstance(value, int | float) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("engine resource numbers must be finite")
        return
    if not isinstance(value, dict):
        raise ValueError("engine resource values must be scalars or typed variant objects")
    kind = value.get("type")
    fields = {
        "Vector2": ("x", "y"),
        "Vector2i": ("x", "y"),
        "Vector3": ("x", "y", "z"),
        "Vector3i": ("x", "y", "z"),
        "Vector4": ("x", "y", "z", "w"),
        "Color": ("r", "g", "b", "a"),
        "Rect2": ("x", "y", "width", "height"),
    }.get(str(kind))
    if fields is None or set(value) != {"type", *fields}:
        raise ValueError("engine resource typed variant has an unsupported shape")
    for field_name in fields:
        component = value[field_name]
        if not isinstance(component, int | float) or isinstance(component, bool):
            raise ValueError("engine resource variant components must be numeric")
        if isinstance(component, float) and not math.isfinite(component):
            raise ValueError("engine resource variant components must be finite")
        if str(kind).endswith("i") and not isinstance(component, int):
            raise ValueError("integer vector components must be integers")


def _scene_rooted_node_paths(sections: list[object]) -> frozenset[str]:
    nodes = [
        section
        for section in sections
        if isinstance(section, dict) and section.get("section") == "node"
    ]
    roots = [
        section
        for section in nodes
        if isinstance(section.get("attributes"), dict) and "parent" not in section["attributes"]
    ]
    if len(roots) != 1:
        raise ValueError("scene path resolution requires exactly one root node")
    root_attributes = roots[0]["attributes"]
    assert isinstance(root_attributes, dict)
    root_name = root_attributes.get("name")
    if not isinstance(root_name, str) or not root_name:
        raise ValueError("scene root node has no public name")
    paths: set[str] = set()
    for section in nodes:
        attributes = section.get("attributes")
        if not isinstance(attributes, dict):
            continue
        name = attributes.get("name")
        parent = attributes.get("parent")
        if not isinstance(name, str) or not name:
            continue
        if parent is None:
            paths.add(name)
        elif parent == ".":
            paths.add(f"{root_name}/{name}")
        elif isinstance(parent, str) and parent:
            paths.add(f"{root_name}/{parent}/{name}")
    return frozenset(paths)


def _synchronize_script_node_references(
    document: GodotResourceDocument,
    project: Path,
) -> list[dict[str, object]]:
    """Add Godot node_paths metadata only for script exports typed as Node objects."""
    scripts: dict[str, Path] = {}
    for section in document.sections:
        if section.section != "ext_resource":
            continue
        if section.attributes.get("type") != "Script":
            continue
        identifier = section.attributes.get("id")
        raw_path = section.attributes.get("path")
        if not identifier or not raw_path or not raw_path.startswith("res://"):
            continue
        relative = _safe_relative(raw_path.removeprefix("res://"))
        target = project / relative
        if (
            target.is_file()
            and not target.is_symlink()
            and target.resolve(strict=True).is_relative_to(project.resolve(strict=True))
        ):
            scripts[identifier] = target

    exported_types_by_script: dict[Path, dict[str, str]] = {}
    changes: list[dict[str, object]] = []
    for section in document.sections:
        if section.section != "node":
            continue
        properties = section.properties()
        script_value = properties.get("script")
        matched_script = (
            _EXT_RESOURCE_VALUE.fullmatch(script_value) if script_value is not None else None
        )
        if matched_script is None:
            continue
        script = scripts.get(matched_script.group("id"))
        if script is None:
            continue
        if script not in exported_types_by_script:
            source = script.read_text(encoding="utf-8")
            exported_types_by_script[script] = {
                match.group("name"): match.group("type")
                for match in _GDSCRIPT_EXPORTED_TYPE.finditer(source)
            }
        exported_types = exported_types_by_script[script]
        node_references = tuple(
            name
            for name, value in properties.items()
            if value.startswith("NodePath(")
            and value.endswith(")")
            and name in exported_types
            and exported_types[name] != "NodePath"
        )
        if (
            node_references
            and (change := document.ensure_node_reference_metadata(section, node_references))
            is not None
        ):
            changes.append(change)
    return changes


def _context_fits(paths: tuple[str, ...], existing_sizes: dict[str, int]) -> bool:
    if len(paths) > _CONTEXT_MAXIMUM_FILES:
        return False
    sizes = [existing_sizes.get(path, 0) for path in paths]
    return (
        all(size <= _CONTEXT_MAXIMUM_FILE_BYTES for size in sizes)
        and sum(sizes) <= _CONTEXT_MAXIMUM_TOTAL_BYTES
    )


def _member_relative(filename: str, prefix: PurePosixPath) -> PurePosixPath | None:
    member = PurePosixPath(filename)
    if member.is_absolute() or ".." in member.parts:
        raise ValueError(f"unsafe benchmark archive member: {filename}")
    try:
        return member.relative_to(prefix)
    except ValueError:
        return None


def _is_hidden_benchmark_file(relative: PurePosixPath) -> bool:
    if any(part.startswith(".") for part in relative.parts):
        return True
    name = relative.name.lower()
    return name == "task_config.json" or name.startswith("test") or name.endswith(".log")


def _safe_relative(raw_path: str) -> Path:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise PolicyViolation(f"path must be project-relative and cannot traverse: {raw_path}")
    return relative


def _run_bounded_process(
    command: list[str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    invocation = next(_GODOT_PROCESS_INVOCATIONS)
    crashes: list[subprocess.CompletedProcess[str]] = []
    artifacts: list[str] = []
    for attempt in range(1, _GODOT_PROCESS_ATTEMPTS + 1):
        attempt_command = _godot_command_for_attempt(command, attempt)
        completed = _run_process_once(
            attempt_command,
            timeout_seconds=timeout_seconds,
        )
        if not _godot_process_crashed(completed):
            return completed
        crashes.append(completed)
        artifact = _persist_godot_crash_attempt(
            command,
            completed,
            attempt,
            invocation=invocation,
        )
        if artifact is not None:
            artifacts.append(str(artifact))
    environment_crash = next(
        (
            (completed, detail)
            for completed in reversed(crashes)
            if (detail := _godot_environment_failure(completed)) is not None
        ),
        None,
    )
    if environment_crash is not None:
        completed, environment_failure = environment_crash
        artifact = _persist_godot_environment_failure(
            command,
            completed,
            invocation=invocation,
        )
        detail = (
            "Godot host environment rejected a required data path and the engine "
            f"crashed after {_GODOT_PROCESS_ATTEMPTS} attempts: {environment_failure}"
        )
        evidence = [*artifacts, *((str(artifact),) if artifact is not None else ())]
        if evidence:
            detail += "; evidence: " + ", ".join(evidence)
        raise GodotEnvironmentError(detail)
    codes = ", ".join(str(item.returncode) for item in crashes)
    detail = f"Godot engine crashed after {_GODOT_PROCESS_ATTEMPTS} attempts; return codes: {codes}"
    if artifacts:
        detail += "; evidence: " + ", ".join(artifacts)
    raise GodotEngineCrash(detail)


def _run_process_once(
    command: list[str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    with _godot_process_slot(timeout_seconds) as process_timeout_seconds:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_godot_process_environment(command[0]),
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=process_timeout_seconds)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=_GODOT_GRACEFUL_TERMINATION_SECONDS)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                stdout, stderr = process.communicate()
            stderr += f"\npublic validation timed out after {timeout_seconds:g} seconds\n"
            return subprocess.CompletedProcess(command, 124, stdout, stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@contextmanager
def _godot_process_slot(timeout_seconds: float) -> Iterator[float]:
    """Serialize Host-owned Godot processes without spending their execution budget in queue."""

    if fcntl is None:
        yield timeout_seconds
        return
    started = time.monotonic()
    with _GODOT_PROCESS_LOCK.open("a+b") as stream:
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                elapsed = time.monotonic() - started
                if elapsed >= _GODOT_PROCESS_QUEUE_TIMEOUT_SECONDS:
                    raise GodotEnvironmentError(
                        "timed out waiting for the cross-task Host Godot process slot"
                    ) from error
                time.sleep(min(0.05, _GODOT_PROCESS_QUEUE_TIMEOUT_SECONDS - elapsed))
        try:
            yield timeout_seconds
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _godot_process_crashed(completed: subprocess.CompletedProcess[str]) -> bool:
    if completed.returncode in {-11, -6, 134, 139}:
        return True
    output = (completed.stdout or "") + (completed.stderr or "")
    return any(pattern in output for pattern in _GODOT_CRASH_PATTERNS)


def _godot_environment_failure(
    completed: subprocess.CompletedProcess[str],
) -> str | None:
    if not _godot_process_crashed(completed):
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    lines = output.splitlines()
    for line in lines:
        stripped = line.strip()
        if "/Library/Application Support/Godot" in stripped and any(
            marker in stripped
            for marker in (
                "Cannot save file",
                "Could not create directory",
                "Error attempting to create data dir",
                "Error saving editor settings",
            )
        ):
            return stripped[:1000]
        if any(pattern in stripped for pattern in _GODOT_ENVIRONMENT_FAILURE_PATTERNS):
            return stripped[:1000]
        if "Could not create directory: 'user://" in stripped:
            return stripped[:1000]
    return None


def _godot_command_for_attempt(command: list[str], attempt: int) -> list[str]:
    retried = list(command)
    if attempt == 1 or "--log-file" not in retried:
        return retried
    index = retried.index("--log-file") + 1
    if index >= len(retried):
        return retried
    original = Path(retried[index])
    retried[index] = str(
        original.with_name(f"{original.stem}.retry-{attempt - 1}{original.suffix}")
    )
    return retried


def _godot_process_environment(executable: str) -> dict[str, str] | None:
    """Keep AppKit crash restoration out of non-interactive Godot processes."""

    if sys.platform != "darwin":
        return None
    resolved = Path(executable).resolve()
    runtime_root = next(
        (parent for parent in resolved.parents if (parent / "._sc_").is_file()),
        None,
    )
    if runtime_root is None:
        return None
    home_root = runtime_root / "home"
    home_root.mkdir(parents=True, exist_ok=True)
    private_home = Path(tempfile.mkdtemp(prefix="process-", dir=home_root))
    environment = os.environ.copy()
    environment["HOME"] = str(private_home)
    environment["CFFIXED_USER_HOME"] = str(private_home)
    return environment


def _persist_godot_crash_attempt(
    command: list[str],
    completed: subprocess.CompletedProcess[str],
    attempt: int,
    *,
    invocation: int,
) -> Path | None:
    if "--log-file" not in command:
        return None
    index = command.index("--log-file") + 1
    if index >= len(command):
        return None
    base = Path(command[index])
    artifact = base.with_name(
        f"{base.stem}.engine-crash-attempt-{attempt}-"
        f"invocation-{invocation:04d}{base.suffix or '.log'}"
    )
    payload = (
        f"command={json.dumps(completed.args)}\n"
        f"return_code={completed.returncode}\n"
        "[stdout]\n"
        f"{completed.stdout or ''}\n"
        "[stderr]\n"
        f"{completed.stderr or ''}\n"
    )
    try:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(payload, encoding="utf-8")
    except OSError:
        return None
    return artifact


def _persist_godot_environment_failure(
    command: list[str],
    completed: subprocess.CompletedProcess[str],
    *,
    invocation: int,
) -> Path | None:
    if "--log-file" not in command:
        return None
    index = command.index("--log-file") + 1
    if index >= len(command):
        return None
    base = Path(command[index])
    artifact = base.with_name(
        f"{base.stem}.environment-failure-invocation-{invocation:04d}{base.suffix or '.log'}"
    )
    payload = (
        f"command={json.dumps(completed.args)}\n"
        f"return_code={completed.returncode}\n"
        "[stdout]\n"
        f"{completed.stdout or ''}\n"
        "[stderr]\n"
        f"{completed.stderr or ''}\n"
    )
    try:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(payload, encoding="utf-8")
    except OSError:
        return None
    return artifact


def _public_diagnostics(output: str) -> list[str]:
    diagnostics: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if any(pattern in stripped for pattern in _PUBLIC_ERROR_PATTERNS):
            diagnostics.append(stripped[:1000])
    return list(dict.fromkeys(diagnostics))


def _decode_public_text(payload: bytes) -> tuple[str, str]:
    """Decode common project text encodings while rejecting binary-looking content."""

    candidates: tuple[tuple[str, str], ...]
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = (("utf-16", "utf-16"),)
    elif payload.startswith(b"\xef\xbb\xbf"):
        candidates = (("utf-8-sig", "utf-8-sig"),)
    else:
        candidates = (
            ("utf-8", "utf-8"),
            ("cp1252", "windows-1252"),
            ("latin-1", "iso-8859-1"),
        )
    for codec, label in candidates:
        try:
            content = payload.decode(codec)
        except UnicodeDecodeError:
            continue
        controls = sum(ord(character) < 32 and character not in "\t\n\r" for character in content)
        if "\x00" in content or controls > max(2, len(content) // 100):
            continue
        return content, label
    raise ValueError("payload is not supported public text")


def _public_validation_regressions(
    current: Mapping[str, object],
    baseline: Mapping[str, object],
) -> tuple[list[str], list[str]]:
    """Identify failures introduced by the candidate instead of inherited from input."""

    baseline_diagnostics = {
        str(item) for item in baseline.get("diagnostics", []) if isinstance(item, str)
    }
    new_diagnostics = [
        str(item)
        for item in current.get("diagnostics", [])
        if isinstance(item, str) and item not in baseline_diagnostics
    ]
    regressions = [f"new diagnostic: {item}" for item in new_diagnostics]
    for key, stage in (
        ("import_return_code", "import"),
        ("resource_return_code", "resource load"),
    ):
        current_code = current.get(key)
        baseline_code = baseline.get(key)
        if (
            isinstance(current_code, int)
            and current_code != 0
            and (not isinstance(baseline_code, int) or baseline_code == 0)
        ):
            regressions.append(f"{stage} newly failed with return code {current_code}")
    current_startup = current.get("startup_return_code")
    baseline_startup = baseline.get("startup_return_code")
    if (
        isinstance(current_startup, int)
        and current_startup not in {0, 124}
        and (not isinstance(baseline_startup, int) or baseline_startup in {0, 124})
    ):
        regressions.append(f"startup newly failed with return code {current_startup}")
    return list(dict.fromkeys(regressions)), new_diagnostics


def _project_has_main_scene(project_file: Path) -> bool:
    try:
        content = project_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(
        re.search(
            r'^\s*run/main_scene\s*=\s*"res://[^"\r\n]+"\s*$',
            content,
            flags=re.MULTILINE,
        )
    )


def _copy_without_engine_cache(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        return {
            name for name in names if name in IGNORED_DIRECTORY_NAMES or (base / name).is_symlink()
        }

    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
