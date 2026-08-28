#!/usr/bin/env python3
"""Validate and summarize the complete qualified GameDevBench run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_fresh50_official_v4 import _candidate_trace_metrics, _resource
from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TASKS = 332
VALIDATION_FAILURE = re.compile(r"VALIDATION_FAILED(?::\s*(.+))?")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-provenance-v19-run1",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/gamedevbench-full-qualified332-v1.json",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _wilson(successes: int, attempts: int) -> list[float] | None:
    if attempts == 0:
        return None
    z = 1.959963984540054
    estimate = successes / attempts
    denominator = 1 + z**2 / attempts
    centre = estimate + z**2 / (2 * attempts)
    margin = z * math.sqrt(estimate * (1 - estimate) / attempts + z**2 / (4 * attempts**2))
    return [(centre - margin) / denominator, (centre + margin) / denominator]


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


def _family_metrics(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Report both task-weighted and family-balanced robustness.

    GameDevBench contains large rewritten families. Treating every rewrite as independent
    understates uncertainty and lets a single family dominate the headline, so the report
    retains the ordinary task-weighted score while also resampling whole families.
    """
    family_rows: list[dict[str, Any]] = []
    for family, rows in sorted(groups.items()):
        passes = sum(row["normalized_status"] == "PASS" for row in rows)
        family_rows.append(
            {
                "family_key": family,
                "passes": passes,
                "attempts": len(rows),
                "pass_rate": _rate(passes, len(rows)),
                "task_ids": [str(row["task_id"]) for row in rows],
            }
        )

    rates = [float(row["pass_rate"]) for row in family_rows]
    repeated = [row for row in family_rows if int(row["attempts"]) > 1]
    rng = random.Random(20260813)
    cluster_rates: list[float] = []
    macro_rates: list[float] = []
    if family_rows:
        for _ in range(20_000):
            sample = [rng.choice(family_rows) for _ in family_rows]
            passes = sum(int(row["passes"]) for row in sample)
            attempts = sum(int(row["attempts"]) for row in sample)
            cluster_rates.append(passes / attempts)
            macro_rates.append(sum(float(row["pass_rate"]) for row in sample) / len(sample))
    return {
        "family_count": len(family_rows),
        "repeated_family_count": len(repeated),
        "maximum_family_size": max((int(row["attempts"]) for row in family_rows), default=0),
        "family_macro_pass_rate": sum(rates) / len(rates) if rates else None,
        "family_macro_cluster_bootstrap_ci95": [
            _percentile(macro_rates, 0.025),
            _percentile(macro_rates, 0.975),
        ],
        "task_weighted_cluster_bootstrap_ci95": [
            _percentile(cluster_rates, 0.025),
            _percentile(cluster_rates, 0.975),
        ],
        "repeated_families": sorted(
            repeated,
            key=lambda row: (float(row["pass_rate"]), -int(row["attempts"]), row["family_key"]),
        ),
        "all_families": family_rows,
        "interpretation": (
            "The ordinary headline remains task weighted. Family-macro and whole-family "
            "bootstrap results expose robustness and dependence across rewritten variants."
        ),
    }


def _duration_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        float(row["record"]["duration_seconds"])
        for row in rows
        if isinstance(row["record"].get("duration_seconds"), int | float)
    ]
    values.sort()
    return {
        "count": len(values),
        "median_seconds": _percentile(values, 0.5),
        "p90_seconds": _percentile(values, 0.9),
        "p95_seconds": _percentile(values, 0.95),
        "maximum_seconds": max(values, default=None),
        "total_seconds": sum(values),
    }


