#!/usr/bin/env python3
"""Validate and analyze the paired fresh-50 Official versus v4 experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = ("official-default", "programmable-open-global-diagnostic-v4")
SAMPLES = 100_000
SEED = 20260811


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-fresh50-official-v4-v1",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/gamedevbench-fresh50-v1.json",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _evaluator_status(row: dict[str, Any]) -> str:
    if row["condition"] == "official-default":
        return "PASS" if row["normalized_status"] == "PASS" else "FAIL"
    value = str(row["record"].get("official_gate", "NOT_RUN"))
    return value if value in {"PASS", "FAIL"} else "NOT_RUN"


def _duration(row: dict[str, Any]) -> float | None:
    record = row["record"]
    value = record.get("duration_seconds", record.get("solver_duration"))
    return float(value) if isinstance(value, int | float) else None


def _numeric_total(rows: list[dict[str, Any]], key: str) -> tuple[int, int]:
    """Return the known total and preserve how many observations were unknown.

    Synthetic runner receipts can legitimately use ``null`` when an outer timeout
    prevents the child process from reporting its counters.  Treating those values
    as zero would make the resource report look more complete than it is.
    """
    values = [row["record"].get(key) for row in rows]
    known = [int(value) for value in values if isinstance(value, int | float)]
    return sum(known), len(values) - len(known)


def _resource(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [value for row in rows if (value := _duration(row)) is not None]
    model_turns, model_turns_missing = _numeric_total(rows, "model_turns")
    tool_calls, tool_calls_missing = _numeric_total(rows, "tool_calls")
    token_totals: dict[str, int] = {}
    token_missing: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        token_totals[key], token_missing[key] = _numeric_total(rows, key)
    return {
        "duration_seconds": {
            "total": sum(durations),
            "median": statistics.median(durations) if durations else None,
            "p95": _percentile(durations, 0.95),
            "max": max(durations, default=None),
            "missing": len(rows) - len(durations),
        },
        "model_turns": model_turns,
        "tool_calls": tool_calls,
        "tokens": token_totals,
        "missing": {
            "model_turns": model_turns_missing,
            "tool_calls": tool_calls_missing,
            "tokens": token_missing,
        },
    }


def _candidate_trace_metrics(row: dict[str, Any]) -> dict[str, Any]:
    run_directory = row["record"].get("run_directory")
    if not isinstance(run_directory, str):
        return {"program_cell_errors": 0, "decision_errors": 0, "errors": []}
    source = Path(run_directory) / "agent-result.json"
    if not source.is_file():
        return {"program_cell_errors": 0, "decision_errors": 0, "errors": []}
    payload = json.loads(source.read_text(encoding="utf-8"))
    trace = payload.get("trace", [])
    if not isinstance(trace, list):
        return {"program_cell_errors": 0, "decision_errors": 0, "errors": []}
    cell_errors = [
        str(item.get("error", "unknown program-cell error"))
        for item in trace
        if isinstance(item, dict) and item.get("event") == "program_cell_error"
    ]
    decision_errors = [
        str(item.get("error", "unknown structured-decision error"))
        for item in trace
        if isinstance(item, dict) and item.get("event") == "model_program_decision_error"
    ]
    return {
        "program_cell_errors": len(cell_errors),
        "decision_errors": len(decision_errors),
        "errors": [*cell_errors, *decision_errors],
    }


def _mcnemar(first: list[bool], second: list[bool]) -> dict[str, Any]:
    first_only = sum(a and not b for a, b in zip(first, second, strict=True))
    second_only = sum(b and not a for a, b in zip(first, second, strict=True))
    discordant = first_only + second_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(math.comb(discordant, k) for k in range(min(first_only, second_only) + 1))
        p_value = min(1.0, 2 * tail / (2**discordant))
    return {
        "first_only": first_only,
        "second_only": second_only,
        "discordant": discordant,
        "two_sided_exact_p": p_value,
    }


def main() -> int:
    arguments = _arguments()
    experiment = arguments.experiment.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schedule = json.loads((experiment / "schedule.json").read_text(encoding="utf-8"))
    protocol = json.loads((experiment / "protocol-freeze.json").read_text(encoding="utf-8"))
    complete = json.loads((experiment / "complete.json").read_text(encoding="utf-8"))
    receipt_paths = sorted((experiment / "receipts").glob("*.json"))
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in receipt_paths]
    errors: list[str] = []
    if len(rows) != 100:
        errors.append(f"expected 100 receipts, observed {len(rows)}")
    if complete.get("attempts_completed") != 100:
        errors.append("complete marker does not report 100 attempts")
    if protocol.get("manifest_sha256") != _sha256(manifest_path):
        errors.append("manifest digest mismatch")
    if protocol.get("schedule_sha256") != _sha256(experiment / "schedule.json"):
        errors.append("schedule digest mismatch")
    expected_ids = [item["attempt_id"] for item in schedule["attempts"]]
    observed_ids = [item["attempt_id"] for item in rows]
    if expected_ids != observed_ids:
        errors.append("receipt order/identity differs from schedule")
    for row in rows:
        source = Path(row["result_source"])
        if not source.is_file() or _sha256(source) != row["result_source_sha256"]:
            errors.append(f"result source mismatch: {row['attempt_id']}")

    by_condition = {
        condition: [row for row in rows if row["condition"] == condition]
        for condition in CONDITIONS
    }
    overall: dict[str, Any] = {}
    for condition, items in by_condition.items():
        evaluator = Counter(_evaluator_status(row) for row in items)
        terminal = Counter(str(row["normalized_status"]) for row in items)
        overall[condition] = {
            "attempts": len(items),
            "evaluator": {
                "status_counts": dict(sorted(evaluator.items())),
                "pass_rate": evaluator["PASS"] / len(items),
            },
            "terminal": {
                "status_counts": dict(sorted(terminal.items())),
                "pass_rate": terminal["PASS"] / len(items),
            },
            "by_stratum": {
                stratum: {
                    "attempts": len(group),
                    "passes": sum(_evaluator_status(row) == "PASS" for row in group),
                    "pass_rate": sum(
                        _evaluator_status(row) == "PASS" for row in group
                    )
                    / len(group),
                }
                for stratum in sorted({row["stratum"] for row in items})
                if (group := [row for row in items if row["stratum"] == stratum])
            },
            "by_novelty_tier": {
                tier: {
                    "attempts": len(group),
                    "passes": sum(_evaluator_status(row) == "PASS" for row in group),
                    "pass_rate": sum(
                        _evaluator_status(row) == "PASS" for row in group
                    )
                    / len(group),
                }
                for tier in sorted({
                    task["novelty_tier"] for task in manifest["tasks"]
                })
                if (
                    group := [
                        row
                        for row in items
                        if next(
                            task
                            for task in manifest["tasks"]
                            if task["task_id"] == row["task_id"]
                        )["novelty_tier"]
                        == tier
                    ]
                )
            },
            "resources": _resource(items),
        }

    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[row["task_id"]][row["condition"]] = row
    tasks: list[dict[str, Any]] = []
    deltas: list[float] = []
    official_bools: list[bool] = []
    candidate_bools: list[bool] = []
    for task in manifest["tasks"]:
        task_id = task["task_id"]
        official = grouped[task_id]["official-default"]
        candidate = grouped[task_id]["programmable-open-global-diagnostic-v4"]
        official_pass = _evaluator_status(official) == "PASS"
        candidate_pass = _evaluator_status(candidate) == "PASS"
        delta = float(candidate_pass) - float(official_pass)
        deltas.append(delta)
        official_bools.append(official_pass)
        candidate_bools.append(candidate_pass)
        tasks.append(
            {
                "task_id": task_id,
                "task_name": task["name"],
                "stratum": task["stratum"],
                "novelty_tier": task["novelty_tier"],
                "official": _evaluator_status(official),
                "v4_evaluator": _evaluator_status(candidate),
                "v4_terminal": candidate["normalized_status"],
                "delta": delta,
                "v4_failure_detail": candidate["record"].get("failure_detail"),
                "v4_trace": _candidate_trace_metrics(candidate),
                "official_result_source": official["result_source"],
                "v4_result_source": candidate["result_source"],
            }
        )

    rng = random.Random(SEED)
    boot = [
        statistics.fmean(deltas[rng.randrange(len(deltas))] for _ in deltas)
        for _ in range(SAMPLES)
    ]
    candidate_rows = by_condition["programmable-open-global-diagnostic-v4"]
    preservation = Counter(
        str(row["record"].get("preservation_gate", "NOT_RUN"))
        for row in candidate_rows
    )
    policy, policy_missing = _numeric_total(candidate_rows, "policy_violations")
    candidate_traces = [_candidate_trace_metrics(row) for row in candidate_rows]
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {
            "valid": not errors,
            "errors": errors,
            "expected_receipts": 100,
            "observed_receipts": len(rows),
        },
        "inputs": {
            "experiment": str(experiment),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "schedule_sha256": _sha256(experiment / "schedule.json"),
            "runtime_tree_sha256": protocol["runtime_tree_sha256"],
        },
        "overall": overall,
        "paired": {
            "v4_minus_official": {
                "estimate": statistics.fmean(deltas),
                "ci95": [_percentile(boot, 0.025), _percentile(boot, 0.975)],
                "method": "paired task bootstrap percentile interval",
                "samples": SAMPLES,
                "seed": SEED,
            },
            "mcnemar": _mcnemar(candidate_bools, official_bools),
        },
        "candidate_controls": {
            "preservation_gate": dict(sorted(preservation.items())),
        "policy_violations": policy,
        "policy_violation_metric_missing": policy_missing,
            "lineage_complete": Counter(
                bool(row["record"].get("lineage_complete"))
                for row in candidate_rows
            ),
            "mean_required_evidence_coverage": statistics.fmean(
                float(row["record"].get("required_evidence_coverage", 0)) for row in candidate_rows
            ),
            "execution_errors": {
                "program_cell_errors": sum(
                    int(trace["program_cell_errors"]) for trace in candidate_traces
                ),
                "decision_errors": sum(
                    int(trace["decision_errors"]) for trace in candidate_traces
                ),
                "tasks_with_errors": sum(bool(trace["errors"]) for trace in candidate_traces),
                "messages": dict(
                    sorted(
                        Counter(
                            error
                            for trace in candidate_traces
                            for error in trace["errors"]
                        ).items()
                    )
                ),
            },
        },
        "quota": {
            "minimum_remaining_percent_observed": 100
            - max(float(row["weekly_used_percent_before"]) for row in rows)
        },
        "tasks": tasks,
    }
    output = experiment / "analysis.json"
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(output)
    if errors:
        raise RuntimeError("fresh50 validation failed: " + "; ".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
