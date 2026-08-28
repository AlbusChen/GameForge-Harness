#!/usr/bin/env python3
"""Analyze the outcome-blind paired GameDevBench blind64 three-way experiment."""

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

CONDITION_KEYS = ("baseline", "observation_toolkit", "godot_host_execution")
CRASH_EXIT_CODES = {-11, -6, 134, 139}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--observation-toolkit", required=True, type=Path)
    parser.add_argument("--godot-host-execution", required=True, type=Path)
    parser.add_argument("--rotation-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _wilson_interval(passes: int, attempts: int, z: float = 1.959963984540054) -> list[float]:
    proportion = passes / attempts
    denominator = 1.0 + z * z / attempts
    center = (proportion + z * z / (2.0 * attempts)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / attempts
            + z * z / (4.0 * attempts * attempts)
        )
        / denominator
    )
    return [round(center - margin, 6), round(center + margin, 6)]


def _receipts(experiment: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    complete_path = experiment / "complete.json"
    progress_path = experiment / "progress.json"
    freeze_path = experiment / "protocol-freeze.json"
    if not all(path.is_file() for path in (complete_path, progress_path, freeze_path)):
        raise RuntimeError(f"experiment is incomplete: {experiment}")
    complete = _read_json(complete_path)
    progress = _read_json(progress_path)
    freeze = _read_json(freeze_path)
    rows = [_read_json(path) for path in sorted((experiment / "receipts").glob("*.json"))]
    by_task = {str(row["task_id"]): row for row in rows}
    if len(rows) != 64 or len(by_task) != 64:
        raise RuntimeError(f"expected 64 unique receipts under {experiment}; got {len(rows)}")
    if complete.get("attempts_completed") != 64 or progress.get("attempts_completed") != 64:
        raise RuntimeError(f"completion markers disagree under {experiment}")
    if complete.get("runtime_tree_sha256") != freeze.get("runtime_tree_sha256"):
        raise RuntimeError(f"runtime hashes disagree under {experiment}")
    for row in rows:
        result_source = Path(str(row["result_source"]))
        if not result_source.is_file() or _sha256(result_source) != row["result_source_sha256"]:
            raise RuntimeError(f"receipt source hash mismatch: {result_source}")
    metadata = {
        "experiment": str(experiment),
        "condition": freeze["condition"],
        "complete": complete,
        "progress": progress,
        "protocol_freeze": freeze,
        "receipt_count": len(rows),
        "receipt_sources_verified": len(rows),
    }
    return by_task, metadata


def _command_audit(row: dict[str, Any]) -> dict[str, Any]:
    audit_path = Path(row["record"]["run_directory"]) / "agent-tool-audit.json"
    events = _read_json(audit_path).get("events", [])
    command_events = [event for event in events if event.get("kind") == "command_execution"]
    exit_counts = Counter(int(event["exit_code"]) for event in command_events)
    crash_counts = Counter(
        int(event["exit_code"])
        for event in command_events
        if int(event["exit_code"]) in CRASH_EXIT_CODES
    )
    return {
        "command_count": len(command_events),
        "exit_counts": exit_counts,
        "crash_counts": crash_counts,
        "has_nonzero": any(code != 0 for code in exit_counts),
        "has_crash_exit": bool(crash_counts),
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    records = [row["record"] for row in rows]
    durations = [float(record["duration_seconds"]) for record in records]
    passes = sum(row["normalized_status"] == "PASS" for row in rows)
    numeric_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "tool_calls",
        "engine_crash_retries",
    )
    totals = {
        field: sum(int(record.get(field) or 0) for record in records)
        for field in numeric_fields
    }
    audits = [(str(row["task_id"]), _command_audit(row)) for row in rows]
    exit_counts: Counter[int] = Counter()
    crash_counts: Counter[int] = Counter()
    for _, audit in audits:
        exit_counts.update(audit["exit_counts"])
        crash_counts.update(audit["crash_counts"])
    crash_tasks = [task_id for task_id, audit in audits if audit["has_crash_exit"]]
    nonzero_tasks = [task_id for task_id, audit in audits if audit["has_nonzero"]]
    return {
        "attempts": len(rows),
        "passes": passes,
        "pass_rate": round(passes / len(rows), 6),
        "pass_rate_percent": round(100.0 * passes / len(rows), 3),
        "pass_rate_wilson_95": _wilson_interval(passes, len(rows)),
        "status_counts": dict(
            sorted(Counter(str(row["normalized_status"]) for row in rows).items())
        ),
        "official_verdict_count": sum(
            record.get("official_gate") in {"PASS", "FAIL"} for record in records
        ),
        "duration_seconds": {
            "total": round(sum(durations), 3),
            "mean": round(statistics.mean(durations), 3),
            "median": round(statistics.median(durations), 3),
            "p95": round(_percentile(durations, 0.95), 3),
            "maximum": round(max(durations), 3),
        },
        "totals": totals,
        "infrastructure_retry_count": sum(
            len(row.get("infrastructure_retries", [])) for row in rows
        ),
        "nonzero_runner_exit_count": sum(int(row["runner_exit_code"]) != 0 for row in rows),
        "agent_command_audit": {
            "command_count": sum(audit["command_count"] for _, audit in audits),
            "exit_code_counts": {str(code): count for code, count in sorted(exit_counts.items())},
            "nonzero_exit_task_count": len(nonzero_tasks),
            "nonzero_exit_tasks": sorted(nonzero_tasks),
            "crash_exit_codes": sorted(CRASH_EXIT_CODES),
            "crash_exit_count": sum(crash_counts.values()),
            "crash_exit_code_counts": {
                str(code): count for code, count in sorted(crash_counts.items())
            },
            "crash_exit_task_count": len(crash_tasks),
            "crash_exit_tasks": sorted(crash_tasks),
        },
    }


def _mcnemar_exact_two_sided(baseline_only: int, candidate_only: int) -> float:
    discordant = baseline_only + candidate_only
    if discordant == 0:
        return 1.0
    smaller = min(baseline_only, candidate_only)
    tail = sum(math.comb(discordant, index) for index in range(smaller + 1)) / (2**discordant)
    return min(1.0, 2.0 * tail)


def _paired(
    baseline: dict[str, dict[str, Any]], candidate: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    baseline_only_tasks: list[str] = []
    candidate_only_tasks: list[str] = []
    duration_deltas: list[tuple[str, float, float, float]] = []
    token_deltas: list[tuple[str, int]] = []
    tool_call_deltas: list[tuple[str, int]] = []
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
        if outcome == "baseline_only":
            baseline_only_tasks.append(task_id)
        elif outcome == "candidate_only":
            candidate_only_tasks.append(task_id)
        base_record = baseline_row["record"]
        candidate_record = candidate_row["record"]
        baseline_duration = float(base_record["duration_seconds"])
        candidate_duration = float(candidate_record["duration_seconds"])
        duration_deltas.append(
            (
                task_id,
                candidate_duration - baseline_duration,
                baseline_duration,
                candidate_duration,
            )
        )
        token_deltas.append(
            (task_id, int(candidate_record["input_tokens"]) - int(base_record["input_tokens"]))
        )
        tool_call_deltas.append(
            (task_id, int(candidate_record["tool_calls"]) - int(base_record["tool_calls"]))
        )
    largest = max(duration_deltas, key=lambda item: item[1])
    baseline_total = sum(item[2] for item in duration_deltas)
    candidate_total = sum(item[3] for item in duration_deltas)
    return {
        "both_pass": counts["both_pass"],
        "baseline_only": counts["baseline_only"],
        "candidate_only": counts["candidate_only"],
        "both_fail": counts["both_fail"],
        "baseline_only_tasks": baseline_only_tasks,
        "candidate_only_tasks": candidate_only_tasks,
        "net_pass_delta": counts["candidate_only"] - counts["baseline_only"],
        "mcnemar_exact_two_sided_p": round(
            _mcnemar_exact_two_sided(counts["baseline_only"], counts["candidate_only"]), 6
        ),
        "paired_efficiency": {
            "duration_delta_seconds": round(candidate_total - baseline_total, 3),
            "duration_ratio": round(candidate_total / baseline_total, 6),
            "median_task_duration_delta_seconds": round(
                statistics.median(item[1] for item in duration_deltas), 3
            ),
            "tasks_faster": sum(item[1] < 0 for item in duration_deltas),
            "tasks_slower": sum(item[1] > 0 for item in duration_deltas),
            "largest_positive_delta": {
                "task_id": largest[0],
                "seconds": round(largest[1], 3),
            },
            "duration_ratio_excluding_largest_positive_delta": round(
                (candidate_total - largest[3]) / (baseline_total - largest[2]), 6
            ),
            "input_token_delta": sum(delta for _, delta in token_deltas),
            "median_task_input_token_delta": round(
                statistics.median(delta for _, delta in token_deltas), 3
            ),
            "tool_call_delta": sum(delta for _, delta in tool_call_deltas),
            "median_task_tool_call_delta": round(
                statistics.median(delta for _, delta in tool_call_deltas), 3
            ),
        },
    }


def _observation_usage(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    commands: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    tasks: list[dict[str, Any]] = []
    copy_seconds: list[float] = []
    for task_id, row in sorted(rows.items()):
        run_directory = Path(row["record"]["run_directory"])
        toolkit = _read_json(run_directory / "observation-toolkit.json")
        events = _read_json(run_directory / "observation-tool-events.json").get("events", [])
        copy_seconds.append(float(toolkit["baseline_copy_seconds_outside_solver_timer"]))
        calls = int(toolkit["calls"])
        commands.update(str(event["command"]) for event in events)
        statuses.update(str(event["status"]) for event in events)
        tasks.append(
            {
                "task_id": task_id,
                "status": row["normalized_status"],
                "calls": calls,
                "commands": [str(event["command"]) for event in events],
            }
        )
    used = [task for task in tasks if task["calls"] > 0]
    unused = [task for task in tasks if task["calls"] == 0]
    return {
        "available_tasks": len(tasks),
        "used_tasks": len(used),
        "adoption_rate": round(len(used) / len(tasks), 6),
        "calls": sum(task["calls"] for task in tasks),
        "command_counts": dict(sorted(commands.items())),
        "event_status_counts": dict(sorted(statuses.items())),
        "used_task_passes": sum(task["status"] == "PASS" for task in used),
        "used_task_pass_rate": round(
            sum(task["status"] == "PASS" for task in used) / len(used), 6
        )
        if used
        else None,
        "unused_task_passes": sum(task["status"] == "PASS" for task in unused),
        "unused_task_pass_rate": round(
            sum(task["status"] == "PASS" for task in unused) / len(unused), 6
        )
        if unused
        else None,
        "baseline_copy_seconds_outside_solver_timer": {
            "total": round(sum(copy_seconds), 6),
            "mean": round(statistics.mean(copy_seconds), 6),
            "maximum": round(max(copy_seconds), 6),
        },
        "tasks": tasks,
    }


def _godot_usage(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return_codes: Counter[int] = Counter()
    tasks: list[dict[str, Any]] = []
    total_execution_seconds = 0.0
    for task_id, row in sorted(rows.items()):
        run_directory = Path(row["record"]["run_directory"])
        audit = _read_json(run_directory / "godot-host-execution-events.json")
        events = audit.get("events", [])
        codes = [int(event["return_code"]) for event in events]
        return_codes.update(codes)
        total_execution_seconds += sum(float(event["execution_seconds"]) for event in events)
        tasks.append(
            {
                "task_id": task_id,
                "status": row["normalized_status"],
                "calls": len(events),
                "return_codes": codes,
                "rejections": sum(bool(event["rejected"]) for event in events),
                "timeouts": sum(bool(event["timed_out"]) for event in events),
                "crash_retries": sum(int(event["crash_retries"]) for event in events),
            }
        )
    used = [task for task in tasks if task["calls"] > 0]
    return {
        "available_tasks": len(tasks),
        "used_tasks": len(used),
        "adoption_rate": round(len(used) / len(tasks), 6),
        "calls": sum(task["calls"] for task in tasks),
        "return_code_counts": {
            str(code): count for code, count in sorted(return_codes.items())
        },
        "bridge_crash_exit_count": sum(
            count for code, count in return_codes.items() if code in CRASH_EXIT_CODES
        ),
        "rejections": sum(task["rejections"] for task in tasks),
        "timeouts": sum(task["timeouts"] for task in tasks),
        "crash_retries": sum(task["crash_retries"] for task in tasks),
        "execution_seconds": round(total_execution_seconds, 3),
        "tasks": tasks,
    }


def _strata(rows_by_condition: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    strata = sorted({str(row["stratum"]) for row in rows_by_condition["baseline"].values()})
    output: dict[str, Any] = {}
    for stratum in strata:
        output[stratum] = {}
        for condition, rows in rows_by_condition.items():
            selected = [row for row in rows.values() if row["stratum"] == stratum]
            passes = sum(row["normalized_status"] == "PASS" for row in selected)
            output[stratum][condition] = {
                "attempts": len(selected),
                "passes": passes,
                "pass_rate": round(passes / len(selected), 6),
            }
    return output


def _rotation(rotation_path: Path) -> dict[str, Any]:
    raw = _read_json(rotation_path)
    events = raw.get("events", [])
    counts = Counter(str(event["condition"]) for event in events)
    failures = [event for event in events if int(event["runner_exit_code"]) != 0]
    return {
        "path": str(rotation_path),
        "event_count": len(events),
        "logged_blocks": sorted({int(event["block"]) for event in events}),
        "events_by_condition": dict(sorted(counts.items())),
        "nonzero_runner_exit_events": len(failures),
        "note": (
            "Block 1 was launched before the resumable rotation log was introduced; "
            "blocks 2-8 are fully logged and every experiment independently verifies 64 receipts."
        ),
    }


def main() -> int:
    args = _arguments()
    directories = {
        "baseline": args.baseline.resolve(strict=True),
        "observation_toolkit": args.observation_toolkit.resolve(strict=True),
        "godot_host_execution": args.godot_host_execution.resolve(strict=True),
    }
    rows_by_condition: dict[str, dict[str, dict[str, Any]]] = {}
    integrity: dict[str, Any] = {}
    for condition, directory in directories.items():
        rows_by_condition[condition], integrity[condition] = _receipts(directory)
    task_sets = [set(rows) for rows in rows_by_condition.values()]
    if not all(task_set == task_sets[0] for task_set in task_sets[1:]):
        raise RuntimeError("condition task sets differ")
    task_order = {
        condition: [
            row["task_id"]
            for row in sorted(rows.values(), key=lambda value: int(value["manifest_order"]))
        ]
        for condition, rows in rows_by_condition.items()
    }
    if not all(order == task_order["baseline"] for order in task_order.values()):
        raise RuntimeError("condition manifest orders differ")

    aggregates = {
        condition: _aggregate([rows[task_id] for task_id in sorted(rows)])
        for condition, rows in rows_by_condition.items()
    }
    paired = {
        "observation_toolkit_vs_baseline": _paired(
            rows_by_condition["baseline"], rows_by_condition["observation_toolkit"]
        ),
        "godot_host_execution_vs_baseline": _paired(
            rows_by_condition["baseline"], rows_by_condition["godot_host_execution"]
        ),
    }
    threeway_patterns: Counter[str] = Counter()
    per_task: list[dict[str, Any]] = []
    for task_id in sorted(rows_by_condition["baseline"]):
        statuses = {
            condition: rows[task_id]["normalized_status"]
            for condition, rows in rows_by_condition.items()
        }
        pattern = "/".join(
            "P" if statuses[condition] == "PASS" else "F" for condition in CONDITION_KEYS
        )
        threeway_patterns[pattern] += 1
        per_task.append(
            {
                "task_id": task_id,
                "task_name": rows_by_condition["baseline"][task_id]["task_name"],
                "stratum": rows_by_condition["baseline"][task_id]["stratum"],
                "outcome_pattern_baseline_toolkit_godot": pattern,
                "conditions": {
                    condition: {
                        "status": rows[task_id]["normalized_status"],
                        "duration_seconds": rows[task_id]["record"]["duration_seconds"],
                        "input_tokens": rows[task_id]["record"]["input_tokens"],
                        "output_tokens": rows[task_id]["record"]["output_tokens"],
                        "tool_calls": rows[task_id]["record"]["tool_calls"],
                    }
                    for condition, rows in rows_by_condition.items()
                },
            }
        )

    official = integrity["baseline"]["protocol_freeze"]["official_reference"]
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "design": {
            "type": "outcome-blind paired three-way screen",
            "task_count": 64,
            "repetitions_per_task": 1,
            "selection": "stratified fixed-seed selection using task_id and stratum only",
            "pilot_overlap": 0,
            "same_task_order": True,
            "conditions_run_nonconcurrently": True,
            "warning": (
                "This 64-task single-repetition screen estimates paired candidate effects on the "
                "selected blind subset; efficiency and individual task flips include model "
                "stochasticity."
            ),
        },
        "integrity": integrity,
        "rotation": _rotation(args.rotation_log.resolve(strict=True)),
        "official_reference": official,
        "aggregate": aggregates,
        "paired": paired,
        "threeway_outcome_patterns_baseline_toolkit_godot": dict(
            sorted(threeway_patterns.items())
        ),
        "strata": _strata(rows_by_condition),
        "observation_toolkit": _observation_usage(rows_by_condition["observation_toolkit"]),
        "godot_host_execution": _godot_usage(rows_by_condition["godot_host_execution"]),
        "per_task": per_task,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
