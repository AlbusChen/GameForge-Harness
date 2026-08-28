#!/usr/bin/env python3
"""Audit Godot runtime health and lifecycle costs in a complete candidate run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TASKS = 332


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        type=Path,
        default=(
            ROOT
            / "runs/experiments/"
            "gamedevbench-full-qualified332-minimal-open-godot-runtime-v38p4-run1"
        ),
    )
    parser.add_argument("--output-name", default="godot-runtime-analysis.json")
    return parser.parse_args()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "total": sum(values),
        "mean": statistics.fmean(values) if values else None,
        "median": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "maximum": max(values, default=None),
    }


def _new_crash_reports(started_at: float) -> dict[str, object]:
    directory = Path.home() / "Library/Logs/DiagnosticReports"
    patterns = {
        "godot": ("Godot-*.ips",),
        "python": ("Python-*.ips", "python*.ips"),
    }
    result: dict[str, object] = {}
    for kind, globs in patterns.items():
        paths = sorted(
            {
                path
                for pattern in globs
                for path in directory.glob(pattern)
                if path.stat().st_mtime >= started_at
            }
        )
        reports: list[dict[str, object]] = []
        classifications: Counter[str] = Counter()
        for path in paths:
            lines = path.read_text(encoding="utf-8").splitlines()
            header = json.loads(lines[0])
            body = json.loads("\n".join(lines[1:]))
            triggered = next(
                (thread for thread in body.get("threads", []) if thread.get("triggered")),
                {},
            )
            frames = [
                str(frame["symbol"])
                for frame in triggered.get("frames", [])
                if frame.get("symbol")
            ]
            if "MovieWriterPNGWAV::write_frame(Ref<Image> const&, int const*)" in frames:
                classification = "godot_4_4_1_pngwav_movie_writer_crash"
            elif "MovieWriterMJPEG::write_frame(Ref<Image> const&, int const*)" in frames:
                classification = "godot_4_4_1_mjpeg_movie_writer_crash"
            elif kind == "python":
                classification = "python_unclassified"
            else:
                classification = "godot_unclassified"
            classifications[classification] += 1
            reports.append(
                {
                    "path": str(path),
                    "sha256": _sha256(path),
                    "timestamp": header.get("timestamp"),
                    "app_version": header.get("app_version"),
                    "process_path": body.get("procPath"),
                    "parent_process": body.get("parentProc"),
                    "termination": body.get("termination"),
                    "classification": classification,
                    "triggered_frame_symbols": frames[:16],
                }
            )
        result[kind] = {
            "count": len(paths),
            "classifications": dict(sorted(classifications.items())),
            "reports": reports,
        }
    return result


def main() -> int:
    arguments = _arguments()
    experiment = arguments.experiment.resolve(strict=True)
    output_name = Path(arguments.output_name)
    if output_name.name != arguments.output_name or output_name.suffix != ".json":
        raise ValueError("output name must be a JSON basename")

    receipt_paths = sorted((experiment / "receipts").glob("*.json"))
    receipts = [_load(path) for path in receipt_paths]
    complete = _load(experiment / "complete.json")
    errors: list[str] = []
    if len(receipts) != EXPECTED_TASKS:
        errors.append(f"expected {EXPECTED_TASKS} receipts, found {len(receipts)}")
    if complete.get("attempts_completed") != EXPECTED_TASKS:
        errors.append("complete marker is not a complete Full332 run")

    fixed_executables: Counter[str] = Counter()
    fixed_versions: Counter[str] = Counter()
    return_codes: Counter[int] = Counter()
    attempt_return_codes: Counter[int] = Counter()
    timeout_stages: Counter[str] = Counter()
    command_exit_codes: Counter[int] = Counter()
    queue_waits: list[float] = []
    execution_times: list[float] = []
    broker_durations: list[float] = []
    tasks_with_broker_calls = 0
    tasks_with_timeout = 0
    tasks_with_crash_retry = 0
    rejected_events = 0
    crash_retries = 0
    policy_violations = 0
    infrastructure_retries = 0
    broker_events = 0
    crash_task_rows: list[dict[str, object]] = []
    timeout_task_rows: list[dict[str, object]] = []
    rejected_task_rows: list[dict[str, object]] = []

    for receipt in receipts:
        task_id = str(receipt["task_id"])
        normalized_status = str(receipt["normalized_status"])
        record = receipt["record"]
        run_directory = Path(str(record["run_directory"]))
        broker = _load(run_directory / "godot-broker-events.json")
        events = broker.get("events", [])
        if events:
            tasks_with_broker_calls += 1
        fixed_executables[str(broker.get("fixed_executable"))] += 1
        fixed_versions[str(broker.get("fixed_engine_version"))] += 1
        task_timed_out = False
        task_retried_crash = False
        task_crash_events = 0
        task_timeout_events = 0
        task_rejected_events = 0
        for event in events:
            broker_events += 1
            return_codes[int(event["return_code"])] += 1
            queue_waits.append(float(event["queue_wait_seconds"]))
            execution_times.append(float(event["execution_seconds"]))
            broker_durations.append(float(event["duration_seconds"]))
            rejected_events += bool(event.get("rejected"))
            task_rejected_events += bool(event.get("rejected"))
            event_crash_retries = int(event.get("crash_retries", 0))
            crash_retries += event_crash_retries
            task_retried_crash = task_retried_crash or event_crash_retries > 0
            for code in event.get("attempt_return_codes", []):
                attempt_return_codes[int(code)] += 1
            task_crash_events += 134 in event.get("attempt_return_codes", [])
            if event.get("timed_out"):
                task_timed_out = True
                task_timeout_events += 1
                timeout_stages[str(event.get("timeout_stage"))] += 1
        tasks_with_timeout += task_timed_out
        tasks_with_crash_retry += task_retried_crash
        if task_crash_events:
            crash_task_rows.append(
                {
                    "task_id": task_id,
                    "status": normalized_status,
                    "broker_events_with_attempt_134": task_crash_events,
                }
            )
        if task_timeout_events:
            timeout_task_rows.append(
                {
                    "task_id": task_id,
                    "status": normalized_status,
                    "timeout_events": task_timeout_events,
                }
            )
        if task_rejected_events:
            rejected_task_rows.append(
                {
                    "task_id": task_id,
                    "status": normalized_status,
                    "rejected_events": task_rejected_events,
                }
            )

        result = _load(run_directory / "result.json")
        policy_violations += int(result.get("policy_violations", 0))
        infrastructure_retries += len(receipt.get("infrastructure_retries", []))
        audit = _load(run_directory / "agent-tool-audit.json")
        for event in audit.get("events", []):
            if event.get("kind") != "command_execution":
                continue
            code = event.get("exit_code")
            if isinstance(code, int):
                command_exit_codes[code] += 1

    protocol_path = experiment / "protocol-freeze.json"
    started_at = protocol_path.stat().st_mtime
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "inputs": {
            "experiment": str(experiment),
            "complete_sha256": _sha256(experiment / "complete.json"),
            "protocol_sha256": _sha256(protocol_path),
            "receipt_count": len(receipts),
            "crash_report_window_started_at": datetime.fromtimestamp(
                started_at, UTC
            ).isoformat(),
        },
        "broker": {
            "tasks_with_calls": tasks_with_broker_calls,
            "events": broker_events,
            "fixed_executables": dict(sorted(fixed_executables.items())),
            "fixed_engine_versions": dict(sorted(fixed_versions.items())),
            "return_codes": {str(k): v for k, v in sorted(return_codes.items())},
            "attempt_return_codes": {
                str(k): v for k, v in sorted(attempt_return_codes.items())
            },
            "tasks_with_timeout": tasks_with_timeout,
            "timeout_stages": dict(sorted(timeout_stages.items())),
            "tasks_with_crash_retry": tasks_with_crash_retry,
            "crash_retries": crash_retries,
            "rejected_events": rejected_events,
            "crash_tasks": crash_task_rows,
            "crash_task_status_counts": dict(
                sorted(Counter(str(row["status"]) for row in crash_task_rows).items())
            ),
            "timeout_tasks": timeout_task_rows,
            "timeout_task_status_counts": dict(
                sorted(Counter(str(row["status"]) for row in timeout_task_rows).items())
            ),
            "rejected_tasks": rejected_task_rows,
            "queue_wait_seconds": _distribution(queue_waits),
            "execution_seconds": _distribution(execution_times),
            "duration_seconds": _distribution(broker_durations),
        },
        "agent_audit": {
            "command_exit_codes": {
                str(k): v for k, v in sorted(command_exit_codes.items())
            },
            "command_exit_134": command_exit_codes[134],
            "policy_violations": policy_violations,
            "infrastructure_retries": infrastructure_retries,
        },
        "new_crash_reports": _new_crash_reports(started_at),
    }
    output = experiment / output_name
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(payload["validation"], sort_keys=True))
    print(json.dumps(payload["broker"], sort_keys=True))
    print(json.dumps(payload["new_crash_reports"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
