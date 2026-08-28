from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from gameforge.adapters.llm import CostLedger, MockLanguageModel
from gameforge.adapters.unity import UnityBatchGateway, UnityToolError, UnityToolErrorCode
from gameforge.checkpoints.git import inspect_checkpoint
from gameforge.config import EXPECTED_UNITY_VERSION, configured_unity_editor
from gameforge.orchestrator.planner import create_deterministic_plan
from gameforge.orchestrator.state_machine import RunState, StateMachine
from gameforge.reporting.html_report import write_html_report
from gameforge.schemas.game_spec import GameSpec
from gameforge.schemas.model import PlanningDecision


@dataclass(frozen=True)
class MockRunResult:
    run_id: str
    run_directory: Path
    summary: dict[str, object]


@dataclass(frozen=True)
class BaselineRunResult:
    run_id: str
    run_directory: Path
    summary: dict[str, object]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_mock(spec_path: Path, project_path: Path, runs_path: Path) -> MockRunResult:
    spec = GameSpec.from_yaml(spec_path)
    timestamp = datetime.now(UTC)
    run_id = timestamp.strftime("%Y%m%dT%H%M%SZ-mock")
    run_directory = runs_path / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    trace_path = run_directory / "trace.jsonl"

    def trace(previous: RunState, target: RunState, reason: str | None) -> None:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": "state_transition",
            "from": previous.value,
            "to": target.value,
            "reason": reason,
            "mode": "mock",
        }
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")

    machine = StateMachine(on_transition=trace)
    machine.transition(RunState.VALIDATE_SPEC, "GameSpec schema validation passed")
    machine.transition(RunState.CREATE_CHECKPOINT, "mock checkpoint recorded without mutation")
    machine.transition(RunState.INSPECT_PROJECT, "project path recorded; Unity was not invoked")
    machine.transition(RunState.PLAN, "deterministic planner selected")
    system_prompt = (
        "Return one strict JSON planning decision. You cannot call tools or access credentials."
    )
    user_prompt = (
        f"Game {spec.game.id} has {len(spec.acceptance)} acceptance conditions. "
        "Paid execution is disabled; select the verified deterministic plan."
    )
    model = MockLanguageModel()
    ledger = CostLedger(maximum_usd=0.0)
    with trace_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event": "model_started",
                    "mode": "mock",
                    "provider": "mock",
                    "model": "mock",
                },
                sort_keys=True,
            )
            + "\n"
        )
    model_response = model.complete(system=system_prompt, prompt=user_prompt)
    ledger.record(model_response)
    decision = PlanningDecision.from_json(model_response.text)
    with trace_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event": "model_completed",
                    "mode": "mock",
                    **model_response.trace_record(system=system_prompt, prompt=user_prompt),
                },
                sort_keys=True,
            )
            + "\n"
        )
    _write_json(run_directory / "model-decision.json", decision.model_dump(mode="json"))
    _write_json(
        run_directory / "model-call.json",
        model_response.trace_record(system=system_prompt, prompt=user_prompt),
    )
    plan = create_deterministic_plan(spec)
    _write_json(run_directory / "plan.json", plan.model_dump(mode="json"))
    machine.transition(RunState.REPORT, "mock mode intentionally skips implementation and gates")

    acceptance_results = [
        {
            "id": condition.id,
            "status": "NOT_RUN",
            "reason": "mock mode does not enter Unity Play Mode",
        }
        for condition in spec.acceptance
    ]
    _write_json(run_directory / "acceptance-results.json", acceptance_results)
    _write_json(run_directory / "cost.json", ledger.to_dict())

    summary: dict[str, object] = {
        "run_id": run_id,
        "status": "MOCK_VALIDATED",
        "mode": "mock",
        "specification": str(spec_path),
        "unity_project": str(project_path),
        "spec_validation": "PASS",
        "plan_generation": "PASS",
        "unity_compile": "NOT_RUN",
        "structure_tests": "NOT_RUN",
        "play_mode_tests": "NOT_RUN",
        "build": "NOT_RUN",
        "smoke_test": "NOT_RUN",
        "acceptance_passed": 0,
        "acceptance_total": len(spec.acceptance),
        "model_provider": model_response.provider,
        "model": model_response.model,
        "model_requests": len(ledger.responses),
        "api_cost_usd": ledger.total_usd,
    }
    _write_json(run_directory / "summary.json", summary)
    write_html_report(run_directory / "report.html", summary, acceptance_results)
    machine.transition(RunState.COMPLETE, "mock artifacts written")
    return MockRunResult(run_id=run_id, run_directory=run_directory, summary=summary)


