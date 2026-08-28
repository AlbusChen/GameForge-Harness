from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gameforge.adapters.llm import LanguageModel, ModelError
from gameforge.harness.contracts import (
    ArtifactRecord,
    GateOutcome,
    GateStatus,
    RunResult,
    RunSpec,
    RunSpecV2,
    RunStatus,
)
from gameforge.harness.game_tasks import RuntimeProtocol
from gameforge.harness.interfaces import EngineAdapter, EvaluatorAdapter
from gameforge.harness.preservation import compare_snapshots, snapshot_project
from gameforge.harness.project_context import build_project_context
from gameforge.harness.runtime import AdapterToolExecutor, AgentRuntime, AgentRuntimeResult
from gameforge.harness.skills import SkillRegistry


@dataclass(frozen=True)
class HarnessRun:
    directory: Path
    result: RunResult


@dataclass
class HarnessRunner:
    model: LanguageModel
    engine: EngineAdapter
    evaluator: EvaluatorAdapter
    skills: SkillRegistry | None = None

    def execute(self, run_spec: RunSpec, output_root: Path) -> HarnessRun:
        if (
            isinstance(run_spec, RunSpecV2)
            and run_spec.runtime_protocol is RuntimeProtocol.PROGRAMMABLE_V1
        ):
            from gameforge.harness.programmable_runner import ProgrammableHarnessRunner

            return ProgrammableHarnessRunner(
                self.model,
                self.engine,
                self.evaluator,
                self.skills,
            ).execute(run_spec, output_root)
        started_at = datetime.now(UTC)
        run_directory = output_root / run_spec.run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        self._write_json(run_directory / "run-spec.json", run_spec.model_dump(mode="json"))

        runtime_result: AgentRuntimeResult | None = None
        gates: tuple[GateOutcome, ...] = tuple(
            GateOutcome(gate=name, status=GateStatus.NOT_RUN, detail="agent did not finish")
            for name in run_spec.evaluation.required_gates
        )
        status = RunStatus.BLOCKED
        failure_category: str | None = None
        failure_detail: str | None = None

        try:
            before_snapshot = snapshot_project(run_spec.task.project_path)
            self._write_json(
                run_directory / "project-before.json",
                before_snapshot.model_dump(mode="json"),
            )
            context = build_project_context(
                run_spec.task.project_path,
                run_spec.task.editable_paths,
            )
            self._write_json(
                run_directory / "project-context.json",
                context.model_dump(mode="json"),
            )
            runtime = AgentRuntime(
                model=self.model,
                tools=AdapterToolExecutor(self.engine),
                budgets=run_spec.budgets,
                validation_tool=self.engine.automatic_validation_tool(),
                completion_feedback_tools=self.engine.completion_feedback_tools(),
                max_validation_repairs=run_spec.budgets.max_repairs,
            )
            runtime_result = runtime.run(run_spec.task, context)
            self._write_json(run_directory / "agent-result.json", runtime_result.to_dict())
            self._write_jsonl(run_directory / "trace.jsonl", runtime_result.trace)

            if runtime_result.outcome == "success":
                after_snapshot = snapshot_project(run_spec.task.project_path)
                preservation = compare_snapshots(
                    before_snapshot,
                    after_snapshot,
                    run_spec.task.editable_paths,
                )
                self._write_json(
                    run_directory / "preservation.json",
                    preservation.model_dump(mode="json"),
                )
                preservation_gate = GateOutcome(
                    gate="preservation",
                    status=(GateStatus.PASS if preservation.passed else GateStatus.FAIL),
                    detail=(
                        "all non-editable project files preserved"
                        if preservation.passed
                        else "unrelated project changes detected: "
                        + ", ".join(preservation.unrelated_changed_paths)
                    ),
                    artifacts=("preservation.json",),
                )
                try:
                    evaluator_spec = self._without_preservation_gate(run_spec)
                    evaluated_gates = self.evaluator.evaluate(
                        evaluator_spec,
                        run_spec.task.project_path,
                    )
                except Exception as error:  # An adapter failure must not erase the run.
                    gates = self._merge_preservation_gate(
                        run_spec,
                        (),
                        preservation_gate,
                    )
                    status = RunStatus.BLOCKED
                    failure_category = f"evaluator_{type(error).__name__}"
                    failure_detail = str(error)
                    self._write_json(
                        run_directory / "failure.json",
                        {"category": failure_category, "detail": failure_detail},
                    )
                else:
                    gates = self._merge_preservation_gate(
                        run_spec,
                        evaluated_gates,
                        preservation_gate,
                    )
                    status = self._status_from_gates(run_spec, gates)
                    if status is RunStatus.FAIL:
                        failure_category = "evaluation"
                        failure_detail = "one or more required verification gates did not pass"
                    elif status is RunStatus.BLOCKED:
                        failure_category = "evaluator_inconclusive"
                        failure_detail = "one or more required gates were not evaluated"
                self._write_json(
                    run_directory / "gate-results.json",
                    [gate.model_dump(mode="json") for gate in gates],
                )
            else:
                failure_category = "agent_blocked"
                failure_detail = runtime_result.summary
        except (ModelError, OSError, ValueError, RuntimeError) as error:
            failure_category = type(error).__name__
            failure_detail = str(error)
            self._write_json(
                run_directory / "failure.json",
                {"category": failure_category, "detail": failure_detail},
            )

        artifacts = self._artifact_manifest(run_directory)
        ended_at = datetime.now(UTC)
        result = RunResult(
            run_id=run_spec.run_id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            model=run_spec.model,
            gates=gates,
            artifacts=artifacts,
            input_tokens=runtime_result.input_tokens if runtime_result else 0,
            output_tokens=runtime_result.output_tokens if runtime_result else 0,
            cached_input_tokens=(runtime_result.cached_input_tokens if runtime_result else 0),
            reasoning_output_tokens=(
                runtime_result.reasoning_output_tokens if runtime_result else 0
            ),
            cost_usd=runtime_result.cost_usd if runtime_result else 0,
            tool_calls=runtime_result.tool_calls if runtime_result else 0,
            failure_category=failure_category,
            failure_detail=failure_detail,
        )
        self._write_json(run_directory / "result.json", result.model_dump(mode="json"))
        return HarnessRun(run_directory, result)

    @staticmethod
    def _required_gates_pass(run_spec: RunSpec, gates: tuple[GateOutcome, ...]) -> bool:
        statuses = {gate.gate: gate.status for gate in gates}
        return all(
            statuses.get(required) is GateStatus.PASS
            for required in run_spec.evaluation.required_gates
        )

    @staticmethod
    def _status_from_gates(run_spec: RunSpec, gates: tuple[GateOutcome, ...]) -> RunStatus:
        statuses = {gate.gate: gate.status for gate in gates}
        required = tuple(
            statuses.get(name, GateStatus.NOT_RUN) for name in run_spec.evaluation.required_gates
        )
        if all(status is GateStatus.PASS for status in required):
            return RunStatus.PASS
        if any(status is GateStatus.NOT_RUN for status in required):
            return RunStatus.BLOCKED
        return RunStatus.FAIL

    @staticmethod
    def _without_preservation_gate(run_spec: RunSpec) -> RunSpec:
        required = tuple(
            gate for gate in run_spec.evaluation.required_gates if gate != "preservation"
        )
        evaluation = run_spec.evaluation.model_copy(update={"required_gates": required})
        return run_spec.model_copy(update={"evaluation": evaluation})

    @staticmethod
    def _merge_preservation_gate(
        run_spec: RunSpec,
        evaluated: tuple[GateOutcome, ...],
        preservation: GateOutcome,
    ) -> tuple[GateOutcome, ...]:
        by_name = {gate.gate: gate for gate in evaluated}
        if "preservation" in run_spec.evaluation.required_gates:
            by_name["preservation"] = preservation
        return tuple(
            by_name.get(
                name,
                GateOutcome(
                    gate=name,
                    status=GateStatus.NOT_RUN,
                    detail="evaluator did not return the required gate",
                ),
            )
            for name in run_spec.evaluation.required_gates
        )

    @staticmethod
    def _artifact_manifest(run_directory: Path) -> tuple[ArtifactRecord, ...]:
        artifacts: list[ArtifactRecord] = []
        for path in sorted(run_directory.rglob("*")):
            if not path.is_file() or path.name == "result.json":
                continue
            content = path.read_bytes()
            artifacts.append(
                ArtifactRecord(
                    path=str(path.relative_to(run_directory)),
                    kind=path.suffix.lstrip(".") or "file",
                    sha256=hashlib.sha256(content).hexdigest(),
                    bytes=len(content),
                )
            )
        return tuple(artifacts)

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _write_jsonl(path: Path, records: tuple[dict[str, object], ...]) -> None:
        with path.open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
