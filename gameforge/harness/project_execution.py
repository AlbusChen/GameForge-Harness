from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from gameforge.adapters.llm import LanguageModel
from gameforge.adapters.unity import UnityBatchGateway
from gameforge.adapters.unity_harness import UnityHarnessEngineAdapter, UnityNativeEvaluator
from gameforge.config import configured_unity_editor
from gameforge.harness.acceptance import AcceptanceEvaluator, validate_acceptance
from gameforge.harness.contracts import RunSpec
from gameforge.harness.preservation import IGNORED_DIRECTORY_NAMES
from gameforge.harness.runner import HarnessRun, HarnessRunner


@dataclass(frozen=True)
class IsolatedProjectRun:
    workspace: Path
    harness_run: HarnessRun


def execute_isolated_unity_project(
    *,
    root: Path,
    run_spec: RunSpec,
    model: LanguageModel,
) -> IsolatedProjectRun:
    validate_acceptance(run_spec.task.acceptance)
    editor = configured_unity_editor()
    if editor is None:
        raise ValueError("the pinned Unity editor is not installed or UNITY_EDITOR_PATH is invalid")

    output_root = (root / "runs" / "project-runs").resolve()
    workspace_root = (root / "runs" / "project-workspaces" / run_spec.run_id).resolve()
    workspace = workspace_root / "project"
    _copy_project(run_spec.task.project_path, workspace)

    sibling_bridge = run_spec.task.project_path.resolve().parent / "AgentBridge"
    if sibling_bridge.is_dir():
        _copy_tree(sibling_bridge, workspace_root / "AgentBridge")

    task = run_spec.task.model_copy(update={"project_path": workspace})
    isolated_spec = run_spec.model_copy(update={"task": task})
    run_directory = output_root / isolated_spec.run_id
    gateway = UnityBatchGateway(editor, workspace, run_directory)
    engine = UnityHarnessEngineAdapter(gateway, editable_paths=task.editable_paths)
    evaluator = AcceptanceEvaluator(UnityNativeEvaluator(gateway))
    harness_run = HarnessRunner(model, engine, evaluator).execute(isolated_spec, output_root)
    return IsolatedProjectRun(workspace=workspace, harness_run=harness_run)


def _copy_project(source: Path, destination: Path) -> None:
    project = source.resolve(strict=True)
    if not project.is_dir():
        raise ValueError(f"project path is not a directory: {project}")
    if destination.exists():
        raise ValueError(f"isolated workspace already exists: {destination}")
    _copy_tree(project, destination)


def _copy_tree(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        ignored = {
            name for name in names if name in IGNORED_DIRECTORY_NAMES or (base / name).is_symlink()
        }
        return ignored

    shutil.copytree(source, destination, ignore=ignore, copy_function=shutil.copy2)
