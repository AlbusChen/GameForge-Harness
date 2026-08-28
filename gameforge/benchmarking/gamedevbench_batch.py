from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gameforge.benchmarking.gamedevbench import (
    GAMEDEVBENCH_COMMIT,
    GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
    GameDevBenchOfficialEvaluator,
    GameDevBenchSource,
    _official_gate_status,
    run_gamedevbench_harness,
    run_gamedevbench_minimal_harness,
)
from gameforge.harness.contracts import GateOutcome, GateStatus, RunResult, RunSpec, RunStatus
from gameforge.harness.game_tasks import RuntimeProtocol
from gameforge.harness.preservation import (
    PreservationReport,
    ProjectSnapshot,
    compare_snapshots,
    snapshot_project,
)
from gameforge.harness.runner import HarnessRunner
from gameforge.telemetry.subscription import capture_subscription_snapshot, quota_delta


@dataclass(frozen=True)
class GameDevBenchBatchRun:
    directory: Path
    summary: dict[str, object]


def adjudicate_inconclusive_startup_timeouts(
    batch_directory: Path,
    *,
    benchmark_root: Path,
    godot: Path,
) -> list[str]:
    """Evaluate candidates blocked only by a diagnostic-free startup timeout.

    This incident path never invokes the model or mutates the retained candidate. The
    pinned official evaluator runs against a temporary copy, and preservation is
    recalculated from the original pre-run snapshot.
    """
    progress_path = batch_directory / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    records = progress.get("tasks")
    if not isinstance(records, list):
        raise ValueError("batch progress is missing task records")
    adjudicated: list[str] = []
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("status") != "BLOCKED"
            or record.get("failure_detail") != "public validation failed after the bounded repair"
        ):
            continue
        run_directory_value = record.get("run_directory")
        if not isinstance(run_directory_value, str):
            continue
        run_directory = Path(run_directory_value)
        if not _only_inconclusive_startup_timeouts(run_directory):
            continue

        run_spec = RunSpec.model_validate_json(
            (run_directory / "run-spec.json").read_text(encoding="utf-8")
        )
        result = RunResult.model_validate_json(
            (run_directory / "result.json").read_text(encoding="utf-8")
        )
        before = ProjectSnapshot.model_validate_json(
            (run_directory / "project-before.json").read_text(encoding="utf-8")
        )
        workspace = run_spec.task.project_path
        preservation = compare_snapshots(
            before,
            snapshot_project(workspace),
            run_spec.task.editable_paths,
        )
        source = GameDevBenchSource(
            benchmark_root,
            str(record["task_id"]),
            run_spec.task.editable_paths,
        )
        evaluator = GameDevBenchOfficialEvaluator(source, godot, run_directory)
        evaluation_started = time.monotonic()
        evaluated = {gate.gate: gate for gate in evaluator.evaluate(run_spec, workspace)}
        evaluation_seconds = time.monotonic() - evaluation_started
        preservation_gate = GateOutcome(
            gate="preservation",
            status=GateStatus.PASS if preservation.passed else GateStatus.FAIL,
            detail=(
                "all unrelated files remained unchanged"
                if preservation.passed
                else "unrelated files changed"
            ),
            artifacts=("preservation.json",),
        )
        evaluated["preservation"] = preservation_gate
        gates = tuple(
            evaluated.get(
                gate,
                GateOutcome(
                    gate=gate,
                    status=GateStatus.NOT_RUN,
                    detail="required gate was not evaluated",
                ),
            )
            for gate in run_spec.evaluation.required_gates
        )
        status = (
            RunStatus.PASS
            if all(gate.status is GateStatus.PASS for gate in gates)
            else RunStatus.FAIL
        )
        official_status = evaluated["official"].status.value
        failure_category = None if status is RunStatus.PASS else "official_validation"
        failure_detail = (
            None
            if status is RunStatus.PASS
            else "one or more required gates failed after timeout adjudication"
        )
        adjudication = {
            "schema_version": 1,
            "raw_status": result.status.value,
            "raw_failure_detail": result.failure_detail,
            "startup_validation_evidence": _startup_timeout_evidence(run_directory),
            "adjudicated_status": status.value,
            "adjudicated_official_gate": official_status,
            "adjudicated_preservation_gate": preservation_gate.status.value,
            "evaluation_seconds": round(evaluation_seconds, 3),
            "reason": (
                "The public import succeeded without diagnostics; startup timeout was "
                "inconclusive, so the unchanged pinned evaluator remained authoritative."
            ),
            "model_rerun": False,
            "project_mutated": False,
        }
        _write_json_atomic(run_directory / "startup-timeout-adjudication.json", adjudication)
        _write_json_atomic(
            run_directory / "preservation.json", preservation.model_dump(mode="json")
        )
        _write_json_atomic(
            run_directory / "gate-results.json",
            [gate.model_dump(mode="json") for gate in gates],
        )
        updated = result.model_copy(
            update={
                "status": status,
                "gates": gates,
                "failure_category": failure_category,
                "failure_detail": failure_detail,
            }
        )
        updated = updated.model_copy(
            update={"artifacts": HarnessRunner._artifact_manifest(run_directory)}
        )
        _write_json_atomic(run_directory / "result.json", updated.model_dump(mode="json"))

        record["raw_status"] = record.get("status")
        record["raw_official_gate"] = record.get("official_gate")
        record["raw_preservation_gate"] = record.get("preservation_gate")
        record["status"] = status.value
        record["official_gate"] = official_status
        record["preservation_gate"] = preservation_gate.status.value
        record["failure_category"] = failure_category
        record["failure_detail"] = failure_detail
        record["duration_seconds"] = round(
            float(record.get("duration_seconds", 0)) + evaluation_seconds,
            3,
        )
        record["startup_timeout_adjudicated"] = True
        adjudicated.append(str(record.get("task_id")))

    progress["updated_at"] = datetime.now(UTC).isoformat()
    _write_json_atomic(progress_path, progress)
    _write_json_atomic(batch_directory / "summary.json", _summarize(records, len(records)))
    return adjudicated


