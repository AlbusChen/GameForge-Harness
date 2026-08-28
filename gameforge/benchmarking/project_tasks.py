from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gameforge.adapters.llm import ScriptedControlLanguageModel
from gameforge.config import EXPECTED_UNITY_VERSION
from gameforge.harness.acceptance import validate_acceptance
from gameforge.harness.contracts import (
    EngineName,
    EvaluationSpec,
    HarnessProfile,
    ModelRunSpec,
    NormalizedTask,
    TaskSourceKind,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.project_execution import execute_isolated_unity_project
from gameforge.harness.run_factory import create_run_spec


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectControlTask(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    source: Literal["create", "change"]
    instruction: str = Field(min_length=1)
    editable_paths: tuple[str, ...] = Field(min_length=1)
    acceptance: tuple[str, ...] = Field(min_length=1)
    engine_gates: tuple[str, ...] = Field(min_length=1)
    decisions: tuple[dict[str, object], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_task(self) -> ProjectControlTask:
        if len(self.editable_paths) != len(set(self.editable_paths)):
            raise ValueError("editable_paths must be unique")
        if len(self.engine_gates) != len(set(self.engine_gates)):
            raise ValueError("engine_gates must be unique")
        validate_acceptance(self.acceptance)
        return self

    def normalized(self, project: Path, benchmark_name: str, version: str) -> NormalizedTask:
        public_payload = self.model_dump(mode="json", exclude={"decisions"})
        return NormalizedTask(
            id=self.id,
            source=TaskSourceKind(self.source),
            instruction=self.instruction,
            project_path=project.resolve(),
            acceptance=self.acceptance,
            editable_paths=self.editable_paths,
            source_sha256=NormalizedTask.source_hash(public_payload),
            metadata={
                "control_policy": "scripted-control-v1",
                "catalog": benchmark_name,
                "catalog_version": version,
            },
        )


class ProjectTaskCatalog(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    engine: Literal["unity"]
    control_policy: Literal["scripted-control-v1"]
    tasks: tuple[ProjectControlTask, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def task_ids_are_unique(self) -> ProjectTaskCatalog:
        identifiers = [task.id for task in self.tasks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("project task IDs must be unique")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> ProjectTaskCatalog:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read project task catalog: {path}") from error
        except yaml.YAMLError as error:
            raise ValueError(f"invalid project task catalog YAML: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("project task catalog root must be a mapping")
        return cls.model_validate(payload)


@dataclass(frozen=True)
class ProjectTaskBenchmarkRun:
    benchmark_id: str
    directory: Path
    summary: dict[str, object]


def run_project_task_benchmark(
    *,
    root: Path,
    catalog_path: Path,
    template_project: Path,
    adapter_mode: Literal["control", "model"],
    model_profile: ModelProfile | None = None,
    task_ids: tuple[str, ...] = (),
) -> ProjectTaskBenchmarkRun:
    catalog = ProjectTaskCatalog.from_yaml(catalog_path)
    by_id = {task.id: task for task in catalog.tasks}
    selected = task_ids or tuple(by_id)
    if len(selected) != len(set(selected)):
        raise ValueError("selected project task IDs must be unique")
    missing = [task_id for task_id in selected if task_id not in by_id]
    if missing:
        raise ValueError(f"unknown project task IDs: {', '.join(missing)}")

    if adapter_mode == "control":
        profile = ModelProfile(provider="mock", model="scripted-control")
    else:
        if model_profile is None or model_profile.provider.value == "mock":
            raise ValueError("model adapter mode requires a non-mock model profile")
        profile = model_profile

    started = datetime.now(UTC)
    benchmark_id = started.strftime(f"%Y%m%dT%H%M%SZ-project-{adapter_mode}-{len(selected)}")
    directory = root / "runs" / "benchmarks" / benchmark_id
    directory.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, object]] = []

    for index, task_id in enumerate(selected, start=1):
        fixture = by_id[task_id]
        task = fixture.normalized(template_project, catalog.name, catalog.version)
        required_gates = tuple(
            dict.fromkeys(
                (
                    "specification",
                    "task_acceptance",
                    *(gate for gate in fixture.engine_gates if gate != "specification"),
                    "preservation",
                )
            )
        )
        evaluation = EvaluationSpec(
            required_gates=required_gates,
            evaluator_version="unity-native-plus-file-assertions-v1",
        )
        run_spec = create_run_spec(
            task,
            profile,
            engine=EngineName.UNITY,
            engine_version=EXPECTED_UNITY_VERSION,
            profile=HarnessProfile.PROJECT,
            evaluation=evaluation,
        )
        if adapter_mode == "control":
            script_payload = json.dumps(
                fixture.decisions,
                sort_keys=True,
                separators=(",", ":"),
            )
            script_hash = hashlib.sha256(script_payload.encode("utf-8")).hexdigest()
            run_spec = run_spec.model_copy(
                update={
                    "model": ModelRunSpec(
                        provider="scripted-control",
                        model="scripted-control-v1",
                        parameters={
                            "deterministic_replay": True,
                            "decision_script_sha256": script_hash,
                        },
                        profile_sha256=script_hash,
                    )
                }
            )
            model = ScriptedControlLanguageModel(fixture.decisions, name=task_id)
        else:
            model = profile.build_adapter(root)
        print(
            f"Project {adapter_mode} task {index}/{len(selected)} started: {task_id}",
            flush=True,
        )
        execution = execute_isolated_unity_project(root=root, run_spec=run_spec, model=model)
        result = execution.harness_run.result
        preservation = next(
            (gate for gate in result.gates if gate.gate == "preservation"),
            None,
        )
        acceptance = next(
            (gate for gate in result.gates if gate.gate == "task_acceptance"),
            None,
        )
        record = {
            "index": index,
            "task_id": task_id,
            "source": fixture.source,
            "status": result.status.value,
            "run_directory": str(execution.harness_run.directory),
            "workspace": str(execution.workspace),
            "tool_calls": result.tool_calls,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "task_acceptance": acceptance.status.value if acceptance else "NOT_RUN",
            "preservation": preservation.status.value if preservation else "NOT_RUN",
            "failure_category": result.failure_category,
            "failure_detail": result.failure_detail,
        }
        records.append(record)
        _append_jsonl(directory / "runs.jsonl", record)
        print(
            f"Project {adapter_mode} task {index}/{len(selected)} completed: {result.status}",
            flush=True,
        )

    ended = datetime.now(UTC)
    passed = sum(record["status"] == "PASS" for record in records)
    summary: dict[str, object] = {
        "benchmark_id": benchmark_id,
        "benchmark_name": catalog.name,
        "benchmark_version": catalog.version,
        "adapter_mode": "scripted-control" if adapter_mode == "control" else "model",
        "control_result": adapter_mode == "control",
        "model_performance_claim": adapter_mode == "model",
        "model": profile.model,
        "tasks_completed": len(records),
        "tasks_passed": passed,
        "end_to_end_success_rate": round(passed / len(records), 4) if records else 0,
        "task_acceptance_rate": round(
            sum(record["task_acceptance"] == "PASS" for record in records) / len(records),
            4,
        )
        if records
        else 0,
        "preservation_rate": round(
            sum(record["preservation"] == "PASS" for record in records) / len(records),
            4,
        )
        if records
        else 0,
        "input_tokens": sum(int(record["input_tokens"]) for record in records),
        "output_tokens": sum(int(record["output_tokens"]) for record in records),
        "cost_usd": round(sum(float(record["cost_usd"]) for record in records), 8),
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "wall_duration_seconds": round((ended - started).total_seconds(), 3),
    }
    _write_json(directory / "summary.json", summary)
    _write_json(directory / "runs.json", records)
    _write_report(directory / "report.html", summary, records)
    return ProjectTaskBenchmarkRun(benchmark_id, directory, summary)


def run_project_task_control(
    *,
    root: Path,
    catalog_path: Path,
    template_project: Path,
    task_ids: tuple[str, ...] = (),
) -> ProjectTaskBenchmarkRun:
    return run_project_task_benchmark(
        root=root,
        catalog_path=catalog_path,
        template_project=template_project,
        adapter_mode="control",
        task_ids=task_ids,
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_report(
    path: Path,
    summary: dict[str, object],
    records: list[dict[str, object]],
) -> None:
    is_control = summary.get("control_result") is True
    title = "Project task control report" if is_control else "Project task model report"
    interpretation = (
        "This is a deterministic scripted control, not a model performance result."
        if is_control
        else (
            "This is a model-backed result for ordinary create/change project tasks; "
            "acceptance and preservation are evaluated independently."
        )
    )
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    task_rows = "".join(
        "<tr>"
        f"<td>{record['index']}</td>"
        f"<td>{html.escape(str(record['task_id']))}</td>"
        f"<td>{record['source']}</td>"
        f"<td>{record['status']}</td>"
        f"<td>{record['task_acceptance']}</td>"
        f"<td>{record['preservation']}</td>"
        f"<td>{record['tool_calls']}</td>"
        "</tr>"
        for record in records
    )
    path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        f"<title>{title}</title>"
        "<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;}"
        "table{border-collapse:collapse;width:100%;margin:20px 0;}th,td{border:1px solid #bbb;"
        "padding:8px;text-align:left;}th{background:#eee}</style>"
        f"<h1>{title}</h1>"
        f"<p>{interpretation}</p>"
        f"<table>{summary_rows}</table>"
        "<h2>Tasks</h2><table><tr><th>#</th><th>Task</th><th>Source</th><th>Status</th>"
        "<th>Acceptance</th><th>Preservation</th><th>Tool calls</th></tr>"
        f"{task_rows}</table></html>\n",
        encoding="utf-8",
    )
