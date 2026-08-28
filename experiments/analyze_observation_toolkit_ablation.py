#!/usr/bin/env python3
"""Analyze a paired baseline/observation-toolkit experiment."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _receipts(experiment: Path) -> dict[str, dict[str, Any]]:
    rows = [_read_json(path) for path in sorted((experiment / "receipts").glob("*.json"))]
    if not rows:
        raise RuntimeError(f"no receipts found under {experiment}")
    by_task = {str(row["task_id"]): row for row in rows}
    if len(by_task) != len(rows):
        raise RuntimeError(f"duplicate task receipts under {experiment}")
    return by_task


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    records = [row["record"] for row in rows]
    durations = [float(record["duration_seconds"]) for record in records]
    command_exit_counts: Counter[str] = Counter()
    failed_command_tasks: set[str] = set()
    exit_134_tasks: Counter[str] = Counter()
    for row, record in zip(rows, records, strict=True):
        audit_path = Path(record["run_directory"]) / "agent-tool-audit.json"
        for event in _read_json(audit_path).get("events", []):
            if event.get("kind") != "command_execution":
                continue
            exit_code = int(event["exit_code"])
            command_exit_counts[str(exit_code)] += 1
            if exit_code != 0:
                failed_command_tasks.add(str(row["task_id"]))
            if exit_code in {-11, -6, 134, 139}:
                exit_134_tasks[str(row["task_id"])] += 1
    numeric_totals = {}
    for field in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "tool_calls",
        "engine_crash_retries",
    ):
        numeric_totals[field] = sum(int(record.get(field) or 0) for record in records)
    return {
        "attempts": len(rows),
        "status_counts": dict(Counter(str(row["normalized_status"]) for row in rows)),
        "passes": sum(row["normalized_status"] == "PASS" for row in rows),
        "pass_rate": round(
            sum(row["normalized_status"] == "PASS" for row in rows) / len(rows), 6
        ),
        "duration_seconds": {
            "total": round(sum(durations), 3),
            "mean": round(statistics.mean(durations), 3),
            "median": round(statistics.median(durations), 3),
        },
        "totals": numeric_totals,
        "infrastructure_retry_count": sum(
            len(row.get("infrastructure_retries", [])) for row in rows
        ),
        "nonzero_runner_exit_count": sum(int(row["runner_exit_code"]) != 0 for row in rows),
        "agent_command_audit": {
            "exit_code_counts": dict(sorted(command_exit_counts.items())),
            "failed_command_tasks": sorted(failed_command_tasks),
            "exit_134_tasks": dict(sorted(exit_134_tasks.items())),
        },
    }


def _mcnemar_exact_two_sided(baseline_only: int, candidate_only: int) -> float:
    discordant = baseline_only + candidate_only
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, index) for index in range(min(baseline_only, candidate_only) + 1)
    ) / (2**discordant)
    return min(1.0, 2.0 * tail)


def _observation_usage(candidate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    commands: Counter[str] = Counter()
    event_statuses: Counter[str] = Counter()
    task_rows: list[dict[str, Any]] = []
    copy_seconds: list[float] = []
    for row in candidate_rows:
        run_directory = Path(row["record"]["run_directory"])
        toolkit_path = run_directory / "observation-toolkit.json"
        events_path = run_directory / "observation-tool-events.json"
        if not toolkit_path.is_file() or not events_path.is_file():
            raise RuntimeError(f"missing observation audit under {run_directory}")
        toolkit = _read_json(toolkit_path)
        events = _read_json(events_path).get("events", [])
        copy_seconds.append(float(toolkit["baseline_copy_seconds_outside_solver_timer"]))
        for event in events:
            commands[str(event["command"])] += 1
            event_statuses[str(event["status"])] += 1
        task_rows.append(
            {
                "task_id": row["task_id"],
                "status": row["normalized_status"],
                "calls": int(toolkit["calls"]),
                "commands": [str(event["command"]) for event in events],
                "event_statuses": [str(event["status"]) for event in events],
            }
        )
    used = [row for row in task_rows if row["calls"] > 0]
    unused = [row for row in task_rows if row["calls"] == 0]
    return {
        "available_tasks": len(task_rows),
        "used_tasks": len(used),
        "adoption_rate": round(len(used) / len(task_rows), 6),
        "calls": sum(int(row["calls"]) for row in task_rows),
        "command_counts": dict(sorted(commands.items())),
        "event_status_counts": dict(sorted(event_statuses.items())),
        "used_task_passes": sum(row["status"] == "PASS" for row in used),
        "unused_task_passes": sum(row["status"] == "PASS" for row in unused),
        "baseline_copy_seconds_outside_solver_timer": {
            "total": round(sum(copy_seconds), 6),
            "mean": round(statistics.mean(copy_seconds), 6),
            "max": round(max(copy_seconds), 6),
        },
        "tasks": task_rows,
    }


def main() -> int:
    args = _arguments()
    baseline_dir = args.baseline.resolve(strict=True)
    candidate_dir = args.candidate.resolve(strict=True)
    baseline = _receipts(baseline_dir)
    candidate = _receipts(candidate_dir)
    if set(baseline) != set(candidate):
        raise RuntimeError("baseline and candidate task sets differ")

    per_task = []
    counts = Counter()
    for task_id in sorted(baseline):
        baseline_row = baseline[task_id]
        candidate_row = candidate[task_id]
        baseline_pass = baseline_row["normalized_status"] == "PASS"
        candidate_pass = candidate_row["normalized_status"] == "PASS"
        outcome = (
            "both_pass"
            if baseline_pass and candidate_pass
            else "baseline_only"
            if baseline_pass
            else "candidate_only"
            if candidate_pass
            else "both_fail"
        )
        counts[outcome] += 1
        per_task.append(
            {
                "task_id": task_id,
                "task_name": baseline_row["task_name"],
                "stratum": baseline_row["stratum"],
                "outcome": outcome,
                "baseline": baseline_row["record"],
                "candidate": candidate_row["record"],
            }
        )

    baseline_rows = [baseline[task_id] for task_id in sorted(baseline)]
    candidate_rows = [candidate[task_id] for task_id in sorted(candidate)]
    baseline_aggregate = _aggregate(baseline_rows)
    candidate_aggregate = _aggregate(candidate_rows)
    duration_ratio = (
        candidate_aggregate["duration_seconds"]["total"]
        / baseline_aggregate["duration_seconds"]["total"]
    )
    largest_duration_delta = max(
        per_task,
        key=lambda row: float(row["candidate"]["duration_seconds"])
        - float(row["baseline"]["duration_seconds"]),
    )
    baseline_without_largest = baseline_aggregate["duration_seconds"]["total"] - float(
        largest_duration_delta["baseline"]["duration_seconds"]
    )
    candidate_without_largest = candidate_aggregate["duration_seconds"]["total"] - float(
        largest_duration_delta["candidate"]["duration_seconds"]
    )
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "design": {
            "type": "paired targeted ablation",
            "repetitions_per_task": 1,
            "task_count": len(per_task),
            "warning": (
                "This failure-enriched subset is not an estimate of Full332 pass rate; "
                "single-run efficiency differences include model stochasticity."
            ),
        },
        "experiments": {
            "baseline": str(baseline_dir),
            "candidate": str(candidate_dir),
        },
        "aggregate": {
            "baseline": baseline_aggregate,
            "candidate": candidate_aggregate,
            "candidate_minus_baseline": {
                "passes": candidate_aggregate["passes"] - baseline_aggregate["passes"],
                "duration_seconds": round(
                    candidate_aggregate["duration_seconds"]["total"]
                    - baseline_aggregate["duration_seconds"]["total"],
                    3,
                ),
                "duration_ratio": round(duration_ratio, 6),
                "input_tokens": candidate_aggregate["totals"]["input_tokens"]
                - baseline_aggregate["totals"]["input_tokens"],
                "output_tokens": candidate_aggregate["totals"]["output_tokens"]
                - baseline_aggregate["totals"]["output_tokens"],
                "tool_calls": candidate_aggregate["totals"]["tool_calls"]
                - baseline_aggregate["totals"]["tool_calls"],
            },
            "duration_sensitivity": {
                "largest_positive_delta_task": largest_duration_delta["task_id"],
                "largest_positive_delta_seconds": round(
                    float(largest_duration_delta["candidate"]["duration_seconds"])
                    - float(largest_duration_delta["baseline"]["duration_seconds"]),
                    3,
                ),
                "candidate_ratio_excluding_that_task": round(
                    candidate_without_largest / baseline_without_largest, 6
                ),
            },
        },
        "paired": {
            **{
                key: counts[key]
                for key in (
                    "both_pass",
                    "baseline_only",
                    "candidate_only",
                    "both_fail",
                )
            },
            "mcnemar_exact_two_sided_p": _mcnemar_exact_two_sided(
                counts["baseline_only"], counts["candidate_only"]
            ),
        },
        "observation_toolkit": _observation_usage(candidate_rows),
        "per_task": per_task,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