def adjudicate_generated_import_metadata(batch_directory: Path) -> list[str]:
    """Correct preservation-only false failures caused by Godot `.import` rewrites."""
    progress_path = batch_directory / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    records = progress.get("tasks")
    if not isinstance(records, list):
        raise ValueError("batch progress is missing task records")
    adjudicated: list[str] = []
    for record in records:
        if not isinstance(record, dict) or record.get("preservation_gate") != "FAIL":
            continue
        run_directory_value = record.get("run_directory")
        if not isinstance(run_directory_value, str):
            continue
        run_directory = Path(run_directory_value)
        raw_report = PreservationReport.model_validate_json(
            (run_directory / "preservation.json").read_text(encoding="utf-8")
        )
        if not raw_report.unrelated_changed_paths or not all(
            Path(path).suffix.lower() == ".import" for path in raw_report.unrelated_changed_paths
        ):
            continue
        before = ProjectSnapshot.model_validate_json(
            (run_directory / "project-before.json").read_text(encoding="utf-8")
        )
        run_spec = RunSpec.model_validate_json(
            (run_directory / "run-spec.json").read_text(encoding="utf-8")
        )
        after = snapshot_project(before.project_path)
        corrected = compare_snapshots(before, after, run_spec.task.editable_paths)
        if not corrected.passed:
            continue

        result = RunResult.model_validate_json(
            (run_directory / "result.json").read_text(encoding="utf-8")
        )
        raw_status = result.status.value
        gates = tuple(
            GateOutcome(
                gate=gate.gate,
                status=GateStatus.PASS,
                detail="engine-generated .import sidecars are excluded from preservation",
                artifacts=("preservation.json", "preservation-adjudication.json"),
            )
            if gate.gate == "preservation"
            else gate
            for gate in result.gates
        )
        required = set(run_spec.evaluation.required_gates)
        status = (
            RunStatus.PASS
            if all(gate.status is GateStatus.PASS for gate in gates if gate.gate in required)
            and required <= {gate.gate for gate in gates}
            else RunStatus.FAIL
        )
        adjudication = {
            "schema_version": 1,
            "raw_status": raw_status,
            "raw_preservation_gate": "FAIL",
            "raw_unrelated_changed_paths": list(raw_report.unrelated_changed_paths),
            "adjudicated_status": status.value,
            "adjudicated_preservation_gate": "PASS",
            "reason": "Godot rewrote non-model-writable .import engine metadata.",
            "model_rerun": False,
            "project_mutated": False,
        }
        _write_json_atomic(run_directory / "preservation-adjudication.json", adjudication)
        _write_json_atomic(run_directory / "preservation.json", corrected.model_dump(mode="json"))
        _write_json_atomic(
            run_directory / "gate-results.json",
            [gate.model_dump(mode="json") for gate in gates],
        )
        updated = result.model_copy(
            update={
                "status": status,
                "gates": gates,
                "failure_category": None if status is RunStatus.PASS else result.failure_category,
                "failure_detail": None if status is RunStatus.PASS else result.failure_detail,
            }
        )
        updated = updated.model_copy(
            update={"artifacts": HarnessRunner._artifact_manifest(run_directory)}
        )
        _write_json_atomic(run_directory / "result.json", updated.model_dump(mode="json"))
        record["raw_status"] = record.get("status")
        record["raw_preservation_gate"] = "FAIL"
        record["status"] = status.value
        record["preservation_gate"] = "PASS"
        record["failure_category"] = (
            None if status is RunStatus.PASS else record.get("failure_category")
        )
        record["failure_detail"] = (
            None if status is RunStatus.PASS else record.get("failure_detail")
        )
        record["preservation_adjudicated"] = True
        adjudicated.append(str(record.get("task_id")))

    progress["updated_at"] = datetime.now(UTC).isoformat()
    _write_json_atomic(progress_path, progress)
    total = len(records)
    _write_json_atomic(batch_directory / "summary.json", _summarize(records, total))
    return adjudicated


