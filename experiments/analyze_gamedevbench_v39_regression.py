#!/usr/bin/env python3
"""Relate v39 Godot broker behavior to its paired Full332 regression."""

from __future__ import annotations

import argparse
import json
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
        "--baseline",
        type=Path,
        default=(ROOT / "runs/experiments/gamedevbench-full-qualified332-minimal-open-v36p4-run1"),
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-v39p4-run1",
    )
    parser.add_argument("--output-name", default="v39-regression-audit.json")
    return parser.parse_args()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _receipts(experiment: Path) -> dict[str, dict[str, Any]]:
    return {
        str(row["task_id"]): row
        for path in sorted((experiment / "receipts").glob("*.json"))
        if isinstance(row := _load(path), dict)
    }


def _rate(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def _pair_group(baseline_pass: bool, candidate_pass: bool) -> str:
    if baseline_pass and candidate_pass:
        return "both_pass"
    if candidate_pass:
        return "candidate_only_pass"
    if baseline_pass:
        return "baseline_only_pass"
    return "neither_pass"


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_seconds = [float(row["baseline_duration_seconds"]) for row in rows]
    candidate_seconds = [float(row["candidate_duration_seconds"]) for row in rows]
    queue_wait = sum(float(row["queue_wait_seconds"]) for row in rows)
    timeout_rows = [row for row in rows if row["timed_out_events"]]
    queue_timeout_rows = [row for row in rows if row["queue_timeout_events"]]
    execution_timeout_rows = [row for row in rows if row["execution_timeout_events"]]
    rejected_rows = [row for row in rows if row["rejected_events"]]
    return {
        "tasks": len(rows),
        "task_ids": [str(row["task_id"]) for row in rows],
        "tasks_with_timeout": len(timeout_rows),
        "timeout_task_rate": _rate(len(timeout_rows), len(rows)),
        "timeout_task_ids": [str(row["task_id"]) for row in timeout_rows],
        "tasks_with_queue_timeout": len(queue_timeout_rows),
        "queue_timeout_task_ids": [str(row["task_id"]) for row in queue_timeout_rows],
        "tasks_with_execution_timeout": len(execution_timeout_rows),
        "execution_timeout_task_ids": [str(row["task_id"]) for row in execution_timeout_rows],
        "tasks_with_rejection": len(rejected_rows),
        "rejection_task_rate": _rate(len(rejected_rows), len(rows)),
        "rejection_task_ids": [str(row["task_id"]) for row in rejected_rows],
        "broker_events": sum(int(row["broker_events"]) for row in rows),
        "broker_queue_wait_seconds": queue_wait,
        "baseline_total_seconds": sum(baseline_seconds),
        "candidate_total_seconds": sum(candidate_seconds),
        "candidate_minus_baseline_seconds": sum(candidate_seconds) - sum(baseline_seconds),
        "baseline_median_seconds": statistics.median(baseline_seconds),
        "candidate_median_seconds": statistics.median(candidate_seconds),
    }


def main() -> int:
    arguments = _arguments()
    baseline = arguments.baseline.resolve(strict=True)
    candidate = arguments.candidate.resolve(strict=True)
    output_name = Path(arguments.output_name)
    if output_name.name != arguments.output_name or output_name.suffix != ".json":
        raise ValueError("output name must be a JSON basename")

    baseline_rows = _receipts(baseline)
    candidate_rows = _receipts(candidate)
    errors: list[str] = []
    if len(baseline_rows) != EXPECTED_TASKS:
        errors.append(f"baseline receipt count is {len(baseline_rows)}, expected 332")
    if len(candidate_rows) != EXPECTED_TASKS:
        errors.append(f"candidate receipt count is {len(candidate_rows)}, expected 332")
    if set(baseline_rows) != set(candidate_rows):
        errors.append("baseline and candidate task identities differ")

    rows: list[dict[str, Any]] = []
    rejected_events_followed_by_success = 0
    rejected_events_without_later_success = 0
    return_codes: Counter[int] = Counter()
    for task_id in sorted(set(baseline_rows) & set(candidate_rows)):
        baseline_row = baseline_rows[task_id]
        candidate_row = candidate_rows[task_id]
        baseline_record = baseline_row["record"]
        candidate_record = candidate_row["record"]
        run_directory = Path(str(candidate_record["run_directory"]))
        broker_path = run_directory / "godot-broker-events.json"
        if not broker_path.is_file():
            errors.append(f"missing broker audit for {task_id}")
            events: list[dict[str, Any]] = []
        else:
            payload = _load(broker_path)
            events = [event for event in payload.get("events", []) if isinstance(event, dict)]

        for index, event in enumerate(events):
            code = event.get("return_code")
            if isinstance(code, int):
                return_codes[code] += 1
            if not event.get("rejected"):
                continue
            later_success = any(
                later.get("return_code") == 0 and not later.get("rejected")
                for later in events[index + 1 :]
            )
            if later_success:
                rejected_events_followed_by_success += 1
            else:
                rejected_events_without_later_success += 1

        baseline_pass = baseline_row["normalized_status"] == "PASS"
        candidate_pass = candidate_row["normalized_status"] == "PASS"
        rows.append(
            {
                "task_id": task_id,
                "pair_group": _pair_group(baseline_pass, candidate_pass),
                "baseline_status": baseline_row["normalized_status"],
                "candidate_status": candidate_row["normalized_status"],
                "baseline_duration_seconds": float(baseline_record.get("duration_seconds", 0)),
                "candidate_duration_seconds": float(candidate_record.get("duration_seconds", 0)),
                "broker_events": len(events),
                "queue_wait_seconds": sum(
                    float(event.get("queue_wait_seconds", 0)) for event in events
                ),
                "execution_seconds": sum(
                    float(event.get("execution_seconds", 0)) for event in events
                ),
                "timed_out_events": sum(bool(event.get("timed_out")) for event in events),
                "queue_timeout_events": sum(
                    event.get("timeout_stage") == "queue" for event in events
                ),
                "execution_timeout_events": sum(
                    event.get("timeout_stage") == "execution" for event in events
                ),
                "rejected_events": sum(bool(event.get("rejected")) for event in events),
                "attempt_exit_134_events": sum(
                    134 in event.get("attempt_return_codes", []) for event in events
                ),
            }
        )

    groups = {
        name: _summarize([row for row in rows if row["pair_group"] == name])
        for name in (
            "both_pass",
            "candidate_only_pass",
            "baseline_only_pass",
            "neither_pass",
        )
    }
    all_rows = _summarize(rows)
    duration_delta = float(all_rows["candidate_minus_baseline_seconds"])
    queue_wait = float(all_rows["broker_queue_wait_seconds"])
    baseline_protocol = _load(baseline / "protocol-freeze.json")
    candidate_protocol = _load(candidate / "protocol-freeze.json")
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "inputs": {
            "baseline": str(baseline),
            "candidate": str(candidate),
            "baseline_runtime_tree_sha256": baseline_protocol.get("runtime_tree_sha256"),
            "candidate_runtime_tree_sha256": candidate_protocol.get("runtime_tree_sha256"),
            "baseline_agent_executable_sha256": baseline_protocol.get("agent_executable_sha256"),
            "candidate_agent_executable_sha256": candidate_protocol.get("agent_executable_sha256"),
        },
        "headline": {
            "all_tasks": all_rows,
            "candidate_total_duration_relative_change": _rate(
                duration_delta, float(all_rows["baseline_total_seconds"])
            ),
            "new_broker_queue_wait_share_of_observed_duration_increase": _rate(
                queue_wait, duration_delta
            ),
            "broker_return_codes": {
                str(code): count for code, count in sorted(return_codes.items())
            },
            "attempt_exit_134_events": sum(int(row["attempt_exit_134_events"]) for row in rows),
            "rejected_events_followed_by_later_success": (rejected_events_followed_by_success),
            "rejected_events_without_later_success": (rejected_events_without_later_success),
        },
        "by_paired_outcome": groups,
        "guard_direct_regression_check": {
            "baseline_only_tasks_with_rejection": groups["baseline_only_pass"][
                "tasks_with_rejection"
            ],
            "interpretation": (
                "A zero count means the MovieWriter compatibility rejection did not directly "
                "turn any v36 PASS into a v39 FAIL in this run."
            ),
        },
        "timeout_regression_signal": {
            "baseline_only_tasks_with_timeout": groups["baseline_only_pass"]["tasks_with_timeout"],
            "candidate_only_tasks_with_timeout": groups["candidate_only_pass"][
                "tasks_with_timeout"
            ],
            "interpretation": (
                "Descriptive association only. Queue serialization and fixed execution "
                "timeouts are new v39 behavior, but one stochastic repetition and a changed "
                "Codex executable prevent runtime-only causal attribution."
            ),
        },
        "tasks": rows,
    }
    output = candidate / output_name
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(payload["validation"], sort_keys=True))
    print(json.dumps(payload["headline"], sort_keys=True))
    print(json.dumps(payload["guard_direct_regression_check"], sort_keys=True))
    print(json.dumps(payload["timeout_regression_signal"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
