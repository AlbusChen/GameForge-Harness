from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from gameforge.adapters.llm import ModelError, ModelResponse
from gameforge.harness.contracts import (
    GateStatus,
    RunResultV2,
    RunSpecV2,
    RunStatus,
)
from gameforge.harness.interfaces import EvaluatorAdapter
from gameforge.harness.preservation import snapshot_project
from gameforge.harness.runner import HarnessRun, HarnessRunner


class NativeWorkspaceModel(Protocol):
    def run_workspace_agent(
        self,
        *,
        project: Path,
        prompt: str,
        timeout_seconds: float,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class MinimalWorkspaceHarnessRunner:
    """Run one open native workspace session, then only the independent evaluator."""

    model: NativeWorkspaceModel
    evaluator: EvaluatorAdapter
    prompt: str
    timeout_seconds: float
    environment_overrides: Mapping[str, str] | None = None

    def execute(self, run_spec: RunSpecV2, output_root: Path) -> HarnessRun:
        started_at = datetime.now(UTC)
        run_directory = output_root / run_spec.run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        HarnessRunner._write_json(run_directory / "run-spec.json", run_spec.model_dump(mode="json"))
        (run_directory / "agent-prompt.txt").write_text(self.prompt + "\n", encoding="utf-8")

        before = snapshot_project(run_spec.task.project_path)
        HarnessRunner._write_json(
            run_directory / "project-before.json", before.model_dump(mode="json")
        )
        response: ModelResponse | None = None
        model_error: ModelError | None = None
        try:
            response = self.model.run_workspace_agent(
                project=run_spec.task.project_path,
                prompt=self.prompt,
                timeout_seconds=self.timeout_seconds,
                environment_overrides=self.environment_overrides,
            )
        except ModelError as error:
            model_error = error

        after = snapshot_project(run_spec.task.project_path)
        HarnessRunner._write_json(
            run_directory / "project-after.json", after.model_dump(mode="json")
        )
        changed_paths = tuple(
            sorted(
                path
                for path in set(before.files) | set(after.files)
                if before.files.get(path) != after.files.get(path)
            )
        )
        changes = [
            {
                "path": path,
                "sha256_before": before.files.get(path),
                "sha256_after": after.files.get(path),
            }
            for path in changed_paths
        ]
        HarnessRunner._write_json(
            run_directory / "project-changes.json",
            {"schema_version": 1, "changes": changes},
        )

        audit = response.tool_audit if response is not None else ()
        HarnessRunner._write_json(
            run_directory / "agent-tool-audit.json",
            {"schema_version": 1, "events": audit},
        )
        outside_project_changes = sum(
            1
            for event in audit
            if event.get("kind") == "file_change"
            for change in event.get("changes", [])
            if isinstance(change, dict) and change.get("within_project") is False
        )
        HarnessRunner._write_json(
            run_directory / "agent-result.json",
            {
                "schema_version": 1,
                "solver_completed": response is not None,
                "solver_error_code": model_error.code if model_error is not None else None,
                "solver_error": str(model_error) if model_error is not None else None,
                "final_message": response.text if response is not None else "",
                "transport_attempts": (response.transport_attempts if response is not None else 0),
                "changed_paths": list(changed_paths),
                "outside_project_changes_reported": outside_project_changes,
            },
        )

        evaluator_error: Exception | None = None
        try:
            evaluated = self.evaluator.evaluate(
                run_spec,
                run_spec.task.project_path,
            )
        except Exception as error:  # Evaluator failure is infrastructure, never model quality.
            evaluator_error = error
            evaluated = ()
            HarnessRunner._write_json(
                run_directory / "failure.json",
                {
                    "category": f"evaluator_{type(error).__name__}",
                    "detail": str(error),
                },
            )
        gates = evaluated
        HarnessRunner._write_json(
            run_directory / "gate-results.json",
            [gate.model_dump(mode="json") for gate in gates],
        )
        status = HarnessRunner._status_from_gates(run_spec, gates)
        if evaluator_error is not None:
            failure_category = f"evaluator_{type(evaluator_error).__name__}"
            failure_detail = str(evaluator_error)
        elif status is RunStatus.FAIL:
            failure_category = "evaluation"
            failure_detail = "the independent evaluator did not pass"
        elif status is RunStatus.BLOCKED:
            failure_category = "evaluator_inconclusive"
            failure_detail = "the independent evaluator produced no verdict"
        else:
            failure_category = None
            failure_detail = None

        required = run_spec.evaluation.required_gates
        by_gate = {gate.gate: gate.status for gate in gates}
        coverage = sum(by_gate.get(name) is not GateStatus.NOT_RUN for name in required) / len(
            required
        )
        usage = response.usage if response is not None else None
        artifacts = HarnessRunner._artifact_manifest(run_directory)
        result = RunResultV2(
            run_id=run_spec.run_id,
            status=status,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            model=run_spec.model,
            gates=gates,
            artifacts=artifacts,
            input_tokens=usage.input_tokens if usage is not None else 0,
            output_tokens=usage.output_tokens if usage is not None else 0,
            cached_input_tokens=usage.cached_input_tokens if usage is not None else 0,
            reasoning_output_tokens=(usage.reasoning_output_tokens if usage is not None else 0),
            cost_usd=usage.cost_usd if usage is not None else 0,
            tool_calls=len(audit),
            program_cells=0,
            model_turns=1 if response is not None else 0,
            prompt_bytes=len(self.prompt.encode("utf-8")),
            capability_calls_per_turn=0,
            evidence_subjects=len(gates),
            required_evidence_ids=required,
            required_evidence_coverage=coverage,
            lineage_complete=True,
            dimension_status={gate.gate: gate.status.value for gate in gates},
            policy_violations=0,
            failure_category=failure_category,
            failure_detail=failure_detail,
        )
        HarnessRunner._write_json(run_directory / "result.json", result.model_dump(mode="json"))
        return HarnessRun(run_directory, result)