def reconcile_retained_evaluator_timeouts(
    batch_directory: Path,
    *,
    root: Path,
) -> list[str]:
    """Recover usage and candidate evidence after a post-model evaluator timeout."""
    progress_path = batch_directory / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    records = progress.get("tasks")
    if not isinstance(records, list):
        raise ValueError("batch progress is missing task records")
    reconciled: list[str] = []
    changes: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        task_id = str(record.get("task_id", ""))
        run_directory = _retained_run_directory(root, task_id, record)
        if run_directory is None:
            continue
        agent_result_path = run_directory / "agent-result.json"
        run_spec_path = run_directory / "run-spec.json"
        if not agent_result_path.is_file() or not run_spec_path.is_file():
            continue
        agent_result = json.loads(agent_result_path.read_text(encoding="utf-8"))
        record["feedback_attempts"] = _count_feedback_attempts(run_directory)
        first_reconciliation = (
            record.get("status") == "INFRASTRUCTURE_ERROR"
            and record.get("failure_category") == "TimeoutExpired"
        )
        retained_inconclusive = (
            record.get("evaluator_timeout_reconciled") is True
            and record.get("official_gate") == GateStatus.NOT_RUN.value
        )
        if not first_reconciliation and not retained_inconclusive:
            continue

        run_spec = RunSpec.model_validate_json(run_spec_path.read_text(encoding="utf-8"))
        before = ProjectSnapshot.model_validate_json(
            (run_directory / "project-before.json").read_text(encoding="utf-8")
        )
        preservation = compare_snapshots(
            before,
            snapshot_project(run_spec.task.project_path),
            run_spec.task.editable_paths,
        )
        log_path = run_directory / "official-evaluator.log"
        evaluator_output = (
            log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        )
        official_status = _official_gate_status(
            evaluator_output,
            timed_out=True,
        )
        preservation_gate = GateOutcome(
            gate="preservation",
            status=GateStatus.PASS if preservation.passed else GateStatus.FAIL,
            detail=(
                "all unrelated files remained unchanged"
                if preservation.passed
                else "unrelated files changed"
            ),
            artifacts=("preservation.json",),
        )
        gates = (
            GateOutcome(
                gate="specification",
                status=GateStatus.PASS,
                detail="normalized task and pinned benchmark source are valid",
            ),
            GateOutcome(
                gate="official",
                status=official_status,
                detail=(
                    "pinned official validator passed"
                    if official_status is GateStatus.PASS
                    else "pinned official validator failed"
                    if official_status is GateStatus.FAIL
                    else "pinned official validator timed out without a verdict"
                ),
                artifacts=(("official-evaluator.log",) if log_path.is_file() else ()),
            ),
            preservation_gate,
        )
        status = (
            RunStatus.PASS
            if all(gate.status is GateStatus.PASS for gate in gates)
            else RunStatus.BLOCKED
            if official_status is GateStatus.NOT_RUN
            else RunStatus.FAIL
        )
        failure_category = (
            None
            if status is RunStatus.PASS
            else "evaluator_inconclusive"
            if status is RunStatus.BLOCKED
            else "evaluation"
        )
        failure_detail = (
            None
            if status is RunStatus.PASS
            else "official evaluator timed out without a retained verdict"
            if status is RunStatus.BLOCKED
            else "one or more required verification gates did not pass"
        )
        duration_seconds = float(record.get("duration_seconds", 0))
        result = RunResult(
            run_id=run_spec.run_id,
            status=status,
            started_at=run_spec.created_at,
            ended_at=run_spec.created_at + timedelta(seconds=duration_seconds),
            model=run_spec.model,
            gates=gates,
            input_tokens=int(agent_result.get("input_tokens", 0)),
            cached_input_tokens=int(agent_result.get("cached_input_tokens", 0)),
            output_tokens=int(agent_result.get("output_tokens", 0)),
            reasoning_output_tokens=int(agent_result.get("reasoning_output_tokens", 0)),
            cost_usd=float(agent_result.get("cost_usd", 0)),
            tool_calls=int(agent_result.get("tool_calls", 0)),
            failure_category=failure_category,
            failure_detail=failure_detail,
        )
        _write_json_atomic(
            run_directory / "preservation.json", preservation.model_dump(mode="json")
        )
        _write_json_atomic(
            run_directory / "gate-results.json",
            [gate.model_dump(mode="json") for gate in gates],
        )
        result = result.model_copy(
            update={"artifacts": HarnessRunner._artifact_manifest(run_directory)}
        )
        _write_json_atomic(run_directory / "result.json", result.model_dump(mode="json"))

        raw = dict(record)
        record.update(
            {
                "raw_status": raw.get("status"),
                "raw_failure_category": raw.get("failure_category"),
                "status": status.value,
                "official_gate": official_status.value,
                "preservation_gate": preservation_gate.status.value,
                "complexity_route": run_spec.model.parameters.get("complexity_route", "unknown"),
                "validation_attempts": int(agent_result.get("validation_attempts", 0)),
                "repair_attempts": int(agent_result.get("repair_attempts", 0)),
                "input_tokens": result.input_tokens,
                "cached_input_tokens": result.cached_input_tokens,
                "output_tokens": result.output_tokens,
                "reasoning_output_tokens": result.reasoning_output_tokens,
                "cost_usd": result.cost_usd,
                "tool_calls": result.tool_calls,
                "failure_category": failure_category,
                "failure_detail": failure_detail,
                "run_directory": str(run_directory),
                "evaluator_timeout_reconciled": True,
            }
        )
        reconciled.append(task_id)
        changes.append({"task_id": task_id, "before": raw, "after": dict(record)})

    progress["updated_at"] = datetime.now(UTC).isoformat()
    _write_json_atomic(progress_path, progress)
    _write_json_atomic(batch_directory / "summary.json", _summarize(records, len(records)))
    _write_json_atomic(
        batch_directory / "evaluator-timeout-reconciliation.json",
        {
            "schema_version": 1,
            "model_rerun": False,
            "candidate_mutated": False,
            "changes": changes,
        },
    )
    return reconciled


