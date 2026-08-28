from __future__ import annotations

import json
from pathlib import Path

from gameforge.adapters.llm import ModelResponse, ModelUsage
from gameforge.harness.contracts import (
    EngineName,
    GateOutcome,
    GateStatus,
    HarnessProfile,
    RunStatus,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.run_factory import create_run_spec
from gameforge.harness.runner import HarnessRunner
from gameforge.harness.task_sources import ChangeRequestTaskSource


class FinishingModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system, prompt
        self.calls += 1
        decision = (
            {
                "kind": "tool",
                "tool": "health_check",
                "arguments": {},
                "rationale": "Inspect the engine before finishing.",
            }
            if self.calls == 1
            else {
                "kind": "finish",
                "outcome": "success",
                "summary": "The requested inspection is complete.",
                "rationale": "The health check passed.",
            }
        )
        return ModelResponse(
            text=json.dumps(decision),
            usage=ModelUsage(12, 6, 0.001),
            provider="scripted",
            model="test-model",
        )


class FakeEngine:
    name = "unity"
    version = "test"

    def available_tools(self) -> tuple[str, ...]:
        return ("health_check",)

    def automatic_validation_tool(self) -> str | None:
        return None

    def completion_feedback_tools(self) -> tuple[str, ...]:
        return ()

    def invoke(self, tool_name: str, arguments: object) -> object:
        assert tool_name == "health_check"
        assert arguments == {}
        return {"status": "healthy"}


class MutatingEngine(FakeEngine):
    def __init__(self, project: Path) -> None:
        self.project = project

    def invoke(self, tool_name: str, arguments: object) -> object:
        (self.project / "HumanChange.cs").write_text("overwritten\n", encoding="utf-8")
        return super().invoke(tool_name, arguments)


class PassingEvaluator:
    name = "fake"
    version = "1"

    def evaluate(self, run_spec: object, workspace: Path) -> tuple[GateOutcome, ...]:
        assert workspace.is_dir()
        required = run_spec.evaluation.required_gates  # type: ignore[attr-defined]
        return tuple(GateOutcome(gate=name, status=GateStatus.PASS) for name in required)


class TimeoutEvaluator:
    name = "timeout"
    version = "1"

    def evaluate(self, run_spec: object, workspace: Path) -> tuple[GateOutcome, ...]:
        del run_spec, workspace
        raise TimeoutError("engine evaluator exceeded its bounded deadline")


def test_harness_runner_persists_run_spec_trace_gates_and_result(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource("Inspect the game project.").load(project)
    profile = ModelProfile(provider="mock", model="scripted-test")
    run_spec = create_run_spec(
        task,
        profile,
        engine=EngineName.UNITY,
        engine_version="test",
        profile=HarnessProfile.PROJECT,
    )

    run = HarnessRunner(FinishingModel(), FakeEngine(), PassingEvaluator()).execute(
        run_spec, tmp_path / "runs"
    )

    assert run.result.status is RunStatus.PASS
    assert run.result.tool_calls == 1
    assert run.result.cost_usd == 0.002
    assert (run.directory / "run-spec.json").is_file()
    assert (run.directory / "trace.jsonl").is_file()
    assert (run.directory / "gate-results.json").is_file()
    assert (run.directory / "result.json").is_file()
    assert {artifact.path for artifact in run.result.artifacts} >= {
        "run-spec.json",
        "trace.jsonl",
        "gate-results.json",
        "preservation.json",
    }


def test_harness_runner_fails_when_engine_changes_unapproved_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "Approved.cs").write_text("approved\n", encoding="utf-8")
    (project / "HumanChange.cs").write_text("human edit\n", encoding="utf-8")
    task = ChangeRequestTaskSource(
        "Inspect without changing unrelated work.",
        editable_paths=("Approved.cs",),
    ).load(project)
    run_spec = create_run_spec(
        task,
        ModelProfile(provider="mock", model="scripted-test"),
        engine=EngineName.UNITY,
        engine_version="test",
        profile=HarnessProfile.PROJECT,
    )

    run = HarnessRunner(FinishingModel(), MutatingEngine(project), PassingEvaluator()).execute(
        run_spec, tmp_path / "runs"
    )

    preservation = next(gate for gate in run.result.gates if gate.gate == "preservation")
    assert run.result.status is RunStatus.FAIL
    assert preservation.status is GateStatus.FAIL
    assert "HumanChange.cs" in preservation.detail


def test_harness_runner_preserves_usage_and_candidate_when_evaluator_times_out(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource("Inspect the game project.").load(project)
    run_spec = create_run_spec(
        task,
        ModelProfile(provider="mock", model="scripted-test"),
        engine=EngineName.UNITY,
        engine_version="test",
        profile=HarnessProfile.PROJECT,
    )

    run = HarnessRunner(FinishingModel(), FakeEngine(), TimeoutEvaluator()).execute(
        run_spec, tmp_path / "runs"
    )

    statuses = {gate.gate: gate.status for gate in run.result.gates}
    assert run.result.status is RunStatus.BLOCKED
    assert run.result.input_tokens == 24
    assert run.result.output_tokens == 12
    assert run.result.tool_calls == 1
    assert run.result.failure_category == "evaluator_TimeoutError"
    assert statuses["preservation"] is GateStatus.PASS
    assert statuses["specification"] is GateStatus.NOT_RUN
    assert (run.directory / "agent-result.json").is_file()
    assert (run.directory / "failure.json").is_file()
    assert (run.directory / "result.json").is_file()