def run_baseline(
    root: Path,
    spec_path: Path,
    project_path: Path,
    runs_path: Path,
) -> BaselineRunResult:
    spec = GameSpec.from_yaml(spec_path)
    editor = configured_unity_editor()
    if editor is None:
        raise ValueError("the pinned Unity editor is not installed")

    started_at = datetime.now(UTC)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ-baseline")
    run_directory = runs_path / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    trace_path = run_directory / "trace.jsonl"

    def append_trace(event: dict[str, object]) -> None:
        payload = {"timestamp": datetime.now(UTC).isoformat(), "mode": "baseline", **event}
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")

    def trace_transition(previous: RunState, target: RunState, reason: str | None) -> None:
        append_trace(
            {
                "event": "state_transition",
                "from": previous.value,
                "to": target.value,
                "reason": reason,
            }
        )

    def invoke(tool: str, operation: Callable[[], object]) -> object:
        append_trace({"event": "tool_started", "tool": tool})
        result = operation()
        append_trace(
            {
                "event": "tool_completed",
                "tool": tool,
                "log": str(result.log_path),
                "artifacts": [str(path) for path in result.artifacts],
            }
        )
        return result

    machine = StateMachine(on_transition=trace_transition)
    gateway = UnityBatchGateway(editor, project_path, run_directory)
    acceptance_results: list[dict[str, object]] = []
    summary: dict[str, object] = {
        "run_id": run_id,
        "status": "RUNNING",
        "mode": "baseline",
        "specification": str(spec_path),
        "unity_project": str(project_path),
        "unity_version": EXPECTED_UNITY_VERSION,
        "spec_validation": "PASS",
        "plan_generation": "NOT_RUN",
        "unity_compile": "NOT_RUN",
        "structure_tests": "NOT_RUN",
        "play_mode_tests": "NOT_RUN",
        "build": "NOT_RUN",
        "smoke_test": "NOT_RUN",
        "acceptance_passed": 0,
        "acceptance_total": len(spec.acceptance),
        "api_cost_usd": 0.0,
    }

    try:
        machine.transition(RunState.VALIDATE_SPEC, "GameSpec schema validation passed")
        machine.transition(RunState.CREATE_CHECKPOINT, "record current Git state")
        checkpoint = inspect_checkpoint(root)
        summary["git_checkpoint"] = checkpoint.commit
        summary["git_dirty_at_start"] = checkpoint.dirty

        machine.transition(RunState.INSPECT_PROJECT, "run typed bridge health check")
        invoke("health_check", gateway.health_check)

        machine.transition(RunState.PLAN, "compile deterministic baseline task DAG")
        plan = create_deterministic_plan(spec)
        _write_json(run_directory / "plan.json", plan.model_dump(mode="json"))
        summary["plan_generation"] = "PASS"

        machine.transition(RunState.IMPLEMENT, "create arena through approved editor method")
        invoke("create_arena", gateway.create_arena)

        machine.transition(RunState.COMPILE, "verify compiled bridge after scene generation")
        invoke("wait_for_compilation", gateway.health_check)
        summary["unity_compile"] = "PASS"

        machine.transition(RunState.STRUCTURE_TEST, "run Edit Mode tests")
        edit_result = invoke("run_edit_mode_tests", lambda: gateway.run_tests("EditMode"))
        _require_test_suite_passed(edit_result.artifacts[0])
        summary["structure_tests"] = "PASS"

        machine.transition(RunState.PLAY_TEST, "run Play Mode acceptance tests")
        play_result = invoke("run_play_mode_tests", lambda: gateway.run_tests("PlayMode"))
        _require_test_suite_passed(play_result.artifacts[0])
        summary["play_mode_tests"] = "PASS"
        acceptance_results = [
            {
                "id": condition.id,
                "status": "PASS",
                "reason": "covered by the passing Play Mode suite",
            }
            for condition in spec.acceptance
        ]
        summary["acceptance_passed"] = len(spec.acceptance)

        machine.transition(RunState.BUILD, "build macOS standalone player")
        invoke("build_player", gateway.build_macos)
        summary["build"] = "PASS"

        machine.transition(RunState.BUILD_SMOKE_TEST, "launch built player and capture evidence")
        invoke("launch_build_smoke_test", gateway.launch_build_smoke_test)
        summary["smoke_test"] = "PASS"
        summary["status"] = "PASS"
        machine.transition(RunState.REPORT, "all deterministic baseline gates passed")
    except (UnityToolError, ValueError, OSError, ElementTree.ParseError) as error:
        summary["status"] = "FAIL"
        summary["failure"] = str(error)
        append_trace(
            {
                "event": "failure",
                "error_type": type(error).__name__,
                "detail": str(error),
            }
        )
        machine.transition(RunState.FAILED, str(error))
        machine.transition(RunState.REPORT, "write truthful failure report")

    ended_at = datetime.now(UTC)
    summary["started_at"] = started_at.isoformat()
    summary["ended_at"] = ended_at.isoformat()
    summary["duration_seconds"] = round((ended_at - started_at).total_seconds(), 3)
    if not acceptance_results:
        acceptance_results = [
            {
                "id": condition.id,
                "status": "NOT_RUN",
                "reason": "baseline stopped before Play Mode acceptance completed",
            }
            for condition in spec.acceptance
        ]
    _write_json(run_directory / "acceptance-results.json", acceptance_results)
    _write_json(
        run_directory / "cost.json",
        {"currency": "USD", "total": 0.0, "input_tokens": 0, "output_tokens": 0},
    )
    _write_changes_diff(root, run_directory / "changes.diff")
    _write_json(run_directory / "summary.json", summary)
    write_html_report(run_directory / "report.html", summary, acceptance_results)
    machine.transition(RunState.COMPLETE, "baseline artifacts written")
    return BaselineRunResult(run_id, run_directory, summary)


def _require_test_suite_passed(path: Path) -> None:
    root = ElementTree.parse(path).getroot()
    result = root.attrib.get("result", "").lower()
    failed = int(root.attrib.get("failed", "0"))
    if result not in {"passed", "pass"} or failed:
        raise UnityToolError(
            code=UnityToolErrorCode.PROCESS_FAILED,
            detail=(
                f"Unity test suite failed: result={result or 'unknown'}, "
                f"failed={failed}; see {path}"
            ),
        )


def _write_changes_diff(root: Path, destination: Path) -> None:
    tracked = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    fragments = [tracked.stdout]
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8"))
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
            continue
        created = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", str(relative)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if created.returncode in {0, 1}:
            fragments.append(created.stdout)
    destination.write_text("".join(fragments), encoding="utf-8")
