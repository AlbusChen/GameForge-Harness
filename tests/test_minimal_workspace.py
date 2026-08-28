from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from gameforge.adapters.llm import ModelError, ModelResponse, ModelUsage
from gameforge.harness.contracts import (
    EngineName,
    EvaluationSpec,
    GateOutcome,
    GateStatus,
    HarnessProfile,
    RunStatus,
)
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    EngineEnvironment,
    GameTaskSpec,
    RequirementEnforcement,
    RuntimeProtocol,
)
from gameforge.harness.minimal_workspace import MinimalWorkspaceHarnessRunner
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.run_factory import create_run_spec_v2
from gameforge.harness.task_sources import ChangeRequestTaskSource


class EditingWorkspaceModel:
    def run_workspace_agent(
        self,
        *,
        project: Path,
        prompt: str,
        timeout_seconds: float,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> ModelResponse:
        assert prompt == "Solve this freely."
        assert timeout_seconds == 900
        assert environment_overrides == {"GODOT": "/isolated/godot"}
        (project / "scene.tscn").write_text("after\n", encoding="utf-8")
        return ModelResponse(
            text="done",
            usage=ModelUsage(100, 20, 0, 40, 5),
            tool_audit=(
                {
                    "kind": "file_change",
                    "changes": [
                        {"path": "scene.tscn", "within_project": True},
                        {"path": "$OUTSIDE_PROJECT", "within_project": False},
                    ],
                },
            ),
        )


class FailedWorkspaceModel:
    def run_workspace_agent(
        self,
        *,
        project: Path,
        prompt: str,
        timeout_seconds: float,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> ModelResponse:
        del project, prompt, timeout_seconds, environment_overrides
        raise ModelError("timeout", "native session timed out")


class ContentEvaluator:
    def evaluate(self, run_spec: object, workspace: Path) -> tuple[GateOutcome, ...]:
        del run_spec
        passed = (workspace / "scene.tscn").read_text(encoding="utf-8") == "after\n"
        return (
            GateOutcome(gate="specification", status=GateStatus.PASS),
            GateOutcome(
                gate="official",
                status=GateStatus.PASS if passed else GateStatus.FAIL,
            ),
        )


def _run_spec(project: Path):  # type: ignore[no-untyped-def]
    task = ChangeRequestTaskSource(
        "Change the scene.", editable_paths=("scene.tscn",)
    ).load(project)
    game_task = GameTaskSpec(
        engine_profile="test",
        entry_points=("scene.tscn",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="official",
                dimension=AcceptanceDimension.BEHAVIOR,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="official",
            ),
        ),
    )
    return create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="test",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(
            engine_version="test", target_platform="test"
        ),
        runtime_protocol=RuntimeProtocol.MINIMAL_OPEN_V1,
        evaluation=EvaluationSpec(
            required_gates=("specification", "official")
        ),
        now=datetime(2026, 8, 17, tzinfo=UTC),
    )


def test_minimal_workspace_runs_one_open_session_then_official(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "scene.tscn").write_text("before\n", encoding="utf-8")

    run = MinimalWorkspaceHarnessRunner(
        model=EditingWorkspaceModel(),
        evaluator=ContentEvaluator(),
        prompt="Solve this freely.",
        timeout_seconds=900,
        environment_overrides={"GODOT": "/isolated/godot"},
    ).execute(_run_spec(project), tmp_path / "runs")

    assert run.result.status is RunStatus.PASS
    assert run.result.model_turns == 1  # type: ignore[attr-defined]
    assert run.result.input_tokens == 100
    assert run.result.required_evidence_coverage == 1  # type: ignore[attr-defined]
    assert run.result.lineage_complete  # type: ignore[attr-defined]
    assert run.result.policy_violations == 0  # The workspace-write sandbox is authoritative.
    agent_result = json.loads(
        (run.directory / "agent-result.json").read_text(encoding="utf-8")
    )
    assert agent_result["outside_project_changes_reported"] == 1
    assert (project / "scene.tscn").read_text(encoding="utf-8") == "after\n"


def test_minimal_workspace_still_evaluates_after_solver_timeout(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "scene.tscn").write_text("before\n", encoding="utf-8")

    run = MinimalWorkspaceHarnessRunner(
        model=FailedWorkspaceModel(),
        evaluator=ContentEvaluator(),
        prompt="Solve this freely.",
        timeout_seconds=900,
    ).execute(_run_spec(project), tmp_path / "runs")

    assert run.result.status is RunStatus.FAIL
    assert run.result.model_turns == 0  # type: ignore[attr-defined]
    assert run.result.gates[1].status is GateStatus.FAIL