def compare_gamedevbench_paired_results(
    *,
    manifest_path: Path,
    official_results_path: Path,
    harness_progress_path: Path,
    output_path: Path,
) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    official = json.loads(official_results_path.read_text(encoding="utf-8"))
    harness = json.loads(harness_progress_path.read_text(encoding="utf-8"))
    official_by_task = {record["task_name"]: record for record in official.get("tasks", [])}
    harness_by_task = {record["task_id"]: record for record in harness.get("tasks", [])}
    pairs: list[dict[str, object]] = []
    for entry in manifest.get("tasks", []):
        task_id = entry["task_id"]
        official_record = official_by_task.get(task_id)
        harness_record = harness_by_task.get(task_id)
        if official_record is None or harness_record is None:
            continue
        harness_infrastructure = harness_record.get("status") == "INFRASTRUCTURE_ERROR"
        official_infrastructure = bool(official_record.get("is_rate_limited"))
        official_evaluable = (
            isinstance(official_record.get("success"), bool) and not official_infrastructure
        )
        evaluable = official_evaluable and not harness_infrastructure
        official_pass = bool(official_record.get("success")) if evaluable else None
        harness_pass = harness_record.get("status") == "PASS" if evaluable else None
        official_input_tokens = int(official_record.get("input_tokens", 0))
        official_output_tokens = int(official_record.get("output_tokens", 0))
        harness_input_tokens = int(harness_record.get("input_tokens", 0))
        harness_output_tokens = int(harness_record.get("output_tokens", 0))
        pairs.append(
            {
                "task_id": task_id,
                "stratum": entry["stratum"],
                "evaluable": evaluable,
                "official_pass": official_pass,
                "harness_pass": harness_pass,
                "official_infrastructure_error": official_infrastructure,
                "harness_status": harness_record.get("status"),
                "harness_failure_category": harness_record.get("failure_category"),
                "official_solver_success": official_record.get("solver_success"),
                "harness_official_gate": harness_record.get("official_gate"),
                "harness_preservation_gate": harness_record.get("preservation_gate"),
                "harness_visual_gate": harness_record.get("visual_gate", "NOT_APPLICABLE"),
                "harness_complexity_route": harness_record.get("complexity_route", "unknown"),
                "harness_validation_attempts": int(harness_record.get("validation_attempts", 0)),
                "harness_repair_attempts": int(harness_record.get("repair_attempts", 0)),
                "harness_feedback_attempts": int(harness_record.get("feedback_attempts", 0)),
                "official_input_tokens": official_input_tokens,
                "official_output_tokens": official_output_tokens,
                "harness_input_tokens": harness_input_tokens,
                "harness_cached_input_tokens": int(harness_record.get("cached_input_tokens", 0)),
                "harness_output_tokens": harness_output_tokens,
                "usage_complete": (
                    official_input_tokens + official_output_tokens > 0
                    and harness_input_tokens + harness_output_tokens > 0
                ),
                "official_duration_seconds": float(official_record.get("solver_duration", 0.0)),
                "harness_duration_seconds": float(harness_record.get("duration_seconds", 0.0)),
                "official_cost_usd": float(official_record.get("cost_usd", 0.0)),
                "harness_cost_usd": float(harness_record.get("cost_usd", 0.0)),
                "harness_tool_calls": int(harness_record.get("tool_calls", 0)),
            }
        )

    evaluable_pairs = [pair for pair in pairs if pair["evaluable"]]
    total = len(evaluable_pairs)
    official_passes = sum(pair["official_pass"] is True for pair in evaluable_pairs)
    harness_passes = sum(pair["harness_pass"] is True for pair in evaluable_pairs)
    official_only = sum(
        pair["official_pass"] is True and pair["harness_pass"] is False for pair in evaluable_pairs
    )
    harness_only = sum(
        pair["official_pass"] is False and pair["harness_pass"] is True for pair in evaluable_pairs
    )
    both_pass = sum(
        pair["official_pass"] is True and pair["harness_pass"] is True for pair in evaluable_pairs
    )
    both_fail = sum(
        pair["official_pass"] is False and pair["harness_pass"] is False for pair in evaluable_pairs
    )
    official_interval = _wilson_interval(official_passes, total)
    harness_interval = _wilson_interval(harness_passes, total)
    usage_complete_pairs = [pair for pair in evaluable_pairs if pair["usage_complete"]]
    official_tokens = sum(
        int(pair["official_input_tokens"]) + int(pair["official_output_tokens"])
        for pair in usage_complete_pairs
    )
    harness_tokens = sum(
        int(pair["harness_input_tokens"]) + int(pair["harness_output_tokens"])
        for pair in usage_complete_pairs
    )
    official_durations = [float(pair["official_duration_seconds"]) for pair in evaluable_pairs]
    harness_durations = [float(pair["harness_duration_seconds"]) for pair in evaluable_pairs]
    official_duration_total = sum(official_durations)
    harness_duration_total = sum(harness_durations)
    status_counts = Counter(str(pair["harness_status"]) for pair in pairs)
    modified_pairs = [pair for pair in pairs if int(pair["harness_tool_calls"]) > 0]
    preserved_modified_pairs = [
        pair for pair in modified_pairs if pair["harness_preservation_gate"] == "PASS"
    ]
    strata: dict[str, object] = {}
    for name in sorted({str(pair["stratum"]) for pair in evaluable_pairs}):
        stratum_pairs = [pair for pair in evaluable_pairs if pair["stratum"] == name]
        stratum_official_passes = sum(pair["official_pass"] is True for pair in stratum_pairs)
        stratum_harness_passes = sum(pair["harness_pass"] is True for pair in stratum_pairs)
        stratum_total = len(stratum_pairs)
        strata[name] = {
            "tasks": stratum_total,
            "official_passes": stratum_official_passes,
            "harness_passes": stratum_harness_passes,
            "official_pass_rate": round(stratum_official_passes / stratum_total, 6),
            "harness_pass_rate": round(stratum_harness_passes / stratum_total, 6),
            "official_pass_rate_wilson_95": _rounded_interval(
                _wilson_interval(stratum_official_passes, stratum_total)
            ),
            "harness_pass_rate_wilson_95": _rounded_interval(
                _wilson_interval(stratum_harness_passes, stratum_total)
            ),
        }
    comparison: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": manifest.get("id"),
        "benchmark_commit": manifest.get("benchmark_commit"),
        "model": official.get("configuration", {}).get("model"),
        "paired_tasks_completed": len(pairs),
        "paired_tasks_evaluable": total,
        "official_passes": official_passes,
        "harness_passes": harness_passes,
        "official_pass_rate": round(official_passes / total, 6) if total else None,
        "harness_pass_rate": round(harness_passes / total, 6) if total else None,
        "official_pass_rate_wilson_95": _rounded_interval(official_interval),
        "harness_pass_rate_wilson_95": _rounded_interval(harness_interval),
        "pass_rate_delta_harness_minus_official": (
            round((harness_passes - official_passes) / total, 6) if total else None
        ),
        "paired_outcomes": {
            "both_pass": both_pass,
            "official_only": official_only,
            "harness_only": harness_only,
            "both_fail": both_fail,
        },
        "strata": strata,
        "mcnemar_exact_two_sided_p": _mcnemar_exact(official_only, harness_only),
        "usage_complete_pairs": len(usage_complete_pairs),
        "usage_missing_pairs": total - len(usage_complete_pairs),
        "efficiency_official_total_tokens": official_tokens,
        "efficiency_harness_total_tokens": harness_tokens,
        "efficiency_token_reduction_fraction": (
            round(1 - harness_tokens / official_tokens, 6) if official_tokens else None
        ),
        "reported_official_total_tokens_all_pairs": sum(
            int(pair["official_input_tokens"]) + int(pair["official_output_tokens"])
            for pair in evaluable_pairs
        ),
        "reported_harness_total_tokens_all_pairs": sum(
            int(pair["harness_input_tokens"]) + int(pair["harness_output_tokens"])
            for pair in evaluable_pairs
        ),
        "official_total_duration_seconds": round(official_duration_total, 3),
        "harness_total_duration_seconds": round(harness_duration_total, 3),
        "harness_duration_increase_fraction": (
            round(harness_duration_total / official_duration_total - 1, 6)
            if official_duration_total
            else None
        ),
        "official_duration_median_seconds": round(statistics.median(official_durations), 3),
        "harness_duration_median_seconds": round(statistics.median(harness_durations), 3),
        "official_duration_p95_seconds": round(_percentile(official_durations, 0.95), 3),
        "harness_duration_p95_seconds": round(_percentile(harness_durations, 0.95), 3),
        "efficiency_official_cost_usd": round(
            sum(float(pair["official_cost_usd"]) for pair in usage_complete_pairs), 8
        ),
        "efficiency_harness_cost_usd": round(
            sum(float(pair["harness_cost_usd"]) for pair in usage_complete_pairs), 8
        ),
        "reported_official_cost_usd_all_pairs": round(
            sum(float(pair["official_cost_usd"]) for pair in evaluable_pairs), 8
        ),
        "reported_harness_cost_usd_all_pairs": round(
            sum(float(pair["harness_cost_usd"]) for pair in evaluable_pairs), 8
        ),
        "harness_status_counts": dict(sorted(status_counts.items())),
        "harness_modified_tasks": len(modified_pairs),
        "harness_modified_tasks_preserved": len(preserved_modified_pairs),
        "harness_validation_attempts": sum(
            int(pair["harness_validation_attempts"]) for pair in pairs
        ),
        "harness_repair_attempts": sum(int(pair["harness_repair_attempts"]) for pair in pairs),
        "harness_feedback_attempts": sum(int(pair["harness_feedback_attempts"]) for pair in pairs),
        "harness_complexity_routes": dict(
            sorted(Counter(str(pair["harness_complexity_route"]) for pair in pairs).items())
        ),
        "harness_visual_gate_counts": dict(
            sorted(Counter(str(pair["harness_visual_gate"]) for pair in pairs).items())
        ),
        "cost_interpretation": (
            "Standardized benchmark price-table estimate; the local runs used saved ChatGPT "
            "authentication rather than API-key billing. Efficiency totals include only paired "
            "tasks where both runners reported usage."
        ),
        "pairs": pairs,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _write_json_atomic(output_path, comparison)
    return comparison


