from __future__ import annotations

import hashlib
import html
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gameforge.adapters.llm import LanguageModel
from gameforge.adapters.unity import UnityBatchGateway
from gameforge.adapters.unity_harness import UnityHarnessEngineAdapter, UnityNativeEvaluator
from gameforge.benchmarking.heterogeneous import (
    HeterogeneousBenchmarkAdapter,
    TaskCatalog,
)
from gameforge.benchmarking.oracle import DeterministicOracleModel
from gameforge.config import configured_unity_editor
from gameforge.harness.contracts import (
    EngineName,
    EvaluationSpec,
    GateOutcome,
    GateStatus,
    HarnessProfile,
    ModelRunSpec,
    RunSpec,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.run_factory import create_run_spec
from gameforge.harness.runner import HarnessRunner


@dataclass(frozen=True)
class HeterogeneousBenchmarkRun:
    benchmark_id: str
    directory: Path
    summary: dict[str, object]


def run_heterogeneous_benchmark(
    *,
    root: Path,
    catalog_path: Path,
    template_project: Path,
    model_profile: ModelProfile,
    adapter_mode: str,
    task_ids: tuple[str, ...] = (),
) -> HeterogeneousBenchmarkRun:
    if adapter_mode not in {"control", "model"}:
        raise ValueError("adapter_mode must be control or model")
    editor = configured_unity_editor()
    if editor is None:
        raise ValueError("the pinned Unity editor is not installed")

    catalog = TaskCatalog.from_yaml(catalog_path)
    catalog.validate_template(template_project)
    adapter = HeterogeneousBenchmarkAdapter(catalog, template_project)
    selected = task_ids or adapter.list_tasks()
    if len(selected) != len(set(selected)):
        raise ValueError("selected benchmark task IDs must be unique")
    for task_id in selected:
        catalog.task(task_id)

    started_at = datetime.now(UTC)
    benchmark_id = started_at.strftime(
        f"%Y%m%dT%H%M%SZ-heterogeneous-{adapter_mode}-{len(selected)}"
    )
    directory = root / "runs" / "benchmarks" / benchmark_id
    workspaces = directory / "workspaces"
    output_root = directory / "runs"
    workspaces.mkdir(parents=True, exist_ok=False)
    output_root.mkdir()
    _copy_shared_unity_packages(template_project, workspaces)

    records: list[dict[str, object]] = []
    for index, task_id in enumerate(selected, start=1):
        fixture_task = catalog.task(task_id)
        workspace = workspaces / task_id
        task = adapter.load_task(task_id, workspace)
        adapter.prepare_workspace(task, workspace)
        evaluation = EvaluationSpec(
            required_gates=("specification", "preservation", *fixture_task.required_gates),
            evaluator_version="unity-native-v1",
        )
        run_spec = create_run_spec(
            task,
            model_profile,
            engine=EngineName.UNITY,
            engine_version="6000.3.20f1",
            profile=HarnessProfile.AUGMENTED,
            evaluation=evaluation,
        )
        preflight_directory = directory / "preflight" / task_id
        preflight_gateway = UnityBatchGateway(editor, workspace, preflight_directory)
        preflight = _verify_fault_preflight(
            run_spec,
            fixture_task.failure_class.value,
            preflight_gateway,
        )
        preflight_directory.mkdir(parents=True, exist_ok=True)
        _write_json(
            preflight_directory / "fault-preflight.json",
            [gate.model_dump(mode="json") for gate in preflight],
        )
        if fixture_task.fixture.regenerate_scene:
            _remove_generated_scene(workspace)
        model: LanguageModel
        if adapter_mode == "control":
            model = DeterministicOracleModel(fixture_task)
            run_spec = run_spec.model_copy(
                update={
                    "model": ModelRunSpec(
                        provider="deterministic-control",
                        model="fixture-repair-v1",
                        parameters={"policy": "fixture-repair-v1", "temperature": 0},
                        profile_sha256=_control_profile_hash(catalog, fixture_task.id),
                    )
                }
            )
        else:
            model = model_profile.build_adapter(root)

        run_directory = output_root / run_spec.run_id
        gateway = UnityBatchGateway(editor, workspace, run_directory)
        engine = UnityHarnessEngineAdapter(gateway, task.editable_paths)
        evaluator = UnityNativeEvaluator(gateway)
        print(
            f"Heterogeneous task {index}/{len(selected)} started: {task_id}",
            flush=True,
        )
        run = HarnessRunner(model, engine, evaluator).execute(run_spec, output_root)
        record = {
            "index": index,
            "task_id": task_id,
            "failure_class": fixture_task.failure_class.value,
            "status": run.result.status.value,
            "run_directory": str(run.directory),
            "workspace": str(workspace),
            "input_tokens": run.result.input_tokens,
            "output_tokens": run.result.output_tokens,
            "cost_usd": run.result.cost_usd,
            "tool_calls": run.result.tool_calls,
            "failure_category": run.result.failure_category,
            "failure_detail": run.result.failure_detail,
            "fault_preflight": "PASS",
        }
        records.append(record)
        _append_jsonl(directory / "runs.jsonl", record)
        print(
            f"Heterogeneous task {index}/{len(selected)} completed: {record['status']}",
            flush=True,
        )

    summary = _aggregate(benchmark_id, adapter_mode, catalog, records, started_at)
    _write_json(directory / "summary.json", summary)
    _write_json(directory / "runs.json", records)
    _write_report(directory / "report.html", summary, records)
    return HeterogeneousBenchmarkRun(benchmark_id, directory, summary)


def _control_profile_hash(catalog: TaskCatalog, task_id: str) -> str:
    payload = f"{catalog.name}:{catalog.version}:deterministic-control:{task_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _copy_shared_unity_packages(template_project: Path, workspaces: Path) -> None:
    agent_bridge = template_project.resolve().parent / "AgentBridge"
    if not (agent_bridge / "package.json").is_file():
        raise ValueError(f"required local Unity package is missing: {agent_bridge}")
    shutil.copytree(agent_bridge, workspaces / "AgentBridge")


def _verify_fault_preflight(
    run_spec: RunSpec,
    failure_class: str,
    gateway: UnityBatchGateway,
) -> tuple[GateOutcome, ...]:
    required = tuple(
        gate
        for gate in run_spec.evaluation.required_gates
        if gate not in {"specification", "preservation"}
    )
    preflight_spec = run_spec.model_copy(
        update={"evaluation": run_spec.evaluation.model_copy(update={"required_gates": required})}
    )
    outcomes = UnityNativeEvaluator(gateway).evaluate(
        preflight_spec,
        gateway.project,
    )
    target_gate = {
        "compilation": "compilation",
        "structure": "structure",
        "gameplay": "gameplay",
        "build_launch": required[-1],
    }[failure_class]
    failed = [outcome.gate for outcome in outcomes if outcome.status is GateStatus.FAIL]
    if failed != [target_gate]:
        statuses = {outcome.gate: outcome.status.value for outcome in outcomes}
        raise ValueError(
            f"injected fixture did not fail only its target gate {target_gate}: {statuses}"
        )
    return outcomes


def _remove_generated_scene(workspace: Path) -> None:
    scene = workspace / "Assets" / "Scenes" / "Arena.unity"
    scene.unlink(missing_ok=True)
    scene.with_suffix(".unity.meta").unlink(missing_ok=True)


def _aggregate(
    benchmark_id: str,
    adapter_mode: str,
    catalog: TaskCatalog,
    records: list[dict[str, object]],
    started_at: datetime,
) -> dict[str, object]:
    ended_at = datetime.now(UTC)
    passed = sum(record["status"] == "PASS" for record in records)
    totals = Counter(str(record["failure_class"]) for record in records)
    passes = Counter(
        str(record["failure_class"]) for record in records if record["status"] == "PASS"
    )
    return {
        "benchmark_id": benchmark_id,
        "benchmark_name": catalog.name,
        "benchmark_version": catalog.version,
        "harness_profile": "augmented",
        "adapter_mode": adapter_mode,
        "control_result": adapter_mode == "control",
        "tasks_completed": len(records),
        "tasks_passed": passed,
        "end_to_end_success_rate": round(passed / len(records), 4) if records else 0,
        "success_rate_by_failure_class": {
            category: round(passes[category] / total, 4)
            for category, total in sorted(totals.items())
        },
        "input_tokens": sum(int(record["input_tokens"]) for record in records),
        "output_tokens": sum(int(record["output_tokens"]) for record in records),
        "cost_usd": round(sum(float(record["cost_usd"]) for record in records), 8),
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "wall_duration_seconds": round((ended_at - started_at).total_seconds(), 3),
    }


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
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    task_rows = "".join(
        "<tr>"
        f"<td>{record['index']}</td>"
        f"<td>{html.escape(str(record['task_id']))}</td>"
        f"<td>{html.escape(str(record['failure_class']))}</td>"
        f"<td>{html.escape(str(record['status']))}</td>"
        f"<td>{record['tool_calls']}</td>"
        f"<td>{record['cost_usd']}</td>"
        "</tr>"
        for record in records
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Heterogeneous game harness benchmark</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;margin:1rem 0 2rem}}th,td{{border:1px solid #ccd2dc;
padding:.55rem;text-align:left}}th{{background:#f2f5f9}}</style></head><body>
<h1>Heterogeneous harness benchmark</h1>
<p>Control and online-model results are intentionally labeled and must not be merged.</p>
<h2>Summary</h2><table>{summary_rows}</table>
<h2>Tasks</h2><table><thead><tr><th>#</th><th>Task</th><th>Class</th><th>Status</th>
<th>Tools</th><th>Cost (USD)</th></tr></thead><tbody>{task_rows}</tbody></table>
</body></html>"""
    path.write_text(document, encoding="utf-8")