def _trace_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions: Counter[str] = Counter()
    capabilities: Counter[str] = Counter()
    transport_attempts: Counter[int] = Counter()
    tasks_with_trace = 0
    tasks_with_rework = 0
    tasks_with_native_agent = 0
    native_agent_audit_files = 0
    native_agent_audit_events = 0
    native_agent_outside_project_changes = 0
    program_errors = 0
    decision_errors = 0
    error_messages: Counter[str] = Counter()
    for row in rows:
        run_directory = row["record"].get("run_directory")
        if not isinstance(run_directory, str):
            continue
        source = Path(run_directory) / "agent-result.json"
        if not source.is_file():
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        trace = payload.get("trace", [])
        if not isinstance(trace, list):
            continue
        tasks_with_trace += 1
        task_rework = 0
        task_used_native_agent = False
        for item in trace:
            if not isinstance(item, dict):
                continue
            attempts = int(item.get("transport_attempts", 1))
            transport_attempts[attempts] += 1
            event = item.get("event")
            if event == "program_cell_error":
                program_errors += 1
                task_rework += 1
                error_messages[str(item.get("error", "unknown program-cell error"))] += 1
            elif event == "model_program_decision_error":
                decision_errors += 1
                task_rework += 1
                error_messages[str(item.get("error", "unknown structured-decision error"))] += 1
            elif event == "model_program_decision":
                decision = item.get("decision", {})
                if not isinstance(decision, dict):
                    continue
                kind = str(decision.get("kind", "unknown"))
                decisions[kind] += 1
                if kind == "capability":
                    capabilities[str(decision.get("capability", "unknown"))] += 1
                elif kind == "agent":
                    task_used_native_agent = True
        tasks_with_rework += task_rework > 0
        tasks_with_native_agent += task_used_native_agent
        audit_paths = sorted((Path(run_directory) / "workspace-agent").glob("trajectory-*.json"))
        native_agent_audit_files += len(audit_paths)
        for audit_path in audit_paths:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            events = audit.get("events", [])
            if not isinstance(events, list):
                continue
            native_agent_audit_events += len(events)
            for event in events:
                if not isinstance(event, dict) or event.get("kind") != "file_change":
                    continue
                changes = event.get("changes", [])
                if isinstance(changes, list):
                    native_agent_outside_project_changes += sum(
                        isinstance(change, dict) and change.get("within_project") is False
                        for change in changes
                    )
    action_count = sum(decisions[kind] for kind in ("capability", "workspace", "agent", "cell"))
    return {
        "tasks_with_trace": tasks_with_trace,
        "decisions": dict(sorted(decisions.items())),
        "capabilities": dict(capabilities.most_common()),
        "native_workspace_agent": {
            "tasks_using": tasks_with_native_agent,
            "task_rate": _rate(tasks_with_native_agent, tasks_with_trace),
            "audit_files": native_agent_audit_files,
            "audit_events": native_agent_audit_events,
            "outside_project_file_changes_reported": native_agent_outside_project_changes,
        },
        "first_class_capability_share_of_actions": _rate(decisions["capability"], action_count),
        "transport_attempts": {
            str(key): value for key, value in sorted(transport_attempts.items())
        },
        "execution_rework": {
            "program_cell_errors": program_errors,
            "structured_decision_errors": decision_errors,
            "tasks_with_errors": tasks_with_rework,
            "first_pass_task_rate": _rate(tasks_with_trace - tasks_with_rework, tasks_with_trace),
            "messages": dict(error_messages.most_common()),
        },
    }


def _native_tool_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks_with_audit = 0
    audit_events = 0
    command_events = 0
    failed_command_events = 0
    exit_134_events = 0
    tasks_with_exit_134 = 0
    reported_outside_change_events = 0
    tasks_with_reported_outside_changes = 0
    for row in rows:
        run_directory = row["record"].get("run_directory")
        if not isinstance(run_directory, str):
            continue
        source = Path(run_directory) / "agent-tool-audit.json"
        if not source.is_file():
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        events = payload.get("events", [])
        if not isinstance(events, list):
            continue
        tasks_with_audit += 1
        audit_events += len(events)
        task_exit_134 = 0
        task_outside = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("kind") == "command_execution":
                command_events += 1
                exit_code = event.get("exit_code")
                if exit_code in {-11, -6, 134, 139}:
                    exit_134_events += 1
                    task_exit_134 += 1
                if event.get("status") == "failed" or (
                    isinstance(exit_code, int) and exit_code != 0
                ):
                    failed_command_events += 1
            elif event.get("kind") == "file_change":
                task_outside += sum(
                    isinstance(change, dict) and change.get("within_project") is False
                    for change in event.get("changes", [])
                )
        tasks_with_exit_134 += task_exit_134 > 0
        reported_outside_change_events += task_outside
        tasks_with_reported_outside_changes += task_outside > 0
    return {
        "tasks_with_audit": tasks_with_audit,
        "audit_events": audit_events,
        "command_events": command_events,
        "failed_command_events": failed_command_events,
        "exit_134_events": exit_134_events,
        "tasks_with_exit_134": tasks_with_exit_134,
        "reported_outside_change_events": reported_outside_change_events,
        "tasks_with_reported_outside_changes": tasks_with_reported_outside_changes,
    }


