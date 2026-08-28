from __future__ import annotations

import hashlib
import json
import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from gameforge.harness.contracts import (
    EvaluationSpec,
    HarnessProfile,
    NormalizedTask,
    RunBudgets,
    TaskSourceKind,
)
from gameforge.harness.engine_registry import resolve_workspace_engine
from gameforge.harness.engine_tools import write_engine_receipt
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    AssetPolicy,
    EngineEnvironment,
    GameTaskSpec,
    RequirementEnforcement,
    RuntimeProtocol,
)
from gameforge.harness.minimal_workspace import (
    MinimalWorkspaceHarnessRunner,
    NativeWorkspaceModel,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.preservation import IGNORED_DIRECTORY_NAMES, snapshot_project
from gameforge.harness.run_factory import create_run_spec_v2
from gameforge.harness.runner import HarnessRun, HarnessRunner


@dataclass(frozen=True)
class OpenGameWorkspaceRun:
    directory: Path
    workspace: Path
    engine: str
    harness_run: HarnessRun


def _copy_project(source: Path, destination: Path) -> None:
    resolved = source.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"game project is not a directory: {resolved}")

    excluded_root_child: str | None = None
    with suppress(ValueError):
        excluded_root_child = destination.resolve().relative_to(resolved).parts[0]

    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        return {
            name
            for name in names
            if name in IGNORED_DIRECTORY_NAMES
            or (base / name).is_symlink()
            or (
                excluded_root_child is not None
                and base.resolve() == resolved
                and name == excluded_root_child
            )
        }

    shutil.copytree(resolved, destination, ignore=ignore, copy_function=shutil.copy2)
    sibling_bridge = resolved.parent / "AgentBridge"
    if sibling_bridge.is_dir():
        shutil.copytree(
            sibling_bridge,
            destination.parent / "AgentBridge",
            ignore=ignore,
            copy_function=shutil.copy2,
        )


def _prompt(request: str, engine: str, profile: str) -> str:
    boundary = (
        "Commands run directly on the Host."
        if profile == "native-open"
        else f"The pinned Host {engine} executable is reached through a transparent broker; "
        "its arguments, output, and exit status are unchanged."
        if profile == "supervised-native"
        else f"Commands run through the configured external strong-isolation runner, which "
        f"must provide {engine} as `{engine}`."
    )
    return (
        f"Complete the following request in the full disposable {engine} project. Choose your "
        "own investigation, editing, asset, engine CLI, and validation strategy; the Harness "
        f"does not prescribe a workflow. The pinned engine is available as `{engine}`. Keep "
        "intended project changes in the current workspace and finish only when the request "
        f"is complete. {boundary}\n\nREQUEST:\n{request.strip()}"
    )


