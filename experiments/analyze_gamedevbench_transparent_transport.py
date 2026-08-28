#!/usr/bin/env python3
"""Analyze the same-task transparent pinned-Godot transport regression."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_gamedevbench_blind64_threeway import (
    CRASH_EXIT_CODES,
    _aggregate,
    _godot_usage,
    _paired,
    _receipts,
    _strata,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--observation-toolkit", required=True, type=Path)
    parser.add_argument("--old-godot", required=True, type=Path)
    parser.add_argument("--transparent", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _transport_usage(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    codes: Counter[int] = Counter()
    scopes: Counter[str] = Counter()
    task_rows: list[dict[str, Any]] = []
    total_execution_seconds = 0.0
    bridge_crash_tasks: list[str] = []
    for task_id, row in sorted(rows.items()):
        run_directory = Path(row["record"]["run_directory"])
        events = _read_json(
            run_directory / "pinned-godot-transport-events.json"
        ).get("events", [])
        task_codes = [int(event["return_code"]) for event in events]
        task_crashes = [code for code in task_codes if code in CRASH_EXIT_CODES]
        if task_crashes:
            bridge_crash_tasks.append(task_id)
        codes.update(task_codes)
        scopes.update(str(event.get("cwd_scope")) for event in events)
        total_execution_seconds += sum(float(event["execution_seconds"]) for event in events)
        task_rows.append(
            {
                "task_id": task_id,
                "status": row["normalized_status"],
                "calls": len(events),
                "return_codes": task_codes,
                "cwd_scopes": [str(event.get("cwd_scope")) for event in events],
                "rejections": sum(bool(event["rejected"]) for event in events),
                "timeouts": sum(bool(event["timed_out"]) for event in events),
                "crash_retries": sum(int(event["crash_retries"]) for event in events),
            }
        )
    bridge_crashes = sum(count for code, count in codes.items() if code in CRASH_EXIT_CODES)
    return {
        "available_tasks": len(task_rows),
        "used_tasks": sum(task["calls"] > 0 for task in task_rows),
        "calls": sum(task["calls"] for task in task_rows),
        "calls_per_task_median": statistics.median(task["calls"] for task in task_rows),
        "return_code_counts": {str(code): count for code, count in sorted(codes.items())},
        "cwd_scope_counts": dict(sorted(scopes.items())),
        "bridge_crash_exit_count": bridge_crashes,
        "bridge_crash_task_count": len(bridge_crash_tasks),
        "bridge_crash_tasks": bridge_crash_tasks,
        "rejections": sum(task["rejections"] for task in task_rows),
        "timeouts": sum(task["timeouts"] for task in task_rows),
        "crash_retries": sum(task["crash_retries"] for task in task_rows),
        "execution_seconds": round(total_execution_seconds, 3),
        "tasks": task_rows,
    }


def main() -> int:
    args = _arguments()
    directories = {
        "baseline": args.baseline.resolve(strict=True),
        "observation_toolkit": args.observation_toolkit.resolve(strict=True),
        "old_godot_host_execution": args.old_godot.resolve(strict=True),
        "transparent_pinned_godot_transport": args.transparent.resolve(strict=True),
    }
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    integrity: dict[str, Any] = {}
    for condition, directory in directories.items():
        rows[condition], integrity[condition] = _receipts(directory)
    if any(set(value) != set(rows["baseline"]) for value in rows.values()):
        raise RuntimeError("condition task sets differ")
    orders = {
        condition: [
            row["task_id"]
            for row in sorted(values.values(), key=lambda item: int(item["manifest_order"]))
        ]
        for condition, values in rows.items()
    }
    if any(order != orders["baseline"] for order in orders.values()):
        raise RuntimeError("condition task orders differ")

    aggregates = {
        condition: _aggregate([values[task_id] for task_id in sorted(values)])
        for condition, values in rows.items()
    }
    paired = {
        "transparent_vs_baseline": _paired(
            rows["baseline"], rows["transparent_pinned_godot_transport"]
        ),
        "transparent_vs_old_godot": _paired(
            rows["old_godot_host_execution"],
            rows["transparent_pinned_godot_transport"],
        ),
    }
    transparent_usage = _transport_usage(rows["transparent_pinned_godot_transport"])
    transparent_crashes = aggregates["transparent_pinned_godot_transport"][
        "agent_command_audit"
    ]
    transparent_usage["model_command_crash_exit_count"] = transparent_crashes[
        "crash_exit_count"
    ]
    transparent_usage["model_command_crash_task_count"] = transparent_crashes[
        "crash_exit_task_count"
    ]
    transparent_usage["estimated_direct_bypass_crash_exit_count"] = (
        transparent_crashes["crash_exit_count"]
        - transparent_usage["bridge_crash_exit_count"]
    )

    patterns: Counter[str] = Counter()
    repeated_host_regressions: list[str] = []
    per_task: list[dict[str, Any]] = []
    pattern_order = (
        "baseline",
        "observation_toolkit",
        "old_godot_host_execution",
        "transparent_pinned_godot_transport",
    )
    for task_id in sorted(rows["baseline"]):
        statuses = {
            condition: values[task_id]["normalized_status"]
            for condition, values in rows.items()
        }
        pattern = "/".join(
            "P" if statuses[condition] == "PASS" else "F" for condition in pattern_order
        )
        patterns[pattern] += 1
        if (
            statuses["baseline"] == "PASS"
            and statuses["old_godot_host_execution"] == "FAIL"
            and statuses["transparent_pinned_godot_transport"] == "FAIL"
        ):
            repeated_host_regressions.append(task_id)
        per_task.append(
            {
                "task_id": task_id,
                "task_name": rows["baseline"][task_id]["task_name"],
                "stratum": rows["baseline"][task_id]["stratum"],
                "pattern_baseline_toolkit_old_godot_transparent": pattern,
                "conditions": {
                    condition: {
                        "status": values[task_id]["normalized_status"],
                        "duration_seconds": values[task_id]["record"]["duration_seconds"],
                        "input_tokens": values[task_id]["record"]["input_tokens"],
                        "output_tokens": values[task_id]["record"]["output_tokens"],
                        "tool_calls": values[task_id]["record"]["tool_calls"],
                    }
                    for condition, values in rows.items()
                },
            }
        )

    output = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "design": {
            "type": "same-task paired post-design regression",
            "task_count": 64,
            "same_task_order": True,
            "warning": (
                "The task set was originally outcome-blind, but the transparent candidate was "
                "designed after prior outcomes were observed. This is directly comparable "
                "regression evidence, not a fresh generalization estimate."
            ),
        },
        "integrity": integrity,
        "aggregate": aggregates,
        "paired": paired,
        "old_godot_host_execution": _godot_usage(rows["old_godot_host_execution"]),
        "transparent_pinned_godot_transport": transparent_usage,
        "outcome_patterns_baseline_toolkit_old_godot_transparent": dict(
            sorted(patterns.items())
        ),
        "repeated_baseline_pass_both_host_transports_fail": repeated_host_regressions,
        "strata": _strata(rows),
        "per_task": per_task,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