def _failure_messages(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    messages: dict[str, list[str]] = {}
    for row in rows:
        if row["normalized_status"] == "PASS":
            continue
        run_directory = row["record"].get("run_directory")
        if not isinstance(run_directory, str):
            continue
        source = Path(run_directory) / "official-evaluator.log"
        if not source.is_file():
            continue
        found = [
            match.group(1) or "unspecified evaluator failure"
            for line in source.read_text(encoding="utf-8").splitlines()
            if (match := VALIDATION_FAILURE.search(line))
        ]
        messages[str(row["task_id"])] = found
    return messages


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row["normalized_status"]) for row in rows)
    passes = statuses["PASS"]
    errors = statuses["ERROR"]
    conditional_attempts = len(rows) - errors
    evaluator = Counter(str(row["record"].get("official_gate", "NOT_RUN")) for row in rows)
    return {
        "eligible_tasks": len(rows),
        "status_counts": dict(sorted(statuses.items())),
        "all_eligible_lower_bound": {
            "passes": passes,
            "attempts": len(rows),
            "pass_rate": _rate(passes, len(rows)),
            "wilson_ci95": _wilson(passes, len(rows)),
            "interpretation": "ERROR and BLOCKED both count as non-passes.",
        },
        "conditional_accuracy": {
            "passes": passes,
            "attempts": conditional_attempts,
            "pass_rate": _rate(passes, conditional_attempts),
            "wilson_ci95": _wilson(passes, conditional_attempts),
            "interpretation": (
                "Only repeated zero-turn infrastructure ERROR is excluded; BLOCKED remains "
                "a Harness failure in the denominator."
            ),
        },
        "official_evaluator": {
            "status_counts": dict(sorted(evaluator.items())),
            "coverage": _rate(evaluator["PASS"] + evaluator["FAIL"], len(rows)),
        },
    }


