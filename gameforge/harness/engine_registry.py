from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from gameforge.adapters.godot_harness import GodotImportEvaluator
from gameforge.adapters.unity import UnityBatchGateway
from gameforge.adapters.unity_harness import UnityCompileEvaluator
from gameforge.config import (
    EXPECTED_GODOT_VERSION,
    EXPECTED_UNITY_VERSION,
    configured_godot_editor,
    configured_unity_editor,
)
from gameforge.harness.contracts import EngineName
from gameforge.harness.engine_tools import (
    EngineToolSession,
    godot_tool_session,
    unity_tool_session,
)
from gameforge.harness.execution_profiles import ExecutionProfile
from gameforge.harness.interfaces import EvaluatorAdapter


@dataclass(frozen=True)
class WorkspaceEngineAdapter:
    """The intentionally thin engine-specific edge of the common workspace pipeline."""

    name: EngineName
    version: str
    entry_points: tuple[str, ...]
    evaluator_ref: str
    evaluator_version: str

    def validate_project(self, project: Path) -> None:
        if self.name is EngineName.UNITY:
            valid = (project / "Assets").is_dir() and (
                project / "ProjectSettings" / "ProjectVersion.txt"
            ).is_file()
        else:
            valid = (project / "project.godot").is_file()
        if not valid:
            raise ValueError(f"{self.name.value} project markers are missing: {project}")

    def executable(self) -> Path | None:
        if self.name is EngineName.UNITY:
            return configured_unity_editor()
        return configured_godot_editor()

    def tool_session(
        self,
        *,
        profile: ExecutionProfile,
        workspace: Path,
        executable: Path | None,
        timeout_seconds: float,
    ) -> AbstractContextManager[EngineToolSession]:
        if self.name is EngineName.UNITY:
            return unity_tool_session(
                profile=profile,
                workspace=workspace,
                editor=executable,
                timeout_seconds=timeout_seconds,
            )
        return godot_tool_session(
            profile=profile,
            workspace=workspace,
            executable=executable,
            timeout_seconds=timeout_seconds,
        )

    def evaluator(
        self,
        *,
        workspace: Path,
        run_directory: Path,
        executable: Path,
    ) -> EvaluatorAdapter:
        if self.name is EngineName.UNITY:
            return UnityCompileEvaluator(UnityBatchGateway(executable, workspace, run_directory))
        return GodotImportEvaluator(executable, run_directory)


ENGINE_REGISTRY = {
    EngineName.UNITY: WorkspaceEngineAdapter(
        name=EngineName.UNITY,
        version=EXPECTED_UNITY_VERSION,
        entry_points=("Assets",),
        evaluator_ref="unity-compile",
        evaluator_version="unity-generic-compile-v1",
    ),
    EngineName.GODOT: WorkspaceEngineAdapter(
        name=EngineName.GODOT,
        version=EXPECTED_GODOT_VERSION,
        entry_points=("project.godot",),
        evaluator_ref="godot-import",
        evaluator_version="godot-generic-import-v1",
    ),
}


def detect_workspace_engine(project: Path) -> EngineName:
    """Detect supported engines only from stable project markers, never task content."""

    resolved = project.resolve(strict=True)
    matches: list[EngineName] = []
    if (resolved / "Assets").is_dir() and (
        resolved / "ProjectSettings" / "ProjectVersion.txt"
    ).is_file():
        matches.append(EngineName.UNITY)
    if (resolved / "project.godot").is_file():
        matches.append(EngineName.GODOT)
    if not matches:
        raise ValueError(
            "could not detect a supported engine; pass --engine explicitly after adding "
            "the engine's standard project files"
        )
    if len(matches) > 1:
        raise ValueError("workspace matches multiple engines; pass --engine explicitly")
    return matches[0]


def resolve_workspace_engine(value: str, project: Path) -> WorkspaceEngineAdapter:
    engine = detect_workspace_engine(project) if value == "auto" else EngineName(value)
    return ENGINE_REGISTRY[engine]
