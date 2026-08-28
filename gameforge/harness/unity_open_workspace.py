from __future__ import annotations

from pathlib import Path

from gameforge.harness.game_workspace import (
    OpenGameWorkspaceRun,
    run_open_game_workspace,
)
from gameforge.harness.game_workspace import _copy_project as _copy_game_project
from gameforge.harness.model_profiles import ModelProfile

OpenUnityWorkspaceRun = OpenGameWorkspaceRun
_copy_project = _copy_game_project


def run_open_unity_workspace(
    *,
    root: Path,
    project: Path,
    request: str,
    model_profile: ModelProfile,
    task_id: str = "unity-open-workspace",
    timeout_seconds: int = 1800,
) -> OpenUnityWorkspaceRun:
    """Compatibility alias for the unified game-workspace entrypoint."""

    return run_open_game_workspace(
        root=root,
        project=project,
        request=request,
        model_profile=model_profile,
        engine="unity",
        task_id=task_id,
        timeout_seconds=timeout_seconds,
    )