def _validate(
    *,
    experiment: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    schedule: dict[str, Any],
    protocol: dict[str, Any],
    complete: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if len(rows) != EXPECTED_TASKS:
        errors.append(f"expected {EXPECTED_TASKS} receipts, observed {len(rows)}")
    if complete.get("attempts_completed") != EXPECTED_TASKS:
        errors.append("complete marker count mismatch")
    if protocol.get("manifest_sha256") != _sha256(manifest_path):
        errors.append("manifest digest mismatch")
    if protocol.get("schedule_sha256") != _sha256(experiment / "schedule.json"):
        errors.append("schedule digest mismatch")
    expected = schedule.get("attempts", [])
    if [item.get("attempt_id") for item in expected] != [row.get("attempt_id") for row in rows]:
        errors.append("receipt order or identity differs from schedule")
    manifest_ids = [str(task["task_id"]) for task in manifest["tasks"]]
    if manifest_ids != [str(row.get("task_id")) for row in rows]:
        errors.append("receipt task order differs from manifest")
    for row in rows:
        source = Path(str(row.get("result_source", "")))
        if not source.is_file() or _sha256(source) != row.get("result_source_sha256"):
            errors.append(f"result source mismatch: {row.get('attempt_id')}")
    frozen = manifest["code_freeze"]
    runtime_audit_path = experiment / "runtime-resume-audit.json"
    runtime_root = ROOT
    if runtime_audit_path.is_file():
        runtime_audit = json.loads(runtime_audit_path.read_text(encoding="utf-8"))
        archive = experiment / str(runtime_audit.get("frozen_runtime_archive", ""))
        if not archive.is_file() or _sha256(archive) != runtime_audit.get(
            "frozen_runtime_archive_sha256"
        ):
            errors.append("frozen Harness runtime archive mismatch")
        declared_root = Path(str(runtime_audit.get("frozen_runtime_root", "")))
        if declared_root.is_dir():
            runtime_root = declared_root
        else:
            errors.append("frozen Harness runtime verification root is unavailable")
        if runtime_audit.get("frozen_runtime_tree_sha256") != frozen[
            "runtime_tree_sha256"
        ]:
            errors.append("runtime resume audit digest differs from manifest freeze")
        if runtime_audit.get("frozen_runtime_file_count") != frozen["runtime_file_count"]:
            errors.append("runtime resume audit file count differs from manifest freeze")
    digest, count = _runtime_digest(runtime_root)
    if digest != frozen["runtime_tree_sha256"] or count != frozen["runtime_file_count"]:
        errors.append("verified Harness runtime differs from manifest freeze")
    return errors


def main() -> int:
    arguments = _arguments()
    experiment = arguments.experiment.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schedule = json.loads((experiment / "schedule.json").read_text(encoding="utf-8"))
    protocol = json.loads((experiment / "protocol-freeze.json").read_text(encoding="utf-8"))
    complete = json.loads((experiment / "complete.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    errors = _validate(
        experiment=experiment,
        manifest_path=manifest_path,
        manifest=manifest,
        schedule=schedule,
        protocol=protocol,
        complete=complete,
        rows=rows,
    )
    headline = _summary(rows)
    by_stratum: dict[str, Any] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["stratum"])].append(row)
    for stratum, group in sorted(groups.items()):
        by_stratum[stratum] = _summary(group)

    manifest_by_task = {str(task["task_id"]): task for task in manifest["tasks"]}
    family_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        family = str(manifest_by_task[str(row["task_id"])]["family_key"])
        family_groups[family].append(row)

    official = manifest["official_reference"]
    official_rates = [
        passes / int(official["qualified_denominator_attempts"])
        for passes in official["qualified_denominator_pass_range"]
    ]
    candidate_rate = headline["all_eligible_lower_bound"]["pass_rate"]
    comparison = {
        "official_public_full": {
            "passes": official["passes"],
            "attempts": official["attempts"],
            "pass_rate": official["pass_rate"],
            "reported_pass_at_1_percent": official["reported_pass_at_1_percent"],
        },
        "official_qualified_rate_range": official_rates,
        "candidate_minus_official_qualified_range": [
            candidate_rate - official_rates[1],
            candidate_rate - official_rates[0],
        ],
        "inference_limit": (
            "Aggregate comparison only: the public Official artifact has no per-task "
            "outcomes, so a paired test is not available."
        ),
    }
    traces = [_candidate_trace_metrics(row) for row in rows]
    retries = [retry for row in rows for retry in row.get("infrastructure_retries", [])]
    preservation = Counter(str(row["record"].get("preservation_gate", "NOT_RUN")) for row in rows)
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "inputs": {
            "experiment": str(experiment),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "condition": protocol["condition"],
            "runtime_tree_sha256": protocol["runtime_tree_sha256"],
            "benchmark_commit": protocol["benchmark_commit"],
        },
        "headline": headline,
        "by_stratum": by_stratum,
        "family_robustness": _family_metrics(family_groups),
        "official_comparison": comparison,
        "controls": {
            "preservation_gate": dict(sorted(preservation.items())),
            "policy_violations": sum(
                int(value)
                for row in rows
                if (value := row["record"].get("policy_violations")) is not None
            ),
            "policy_violation_metric_missing": sum(
                row["record"].get("policy_violations") is None for row in rows
            ),
            "lineage_complete": dict(
                sorted(
                    Counter(
                        str(row["record"].get("lineage_complete", "MISSING")) for row in rows
                    ).items()
                )
            ),
            "infrastructure_retries": len(retries),
            "tasks_with_infrastructure_retry": sum(
                bool(row.get("infrastructure_retries")) for row in rows
            ),
        },
        "resources": _resource(rows),
        "latency": _duration_metrics(rows),
        "decision_substrate": _trace_audit(rows),
        "native_tool_audit": _native_tool_audit(rows),
        "trace_errors_cross_check": {
            "program_cell_errors": sum(int(trace["program_cell_errors"]) for trace in traces),
            "structured_decision_errors": sum(int(trace["decision_errors"]) for trace in traces),
        },
        "quota": {
            "minimum_remaining_percent_observed": 100
            - max(float(row["weekly_used_percent_before"]) for row in rows)
        },
        "failure_messages": _failure_messages(rows),
        "tasks": [
            {
                "task_id": row["task_id"],
                "task_name": row["task_name"],
                "stratum": row["stratum"],
                "family_key": manifest_by_task[str(row["task_id"])]["family_key"],
                "status": row["normalized_status"],
                "official_gate": row["record"].get("official_gate", "NOT_RUN"),
                "preservation_gate": row["record"].get("preservation_gate", "NOT_RUN"),
                "duration_seconds": row["record"].get("duration_seconds"),
                "model_turns": row["record"].get("model_turns", 0),
                "tool_calls": row["record"].get("tool_calls", 0),
            }
            for row in rows
        ],
    }
    output = experiment / "analysis.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["validation"], sort_keys=True))
    print(json.dumps(result["headline"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