def run_open_game_workspace(
    *,
    root: Path,
    project: Path,
    request: str,
    model_profile: ModelProfile,
    engine: str = "auto",
    task_id: str = "game-open-workspace",
    timeout_seconds: int = 1800,
) -> OpenGameWorkspaceRun:
    """Run the common minimal-open pipeline with a thin selected engine adapter."""

    if not request.strip():
        raise ValueError("game workspace request must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("game workspace timeout must be positive")
    source = project.resolve(strict=True)
    engine_adapter = resolve_workspace_engine(engine, source)
    engine_adapter.validate_project(source)
    now = datetime.now(UTC)
    run_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{task_id}"
    workspace_root = root / "runs" / "game-open-workspaces" / run_id
    workspace = workspace_root / "project"
    workspace_root.mkdir(parents=True, exist_ok=False)
    _copy_project(source, workspace)
    initial = snapshot_project(workspace)
    source_payload = {
        "engine": engine_adapter.name.value,
        "instruction": request.strip(),
        "source_project_sha256": hashlib.sha256(
            json.dumps(initial.files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    task = NormalizedTask(
        id=task_id,
        source=TaskSourceKind.CHANGE,
        instruction=request.strip(),
        project_path=workspace,
        acceptance=(f"{engine_adapter.name.value} project imports and scripts compile",),
        editable_paths=tuple(sorted(initial.files)),
        source_sha256=NormalizedTask.source_hash(source_payload),
        metadata={
            "workspace_authority": "all-public-project-files",
            "workflow": "one-open-native-session",
            "engine_selection": "detected" if engine == "auto" else "explicit",
        },
    )
    game_task = GameTaskSpec(
        engine_profile=f"{engine_adapter.name.value}-open-workspace",
        entry_points=engine_adapter.entry_points,
        target_platforms=("editor",),
        requirements=(
            AcceptanceRequirement(
                id="compilation",
                dimension=AcceptanceDimension.BUILD,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref=engine_adapter.evaluator_ref,
            ),
        ),
        asset_policy=AssetPolicy(
            allow_binary_mutation=True,
            allow_text_scope_expansion=True,
            allow_native_workspace_agent=True,
            maximum_writable_paths=10_000,
        ),
    )
    run_spec = create_run_spec_v2(
        task,
        model_profile,
        engine=engine_adapter.name,
        engine_version=engine_adapter.version,
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(
            engine_version=engine_adapter.version,
            target_platform="editor",
        ),
        runtime_protocol=RuntimeProtocol.MINIMAL_OPEN_V1,
        budgets=RunBudgets(
            wall_seconds=timeout_seconds,
            max_turns=1,
            max_tool_calls=1000,
            max_repairs=0,
            max_cost_usd=100,
        ),
        evaluation=EvaluationSpec(
            required_gates=("specification", "compilation"),
            evaluator_version=engine_adapter.evaluator_version,
        ),
        now=now,
    ).model_copy(update={"run_id": run_id})
    output_root = root / "runs" / "game-open"
    run_directory = output_root / run_id
    executable = engine_adapter.executable()
    if executable is None:
        raise ValueError(f"the pinned {engine_adapter.name.value} executable is required")

    with engine_adapter.tool_session(
        profile=model_profile.execution_profile,
        workspace=workspace,
        executable=executable,
        timeout_seconds=min(timeout_seconds, 1800),
    ) as engine_session:
        adapter = model_profile.build_adapter(
            root,
            trusted_read_roots=(
                Path(__file__).resolve().parents[2],
                Path(sys.executable).resolve(strict=True).parents[1],
            ),
            host_runtime_roots=(engine_session.root,) if engine_session.root else (),
        )
        evaluator = engine_adapter.evaluator(
            workspace=workspace,
            run_directory=run_directory,
            executable=executable,
        )
        harness_run = MinimalWorkspaceHarnessRunner(
            model=cast(NativeWorkspaceModel, adapter),
            evaluator=evaluator,
            prompt=_prompt(
                request,
                engine_adapter.name.value,
                model_profile.execution_profile.value,
            ),
            timeout_seconds=timeout_seconds,
            environment_overrides=engine_session.environment,
        ).execute(run_spec, output_root)
        write_engine_receipt(harness_run.directory / "engine-runtime.json", engine_session)

    metadata = {
        "schema_version": 1,
        "engine": engine_adapter.name.value,
        "engine_version": engine_adapter.version,
        "engine_selection": "detected" if engine == "auto" else "explicit",
        "solver_backend": model_profile.provider.value,
        "execution_profile": model_profile.execution_profile.value,
        "workspace_contract": "minimal-open-v1",
        "intermediate_harness_tools": False,
        "intermediate_validation": False,
        "status": harness_run.result.status.value,
    }
    (harness_run.directory / "workspace-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    updated_result = harness_run.result.model_copy(
        update={"artifacts": HarnessRunner._artifact_manifest(harness_run.directory)}
    )
    HarnessRunner._write_json(
        harness_run.directory / "result.json",
        updated_result.model_dump(mode="json"),
    )
    updated_run = HarnessRun(harness_run.directory, updated_result)
    return OpenGameWorkspaceRun(
        updated_run.directory,
        workspace,
        engine_adapter.name.value,
        updated_run,
    )
