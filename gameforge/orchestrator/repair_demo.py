from __future__ import annotations

import difflib
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from gameforge.adapters.unity import UnityBatchGateway, UnityToolError
from gameforge.checkpoints.git import inspect_checkpoint
from gameforge.config import EXPECTED_UNITY_VERSION, configured_unity_editor
from gameforge.orchestrator.diagnostics import diagnose_compiler_log, require_evidence
from gameforge.orchestrator.executor import _require_test_suite_passed, _write_changes_diff
from gameforge.orchestrator.planner import create_deterministic_plan
from gameforge.orchestrator.policy import ToolPolicy
from gameforge.orchestrator.repair_loop import RepairBudget, apply_text_replacement
from gameforge.orchestrator.state_machine import ALLOWED_TRANSITIONS, RunState, StateMachine
from gameforge.reporting.html_report import write_html_report
from gameforge.schemas.diagnostic import FailureCategory, TextReplacement
from gameforge.schemas.evidence import Evidence, EvidenceKind
from gameforge.schemas.game_spec import GameSpec

_TARGET = Path("unity/ArenaTemplate/Assets/Scripts/GameStatus.cs")
_UNITY_TARGET = "Assets/Scripts/GameStatus.cs"
_APPROVED_ROOT = Path("unity/ArenaTemplate/Assets/Scripts")
_ORIGINAL_ANCHOR = "        Won,\n        Lost\n"
_FAULTY_ANCHOR = (
    "        Won,\n"
    "        Lost,\n"
    "        IntentionalCompileFailure = MissingSymbol.Value // GAMEFORGE_FAULT\n"
)


@dataclass(frozen=True)
class RepairDemoResult:
    run_id: str
    run_directory: Path
    summary: dict[str, object]


def _write_json(path: Path, payload: object) -> None:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _json_default(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _git_is_clean(root: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return not completed.stdout.strip()


def _unified_diff(path: Path, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path.as_posix()}",
            tofile=f"b/{path.as_posix()}",
        )
    )