def run_gamedevbench_harness_batch(
    *,
    root: Path,
    benchmark_root: Path,
    manifest_path: Path,
    agent_executable: Path,
    model_name: str,
    reasoning_effort: str | None = None,
    input_usd_per_million: float = 0.0,
    cached_input_usd_per_million: float = 0.0,
    output_usd_per_million: float = 0.0,
    batch_directory: Path | None = None,
    retry_infrastructure_from: Path | None = None,
    runtime_protocol: RuntimeProtocol = RuntimeProtocol.LEGACY_TOOL_V1,
) -> GameDevBenchBatchRun:
    manifest_payload = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    manifest = _load_and_validate_manifest(
        json.loads(manifest_payload.decode("utf-8")), benchmark_root
    )
    output = batch_directory or _new_batch_directory(root, str(manifest["id"]))
    output.mkdir(parents=True, exist_ok=True)
    config_path = output / "batch-config.json"
    config = {
        "schema_version": 1,
        "manifest_id": manifest["id"],
        "manifest_sha256": manifest_sha256,
        "benchmark_commit": GAMEDEVBENCH_COMMIT,
        "model": model_name,
        "reasoning_effort": reasoning_effort,
        "input_usd_per_million": input_usd_per_million,
        "cached_input_usd_per_million": cached_input_usd_per_million,
        "output_usd_per_million": output_usd_per_million,
        "model_timeout_seconds": GAMEDEVBENCH_MODEL_TIMEOUT_SECONDS,
        "agent_executable_sha256": _sha256_file(agent_executable),
        "execution_mode": "serial",
        "parallelism": 1,
        "harness_protocol": (
            "programmable-cells-evidence-v1"
            if runtime_protocol is RuntimeProtocol.PROGRAMMABLE_V1
            else "minimal-open-native-workspace-v1"
            if runtime_protocol is RuntimeProtocol.MINIMAL_OPEN_V1
            else "typed-json-native-objects-grounded-feedback-v4"
        ),
        "retry_infrastructure_from_sha256": (
            _sha256_file(retry_infrastructure_from / "progress.json")
            if retry_infrastructure_from is not None
            else None
        ),
    }
    config["runtime_protocol"] = runtime_protocol.value
    if runtime_protocol is RuntimeProtocol.PROGRAMMABLE_V1:
        config["runtime_budget_profile"] = "simple-10x20-interactive-16x36-visual-18x40-v1"
    elif runtime_protocol is RuntimeProtocol.MINIMAL_OPEN_V1:
        config["runtime_budget_profile"] = "one-native-session-900s-no-intermediate-gates-v1"
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if existing != config:
            raise ValueError("batch resume configuration does not match the existing run")
    else:
        _write_json_atomic(config_path, config)
        (output / "manifest.json").write_bytes(manifest_payload)

    quota_before_path = output / "subscription-quota-before.json"
    if not quota_before_path.exists():
        _write_json_atomic(
            quota_before_path,
            capture_subscription_snapshot(agent_executable),
        )

    progress_path = output / "progress.json"
    if retry_infrastructure_from is not None and not progress_path.exists():
        source_progress_path = retry_infrastructure_from / "progress.json"
        source_records = _validated_progress_records(
            json.loads(source_progress_path.read_text(encoding="utf-8")), manifest
        )
        retry_records = [record for record in source_records if _retryable_pre_request(record)]
        if not retry_records:
            raise ValueError(
                "retry source contains no eligible pre-request infrastructure failures"
            )
        retry_ids = {str(record["task_id"]) for record in retry_records}
        seeded_records = [
            {**record, "carried_from_prior_batch": True}
            for record in source_records
            if str(record["task_id"]) not in retry_ids
        ]
        _write_json_atomic(
            output / "retry-lineage.json",
            {
                "schema_version": 1,
                "source_progress_sha256": _sha256_file(source_progress_path),
                "retried_task_ids": sorted(retry_ids),
                "reason": (
                    "The local CLI rejected image argument parsing before any model request."
                ),
                "eligibility": (
                    "visual route, command failure, zero usage, zero tool calls, under 5 seconds"
                ),
                "original_records": retry_records,
            },
        )
        _write_json_atomic(
            progress_path,
            {
                "schema_version": 1,
                "manifest_id": manifest["id"],
                "tasks": seeded_records,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
    if progress_path.exists():
        records = _validated_progress_records(
            json.loads(progress_path.read_text(encoding="utf-8")), manifest
        )
    else:
        records = []
    completed = {record["task_id"] for record in records}

    for entry in manifest["tasks"]:
        task_id = entry["task_id"]
        if task_id in completed:
            continue
        started = datetime.now(UTC)
        try:
            common = {
                "root": root,
                "benchmark_root": benchmark_root,
                "task_id": task_id,
                "agent_executable": agent_executable,
                "model_name": model_name,
                "reasoning_effort": reasoning_effort,
                "input_usd_per_million": input_usd_per_million,
                "cached_input_usd_per_million": cached_input_usd_per_million,
                "output_usd_per_million": output_usd_per_million,
            }
            if runtime_protocol is RuntimeProtocol.MINIMAL_OPEN_V1:
                run = run_gamedevbench_minimal_harness(**common)
            else:
                run = run_gamedevbench_harness(
                    **common,
                    editable_paths=(),
                    runtime_protocol=runtime_protocol,
                )
            result = run.harness_run.result
            gates = {gate.gate: gate.status.value for gate in result.gates}
            agent_result_path = run.directory / "agent-result.json"
            agent_result = (
                json.loads(agent_result_path.read_text(encoding="utf-8"))
                if agent_result_path.is_file()
                else {}
            )
            feedback_attempts = _count_feedback_attempts(run.directory)
            engine_crash_retries = _count_engine_crash_attempts(run.directory)
            godot_infrastructure_failure = _godot_infrastructure_failure_category(
                result.failure_category,
                run.directory,
            )
            recorded_status = (
                "INFRASTRUCTURE_ERROR"
                if godot_infrastructure_failure is not None
                else result.status.value
            )
            recorded_failure_category = (
                godot_infrastructure_failure
                if godot_infrastructure_failure is not None
                else result.failure_category
            )
            record: dict[str, object] = {
                "task_id": task_id,
                "stratum": entry["stratum"],
                "status": recorded_status,
                "official_gate": gates.get("official", "NOT_RUN"),
                "preservation_gate": gates.get("preservation", "NOT_RUN"),
                "visual_gate": gates.get("visual_auxiliary", "NOT_APPLICABLE"),
                "complexity_route": result.model.parameters.get("complexity_route", "unknown"),
                "validation_attempts": int(agent_result.get("validation_attempts", 0)),
                "repair_attempts": int(agent_result.get("repair_attempts", 0)),
                "feedback_attempts": feedback_attempts,
                "engine_crash_retries": engine_crash_retries,
                "duration_seconds": round((result.ended_at - result.started_at).total_seconds(), 3),
                "input_tokens": result.input_tokens,
                "cached_input_tokens": result.cached_input_tokens,
                "output_tokens": result.output_tokens,
                "reasoning_output_tokens": result.reasoning_output_tokens,
                "cost_usd": result.cost_usd,
                "tool_calls": result.tool_calls,
                "model_turns": int(getattr(result, "model_turns", 0)),
                "program_cells": int(getattr(result, "program_cells", 0)),
                "prompt_bytes": int(getattr(result, "prompt_bytes", 0)),
                "required_evidence_coverage": float(
                    getattr(result, "required_evidence_coverage", 0.0)
                ),
                "lineage_complete": bool(getattr(result, "lineage_complete", False)),
                "policy_violations": int(getattr(result, "policy_violations", 0)),
                "failure_category": recorded_failure_category,
                "failure_detail": result.failure_detail,
                "run_directory": str(run.directory),
            }
        except Exception as error:  # Preserve first-attempt infrastructure failures.
            record = {
                "task_id": task_id,
                "stratum": entry["stratum"],
                "status": "INFRASTRUCTURE_ERROR",
                "official_gate": "NOT_RUN",
                "preservation_gate": "NOT_RUN",
                "visual_gate": "NOT_RUN",
                "complexity_route": "unknown",
                "validation_attempts": 0,
                "repair_attempts": 0,
                "feedback_attempts": 0,
                "engine_crash_retries": 0,
                "duration_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "cost_usd": 0.0,
                "tool_calls": 0,
                "model_turns": 0,
                "program_cells": 0,
                "prompt_bytes": 0,
                "required_evidence_coverage": 0.0,
                "lineage_complete": False,
                "policy_violations": 0,
                "failure_category": type(error).__name__,
                "failure_detail": str(error),
                "run_directory": None,
            }
        records.append(record)
        completed.add(task_id)
        progress = {
            "schema_version": 1,
            "manifest_id": manifest["id"],
            "tasks": records,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _write_json_atomic(progress_path, progress)
        _write_json_atomic(output / "summary.json", _summarize(records, len(manifest["tasks"])))

    quota_after = capture_subscription_snapshot(agent_executable)
    _write_json_atomic(output / "subscription-quota-after.json", quota_after)
    quota_before = json.loads(quota_before_path.read_text(encoding="utf-8"))
    quota_change = quota_delta(quota_before, quota_after)
    _write_json_atomic(output / "subscription-quota-delta.json", quota_change)
    summary = {
        **_summarize(records, len(manifest["tasks"])),
        "subscription_quota_delta": quota_change,
    }
    _write_json_atomic(output / "summary.json", summary)
    return GameDevBenchBatchRun(output, summary)


def _retryable_pre_request(record: dict[str, object]) -> bool:
    return (
        record.get("status") == "BLOCKED"
        and record.get("failure_category") == "ModelError"
        and record.get("complexity_route") == "visual"
        and int(record.get("input_tokens", 0)) == 0
        and int(record.get("output_tokens", 0)) == 0
        and int(record.get("tool_calls", 0)) == 0
        and float(record.get("duration_seconds", 0)) < 5
    )


def _count_feedback_attempts(run_directory: Path) -> int:
    """Count persisted feedback calls, including model-requested and automatic ones."""
    return len(tuple(run_directory.glob("visual-feedback-*.json")))


def _count_engine_crash_attempts(run_directory: Path) -> int:
    """Count retained engine-crash artifacts, including recovered first attempts."""
    return len(tuple(run_directory.rglob("*engine-crash-attempt-*")))


def _has_persistent_engine_crash(
    failure_category: str | None,
    run_directory: Path,
) -> bool:
    """Identify engine crashes that exhausted process-level recovery.

    A recovered first-attempt crash leaves an artifact but remains evaluable. Only a
    terminal exception/category or a retained agent observation containing the
    exhausted-retry marker makes the model attempt an infrastructure error.
    """

    if failure_category and "GodotEngineCrash" in failure_category:
        return True
    marker = "Godot engine crashed after"
    for name in ("failure.json", "agent-result.json", "events.jsonl"):
        path = run_directory / name
        if path.is_file() and marker in path.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def _godot_infrastructure_failure_category(
    failure_category: str | None,
    run_directory: Path,
) -> str | None:
    """Separate host/engine failures from model and project correctness."""

    if failure_category and "GodotEnvironmentError" in failure_category:
        return "GodotEnvironmentError"
    if _has_persistent_engine_crash(failure_category, run_directory):
        return "GodotEngineCrash"
    marker = "Godot host environment rejected a required data path"
    for name in ("failure.json", "agent-result.json", "events.jsonl"):
        path = run_directory / name
        if path.is_file() and marker in path.read_text(encoding="utf-8", errors="replace"):
            return "GodotEnvironmentError"
    return None


def _retained_run_directory(
    root: Path,
    task_id: str,
    record: dict[str, object],
) -> Path | None:
    recorded = record.get("run_directory")
    if isinstance(recorded, str):
        path = Path(recorded)
        if path.is_dir():
            return path
    candidates = sorted(
        (root / "runs" / "gamedevbench-harness").glob(f"*-gamedevbench-{task_id}"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _startup_timeout_evidence(run_directory: Path) -> list[dict[str, object]]:
    agent_result_path = run_directory / "agent-result.json"
    if not agent_result_path.is_file():
        return []
    payload = json.loads(agent_result_path.read_text(encoding="utf-8"))
    trace = payload.get("trace")
    if not isinstance(trace, list):
        return []
    evidence: list[dict[str, object]] = []
    for entry in trace:
        if not isinstance(entry, dict) or entry.get("event") != "automatic_validation":
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        evidence.append(
            {
                "turn": entry.get("turn"),
                "import_return_code": result.get("import_return_code"),
                "startup_return_code": result.get("startup_return_code"),
                "diagnostics": result.get("diagnostics"),
            }
        )
    return evidence


def _only_inconclusive_startup_timeouts(run_directory: Path) -> bool:
    evidence = _startup_timeout_evidence(run_directory)
    return bool(evidence) and all(
        item.get("import_return_code") == 0
        and item.get("startup_return_code") == 124
        and item.get("diagnostics") == []
        for item in evidence
    )


def _load_and_validate_manifest(payload: object, benchmark_root: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("GameDevBench batch manifest must be an object")
    if payload.get("schema_version") != 1 or payload.get("benchmark_commit") != GAMEDEVBENCH_COMMIT:
        raise ValueError("GameDevBench batch manifest version or commit is invalid")
    manifest_id = payload.get("id")
    tasks = payload.get("tasks")
    if not isinstance(manifest_id, str) or not manifest_id or not isinstance(tasks, list):
        raise ValueError("GameDevBench batch manifest is missing id or tasks")
    seen: set[str] = set()
    validated: list[dict[str, str]] = []
    for entry in tasks:
        if not isinstance(entry, dict):
            raise ValueError("GameDevBench batch task entries must be objects")
        task_id = entry.get("task_id")
        stratum = entry.get("stratum")
        instruction_sha256 = entry.get("instruction_sha256")
        if (
            not isinstance(task_id, str)
            or not isinstance(stratum, str)
            or not isinstance(instruction_sha256, str)
            or task_id in seen
        ):
            raise ValueError("GameDevBench batch task entry is invalid or duplicated")
        source = GameDevBenchSource(benchmark_root, task_id, ())
        instruction = str(source.task_config()["instruction"])
        actual_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        if actual_hash != instruction_sha256:
            raise ValueError(f"public task instruction hash does not match: {task_id}")
        seen.add(task_id)
        validated.append({"task_id": task_id, "stratum": stratum})
    if not validated:
        raise ValueError("GameDevBench batch manifest contains no tasks")
    return {"id": manifest_id, "tasks": validated}


def _validated_progress_records(
    payload: object,
    manifest: dict[str, Any],
) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise ValueError("GameDevBench batch progress must be an object")
    tasks = payload.get("tasks")
    if (
        payload.get("schema_version") != 1
        or payload.get("manifest_id") != manifest["id"]
        or not isinstance(tasks, list)
    ):
        raise ValueError("GameDevBench batch progress does not match the manifest")
    strata = {entry["task_id"]: entry["stratum"] for entry in manifest["tasks"]}
    seen: set[str] = set()
    records: list[dict[str, object]] = []
    for record in tasks:
        if not isinstance(record, dict):
            raise ValueError("GameDevBench batch progress task entries must be objects")
        task_id = record.get("task_id")
        if (
            not isinstance(task_id, str)
            or task_id in seen
            or task_id not in strata
            or record.get("stratum") != strata[task_id]
        ):
            raise ValueError("GameDevBench batch progress contains an invalid task entry")
        seen.add(task_id)
        records.append(record)
    return records


def _summarize(records: list[dict[str, object]], tasks_total: int) -> dict[str, object]:
    completed = len(records)
    passes = sum(record.get("status") == "PASS" for record in records)
    evaluable = sum(record.get("status") != "INFRASTRUCTURE_ERROR" for record in records)
    lower, upper = _wilson_interval(passes, evaluable)
    strata: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        strata[str(record["stratum"])][str(record["status"])] += 1
    return {
        "schema_version": 1,
        "tasks_total": tasks_total,
        "tasks_completed": completed,
        "evaluable_tasks": evaluable,
        "passes": passes,
        "pass_rate": round(passes / evaluable, 6) if evaluable else None,
        "pass_rate_wilson_95": {
            "lower": round(lower, 6) if lower is not None else None,
            "upper": round(upper, 6) if upper is not None else None,
        },
        "status_counts": dict(sorted(Counter(str(r["status"]) for r in records).items())),
        "strata": {name: dict(sorted(counts.items())) for name, counts in sorted(strata.items())},
        "input_tokens": sum(int(record["input_tokens"]) for record in records),
        "cached_input_tokens": sum(int(record["cached_input_tokens"]) for record in records),
        "output_tokens": sum(int(record["output_tokens"]) for record in records),
        "reasoning_output_tokens": sum(
            int(record["reasoning_output_tokens"]) for record in records
        ),
        "cost_usd": round(sum(float(record["cost_usd"]) for record in records), 8),
        "tool_calls": sum(int(record["tool_calls"]) for record in records),
        "model_turns": sum(int(record.get("model_turns", 0)) for record in records),
        "program_cells": sum(int(record.get("program_cells", 0)) for record in records),
        "prompt_bytes": sum(int(record.get("prompt_bytes", 0)) for record in records),
        "lineage_complete_tasks": sum(
            bool(record.get("lineage_complete", False)) for record in records
        ),
        "policy_violations": sum(int(record.get("policy_violations", 0)) for record in records),
        "engine_crash_retries": sum(
            int(record.get("engine_crash_retries", 0)) for record in records
        ),
        "validation_attempts": sum(int(record.get("validation_attempts", 0)) for record in records),
        "repair_attempts": sum(int(record.get("repair_attempts", 0)) for record in records),
        "feedback_attempts": sum(int(record.get("feedback_attempts", 0)) for record in records),
        "complexity_routes": dict(
            sorted(Counter(str(r.get("complexity_route", "unknown")) for r in records).items())
        ),
        "visual_gate_counts": dict(
            sorted(Counter(str(r.get("visual_gate", "NOT_APPLICABLE")) for r in records).items())
        ),
        "duration_seconds": round(sum(float(record["duration_seconds"]) for record in records), 3),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _wilson_interval(successes: int, total: int) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _rounded_interval(interval: tuple[float | None, float | None]) -> dict[str, float | None]:
    lower, upper = interval
    return {
        "lower": round(lower, 6) if lower is not None else None,
        "upper": round(upper, 6) if upper is not None else None,
    }


def _mcnemar_exact(official_only: int, harness_only: int) -> float | None:
    discordant = official_only + harness_only
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, index) for index in range(min(official_only, harness_only) + 1)
    ) / (2**discordant)
    return round(min(1.0, 2 * tail), 8)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _new_batch_directory(root: Path, manifest_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root / "runs" / "gamedevbench-batches" / f"{timestamp}-{manifest_id}-harness"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