def run_repair_demo(
    root: Path,
    spec_path: Path,
    project_path: Path,
    runs_path: Path,
) -> RepairDemoResult:
    root = root.resolve()
    spec = GameSpec.from_yaml(spec_path)
    editor = configured_unity_editor()
    if editor is None:
        raise ValueError("the pinned Unity editor is not installed")
    if not _git_is_clean(root):
        raise ValueError("repair demo requires a clean Git worktree to protect user changes")

    target = (root / _TARGET).resolve()
    if not target.is_file():
        raise ValueError(f"repair demo target is missing: {_TARGET}")
    original_source = target.read_text(encoding="utf-8")
    if original_source.count(_ORIGINAL_ANCHOR) != 1 or "GAMEFORGE_FAULT" in original_source:
        raise ValueError("repair demo target does not match the verified baseline")
    faulty_source = original_source.replace(_ORIGINAL_ANCHOR, _FAULTY_ANCHOR, 1)

    started_at = datetime.now(UTC)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ-repair-demo")
    run_directory = runs_path / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    trace_path = run_directory / "trace.jsonl"

    def append_trace(event: dict[str, object]) -> None:
        payload = {"timestamp": datetime.now(UTC).isoformat(), "mode": "repair-demo", **event}
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")

    def trace_transition(previous: RunState, target_state: RunState, reason: str | None) -> None:
        append_trace(
            {
                "event": "state_transition",
                "from": previous.value,
                "to": target_state.value,
                "reason": reason,
            }
        )

    machine = StateMachine(on_transition=trace_transition)
    policy = ToolPolicy(root)
    gateway = UnityBatchGateway(editor, project_path, run_directory)
    budget = RepairBudget(maximum=3, max_seconds=900, max_cost_usd=0.0)
    acceptance_results: list[dict[str, object]] = []
    checkpoint = inspect_checkpoint(root)
    summary: dict[str, object] = {
        "run_id": run_id,
        "status": "RUNNING",
        "mode": "repair-demo",
        "specification": str(spec_path),
        "unity_project": str(project_path),
        "unity_version": EXPECTED_UNITY_VERSION,
        "git_checkpoint": checkpoint.commit,
        "git_dirty_at_start": checkpoint.dirty,
        "spec_validation": "PASS",
        "fault_injected": "NOT_RUN",
        "fault_detected": "NOT_RUN",
        "diagnosis": "NOT_RUN",
        "repair": "NOT_RUN",
        "retest_compile": "NOT_RUN",
        "structure_tests": "NOT_RUN",
        "play_mode_tests": "NOT_RUN",
        "build": "NOT_RUN",
        "smoke_test": "NOT_RUN",
        "repair_attempts": 0,
        "acceptance_passed": 0,
        "acceptance_total": len(spec.acceptance),
        "api_cost_usd": 0.0,
    }

    def invoke(tool: str, operation: Callable[[], object]) -> object:
        policy.require_tool_allowed(machine.state, tool)
        append_trace({"event": "tool_started", "tool": tool})
        try:
            result = operation()
        except Exception as error:
            append_trace(
                {
                    "event": "tool_failed",
                    "tool": tool,
                    "error_type": type(error).__name__,
                    "detail": str(error),
                }
            )
            raise
        append_trace(
            {
                "event": "tool_completed",
                "tool": tool,
                "log": str(getattr(result, "log_path", "")),
                "artifacts": [str(path) for path in getattr(result, "artifacts", ())],
            }
        )
        return result

    try:
        machine.transition(RunState.VALIDATE_SPEC, "GameSpec schema validation passed")
        machine.transition(RunState.CREATE_CHECKPOINT, "record clean Git checkpoint")
        _write_json(run_directory / "checkpoint.json", checkpoint.__dict__)

        machine.transition(
            RunState.INSPECT_PROJECT,
            "verify baseline compiles before fault injection",
        )
        invoke("health_check", gateway.health_check)

        machine.transition(RunState.PLAN, "write deterministic repair demonstration plan")
        plan = create_deterministic_plan(spec)
        _write_json(run_directory / "plan.json", plan)

        machine.transition(RunState.IMPLEMENT, "inject one declared C# compiler fault")
        injection = TextReplacement(
            path=_TARGET.as_posix(),
            expected=_ORIGINAL_ANCHOR,
            replacement=_FAULTY_ANCHOR,
            rationale="Inject one bounded compiler fault to verify the repair loop.",
            evidence_sources=(str(run_directory / "checkpoint.json"),),
        )
        injection_result = invoke(
            "apply_code_patch",
            lambda: apply_text_replacement(
                root,
                injection,
                allowed_roots=(_APPROVED_ROOT,),
            ),
        )
        _write_json(run_directory / "fault-injection.json", injection_result)
        (run_directory / "fault-injection.diff").write_text(
            _unified_diff(_TARGET, original_source, faulty_source),
            encoding="utf-8",
        )
        summary["fault_injected"] = "PASS"

        machine.transition(RunState.COMPILE, "run Unity compilation and require the fault to fail")
        try:
            invoke("wait_for_compilation", gateway.health_check)
        except UnityToolError:
            failure_log = run_directory / "logs" / "compile-failure.log"
            shutil.copy2(run_directory / "logs" / "health_check.log", failure_log)
            summary["fault_detected"] = "PASS"
        else:
            raise RuntimeError("injected compiler fault was not detected by Unity")

        machine.transition(RunState.DIAGNOSE, "parse the concrete Unity compiler evidence")
        diagnosis = diagnose_compiler_log(failure_log)
        matching = [
            finding
            for finding in diagnosis.findings
            if finding.category is FailureCategory.COMPILATION
            and finding.location is not None
            and finding.location.path == _UNITY_TARGET
            and "MissingSymbol" in finding.message
        ]
        if len(matching) != 1:
            raise RuntimeError("compiler evidence did not uniquely identify the injected fault")
        proposal = TextReplacement(
            path=_TARGET.as_posix(),
            expected=_FAULTY_ANCHOR,
            replacement=_ORIGINAL_ANCHOR,
            rationale=(
                f"{matching[0].code} at {_UNITY_TARGET}:{matching[0].location.line} "
                "identifies the injected undefined symbol; remove only that enum member."
            ),
            evidence_sources=(str(failure_log.resolve()),),
        )
        diagnosis = diagnosis.model_copy(update={"proposed_repair": proposal})
        evidence = [
            Evidence(
                kind=EvidenceKind.CONSOLE,
                source=str(failure_log.resolve()),
                payload={"finding_count": len(diagnosis.findings)},
                artifact_path=str(failure_log),
            )
        ]
        require_evidence(diagnosis, evidence)
        _write_json(run_directory / "diagnosis.json", diagnosis)
        summary["diagnosis"] = "PASS"

        machine.transition(RunState.REPAIR, "apply the single evidence-backed minimal repair")
        attempt = budget.consume()
        repair_result = invoke(
            "apply_code_patch",
            lambda: apply_text_replacement(
                root,
                proposal,
                allowed_roots=(_APPROVED_ROOT,),
            ),
        )
        _write_json(
            run_directory / "repair-actions.json",
            [{"attempt": attempt, "proposal": proposal, "result": repair_result}],
        )
        (run_directory / "repair.diff").write_text(
            _unified_diff(_TARGET, faulty_source, original_source),
            encoding="utf-8",
        )
        summary["repair"] = "PASS"
        summary["repair_attempts"] = attempt

        machine.transition(RunState.RETEST, "rerun compile and both Unity test layers")
        invoke("wait_for_compilation", gateway.health_check)
        summary["retest_compile"] = "PASS"
        edit_result = invoke("run_edit_mode_tests", lambda: gateway.run_tests("EditMode"))
        _require_test_suite_passed(edit_result.artifacts[0])
        summary["structure_tests"] = "PASS"
        play_result = invoke("run_play_mode_tests", lambda: gateway.run_tests("PlayMode"))
        _require_test_suite_passed(play_result.artifacts[0])
        summary["play_mode_tests"] = "PASS"
        acceptance_results = [
            {
                "id": condition.id,
                "status": "PASS",
                "reason": "covered by the passing post-repair Play Mode suite",
            }
            for condition in spec.acceptance
        ]
        summary["acceptance_passed"] = len(spec.acceptance)

        machine.transition(RunState.BUILD, "build the repaired macOS standalone player")
        invoke("build_player", gateway.build_macos)
        summary["build"] = "PASS"

        machine.transition(
            RunState.BUILD_SMOKE_TEST,
            "launch the repaired player and capture structured and visual evidence",
        )
        invoke("launch_build_smoke_test", gateway.launch_build_smoke_test)
        summary["smoke_test"] = "PASS"
        summary["status"] = "PASS"
        machine.transition(RunState.REPORT, "repair loop and all post-repair gates passed")
    except (OSError, ValueError, RuntimeError, ElementTree.ParseError) as error:
        summary["status"] = "FAIL"
        summary["failure"] = str(error)
        append_trace(
            {
                "event": "failure",
                "error_type": type(error).__name__,
                "detail": str(error),
            }
        )
        if RunState.FAILED in ALLOWED_TRANSITIONS[machine.state]:
            machine.transition(RunState.FAILED, str(error))
            machine.transition(RunState.REPORT, "write truthful repair failure report")
    finally:
        current_source = target.read_text(encoding="utf-8")
        if current_source == faulty_source:
            emergency = TextReplacement(
                path=_TARGET.as_posix(),
                expected=_FAULTY_ANCHOR,
                replacement=_ORIGINAL_ANCHOR,
                rationale="Restore the exact pre-run source after an interrupted repair demo.",
                evidence_sources=(str(run_directory / "checkpoint.json"),),
            )
            apply_text_replacement(root, emergency, allowed_roots=(_APPROVED_ROOT,))
            current_source = target.read_text(encoding="utf-8")
            append_trace({"event": "emergency_restore", "path": _TARGET.as_posix()})
        summary["source_restored"] = current_source == original_source
        summary["git_clean_after_run"] = _git_is_clean(root)
        if not summary["source_restored"] or not summary["git_clean_after_run"]:
            summary["status"] = "FAIL"
            summary["failure"] = "repair demo did not restore the exact clean checkpoint state"

    ended_at = datetime.now(UTC)
    summary["started_at"] = started_at.isoformat()
    summary["ended_at"] = ended_at.isoformat()
    summary["duration_seconds"] = round((ended_at - started_at).total_seconds(), 3)
    if not acceptance_results:
        acceptance_results = [
            {
                "id": condition.id,
                "status": "NOT_RUN",
                "reason": "repair demo stopped before post-repair acceptance completed",
            }
            for condition in spec.acceptance
        ]
    _write_json(run_directory / "acceptance-results.json", acceptance_results)
    _write_json(
        run_directory / "cost.json",
        {
            "currency": "USD",
            "total": budget.cost_usd,
            "input_tokens": 0,
            "output_tokens": 0,
        },
    )
    _write_changes_diff(root, run_directory / "changes.diff")
    _write_json(run_directory / "summary.json", summary)
    write_html_report(run_directory / "report.html", summary, acceptance_results)
    if machine.state is RunState.REPORT:
        machine.transition(RunState.COMPLETE, "repair demo artifacts written")
    return RepairDemoResult(run_id, run_directory, summary)
