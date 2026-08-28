#!/usr/bin/env python3
"""Analyze a quota-stopped GameDevBench candidate on its exact matched task prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_gamedevbench_full import _percentile, _rate, _wilson
from experiments.analyze_gamedevbench_full_paired import _exact_mcnemar

ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--v31",
        type=Path,
        default=ROOT
        / "runs/experiments/gamedevbench-full-qualified332-host-managed-engine-queue-v31p4-run1",
    )
    parser.add_argument(
        "--v19",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-provenance-v19-run1",
    )
    parser.add_argument(
        "--official",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-official-run1",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _receipts(experiment: Path) -> list[dict[str, Any]]:
    rows = [_json(path) for path in (experiment / "receipts").glob("*.json")]
    return sorted(rows, key=lambda row: int(row["ordinal"]))


def _receipt_map(experiment: Path) -> dict[str, dict[str, Any]]:
    return {str(row["task_id"]): row for row in _receipts(experiment)}


def _status_summary(
    rows: list[dict[str, Any]], *, pass_key: str = "candidate_pass"
) -> dict[str, Any]:
    passes = sum(bool(row[pass_key]) for row in rows)
    return {
        "attempts": len(rows),
        "passes": passes,
        "pass_rate": _rate(passes, len(rows)),
        "wilson_ci95": _wilson(passes, len(rows)),
    }


def _comparison(
    rows: list[dict[str, Any]],
    *,
    reference_key: str,
    reference_label: str,
    candidate_key: str = "candidate_pass",
) -> dict[str, Any]:
    candidate_passes = sum(bool(row[candidate_key]) for row in rows)
    reference_passes = sum(bool(row[reference_key]) for row in rows)
    candidate_only = sum(row[candidate_key] and not row[reference_key] for row in rows)
    reference_only = sum(row[reference_key] and not row[candidate_key] for row in rows)
    both = sum(row[candidate_key] and row[reference_key] for row in rows)
    return {
        "attempts": len(rows),
        "candidate": {
            "passes": candidate_passes,
            "pass_rate": _rate(candidate_passes, len(rows)),
        },
        reference_label: {
            "passes": reference_passes,
            "pass_rate": _rate(reference_passes, len(rows)),
        },
        "candidate_minus_reference": _rate(candidate_passes - reference_passes, len(rows)),
        "paired_outcomes": {
            "both_pass": both,
            "candidate_only_pass": candidate_only,
            "reference_only_pass": reference_only,
            "neither_pass": len(rows) - both - candidate_only - reference_only,
        },
        "mcnemar_exact_two_sided_p": _exact_mcnemar(candidate_only, reference_only),
    }


def _family_bootstrap(
    rows: list[dict[str, Any]],
    *,
    reference_key: str,
    candidate_key: str = "candidate_pass",
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["family_key"])].append(row)
    families = list(grouped.values())
    rng = random.Random(20260817)
    differences: list[float] = []
    for _ in range(20_000):
        sample = [rng.choice(families) for _ in families]
        flat = [row for family in sample for row in family]
        differences.append(
            sum(int(row[candidate_key]) - int(row[reference_key]) for row in flat)
            / len(flat)
        )
    return {
        "observed_family_count": len(families),
        "candidate_minus_reference_ci95": [
            _percentile(differences, 0.025),
            _percentile(differences, 0.975),
        ],
        "replicates": len(differences),
        "seed": 20260817,
        "caveat": "Families are resampled only within the observed ordered prefix.",
    }


def _receipt_resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
    records = [row["record"] for row in rows]
    passes = sum(row["normalized_status"] == "PASS" for row in rows)
    durations = sorted(float(record.get("duration_seconds", 0)) for record in records)

    def total(key: str) -> int:
        return sum(int(record.get(key, 0)) for record in records)

    total_tokens = total("input_tokens") + total("output_tokens")
    total_seconds = sum(durations)
    return {
        "attempts": len(rows),
        "passes": passes,
        "duration_seconds": {
            "total": total_seconds,
            "mean_per_task": _rate(total_seconds, len(rows)),
            "median": _percentile(durations, 0.5),
            "p95": _percentile(durations, 0.95),
            "per_pass": _rate(total_seconds, passes),
        },
        "tokens": {
            "total": total_tokens,
            "mean_per_task": _rate(total_tokens, len(rows)),
            "per_pass": _rate(total_tokens, passes),
            "input": total("input_tokens"),
            "cached_input": total("cached_input_tokens"),
            "output": total("output_tokens"),
            "reasoning_output": total("reasoning_output_tokens"),
        },
        "model_turns": {
            "total": total("model_turns"),
            "mean_per_task": _rate(total("model_turns"), len(rows)),
        },
        "tool_calls": {
            "total": total("tool_calls"),
            "mean_per_task": _rate(total("tool_calls"), len(rows)),
        },
        "prompt_bytes": {
            "total": total("prompt_bytes"),
            "mean_per_task": _rate(total("prompt_bytes"), len(rows)),
        },
    }


def _official_resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
    passes = sum(row.get("success") is True for row in rows)
    durations = sorted(float(row.get("solver_duration", 0)) for row in rows)
    tokens = [int(row.get("total_tokens", 0)) for row in rows]
    return {
        "attempts": len(rows),
        "passes": passes,
        "duration_seconds": {
            "total": sum(durations),
            "mean_per_task": _rate(sum(durations), len(rows)),
            "median": _percentile(durations, 0.5),
            "p95": _percentile(durations, 0.95),
            "per_pass": _rate(sum(durations), passes),
        },
        "tokens": {
            "total": sum(tokens),
            "mean_per_task": _rate(sum(tokens), len(rows)),
            "per_pass": _rate(sum(tokens), passes),
        },
    }


def _candidate_tool_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks_with_exit_134 = 0
    exit_134_events = 0
    failed_command_events = 0
    tasks_with_outside_changes = 0
    outside_change_events = 0
    for row in rows:
        run_directory = Path(str(row["record"]["run_directory"]))
        audit = _json(run_directory / "agent-tool-audit.json").get("events", [])
        task_exit_134 = 0
        task_outside = 0
        for event in audit:
            if not isinstance(event, dict):
                continue
            if event.get("kind") == "command_execution":
                exit_code = event.get("exit_code")
                if exit_code in {-11, -6, 134, 139}:
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
        exit_134_events += task_exit_134
        outside_change_events += task_outside
        tasks_with_exit_134 += task_exit_134 > 0
        tasks_with_outside_changes += task_outside > 0
    return {
        "tasks_with_exit_134": tasks_with_exit_134,
        "exit_134_events": exit_134_events,
        "failed_command_events": failed_command_events,
        "tasks_with_reported_outside_changes": tasks_with_outside_changes,
        "reported_outside_change_events": outside_change_events,
        "interpretation": (
            "These are model-visible tool events retained by the native agent audit. Exit 134 "
            "did not prevent final Official coverage, but represents avoidable engine friction."
        ),
    }


def main() -> int:
    arguments = _arguments()
    candidate = arguments.candidate.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    v31 = arguments.v31.resolve(strict=True)
    v19 = arguments.v19.resolve(strict=True)
    official = arguments.official.resolve(strict=True)

    manifest = _json(manifest_path)
    manifest_by_task = {str(task["task_id"]): task for task in manifest["tasks"]}
    schedule = _json(candidate / "schedule.json")
    protocol = _json(candidate / "protocol-freeze.json")
    progress = _json(candidate / "progress.json")
    quota_stop = _json(candidate / "quota-stop.json")
    candidate_rows = _receipts(candidate)
    candidate_ids = [str(row["task_id"]) for row in candidate_rows]
    v31_by_task = _receipt_map(v31)
    v19_by_task = _receipt_map(v19)

    official_complete = _json(official / "complete.json")
    official_source = Path(str(official_complete["result_source"])).resolve(strict=True)
    official_payload = _json(official_source)
    official_by_task = {
        str(row["task_name"]): row for row in official_payload.get("tasks", [])
    }

    errors: list[str] = []
    if progress.get("attempts_completed") != len(candidate_rows):
        errors.append("progress count differs from receipt count")
    if quota_stop.get("stopped_before_ordinal") != len(candidate_rows) + 1:
        errors.append("quota stop ordinal does not follow the completed prefix")
    if protocol.get("manifest_sha256") != _sha256(manifest_path):
        errors.append("manifest digest mismatch")
    if protocol.get("schedule_sha256") != _sha256(candidate / "schedule.json"):
        errors.append("schedule digest mismatch")
    expected_schedule = schedule.get("attempts", [])[: len(candidate_rows)]
    if [item.get("attempt_id") for item in expected_schedule] != [
        row.get("attempt_id") for row in candidate_rows
    ]:
        errors.append("candidate receipts are not the exact schedule prefix")
    expected_ids = [str(task["task_id"]) for task in manifest["tasks"][: len(candidate_rows)]]
    if candidate_ids != expected_ids:
        errors.append("candidate receipts are not the exact manifest prefix")
    for row in candidate_rows:
        source = Path(str(row.get("result_source", "")))
        if not source.is_file() or _sha256(source) != row.get("result_source_sha256"):
            errors.append(f"result source mismatch: {row.get('attempt_id')}")
    for name, reference in (("v31", v31_by_task), ("v19", v19_by_task)):
        missing = sorted(set(candidate_ids) - set(reference))
        if missing:
            errors.append(f"{name} lacks matched tasks: {missing}")
    if _sha256(official_source) != official_complete.get("result_source_sha256"):
        errors.append("Official result source digest mismatch")
    missing_official = sorted(set(candidate_ids) - set(official_by_task))
    if missing_official:
        errors.append(f"Official lacks matched tasks: {missing_official}")

    rows: list[dict[str, Any]] = []
    for candidate_row in candidate_rows:
        task_id = str(candidate_row["task_id"])
        v31_row = v31_by_task[task_id]
        v19_row = v19_by_task[task_id]
        official_row = official_by_task[task_id]
        rows.append(
            {
                "task_id": task_id,
                "task_name": candidate_row["task_name"],
                "stratum": candidate_row["stratum"],
                "family_key": manifest_by_task[task_id]["family_key"],
                "candidate_status": candidate_row["normalized_status"],
                "candidate_pass": candidate_row["normalized_status"] == "PASS",
                "candidate_official_pass": candidate_row["record"].get("official_gate")
                == "PASS",
                "v31_pass": v31_row["normalized_status"] == "PASS",
                "v19_pass": v19_row["normalized_status"] == "PASS",
                "official_local_pass": official_row.get("success") is True,
            }
        )

    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[str(row["stratum"])].append(row)

    comparisons = {}
    for key, label in (
        ("v31_pass", "v31"),
        ("v19_pass", "v19"),
        ("official_local_pass", "official_local"),
    ):
        comparisons[label] = {
            "raw_protocol": {
                "headline": _comparison(rows, reference_key=key, reference_label=label),
                "family_bootstrap": _family_bootstrap(rows, reference_key=key),
            },
            "official_only_diagnostic": {
                "headline": _comparison(
                    rows,
                    reference_key=key,
                    reference_label=label,
                    candidate_key="candidate_official_pass",
                ),
                "family_bootstrap": _family_bootstrap(
                    rows,
                    reference_key=key,
                    candidate_key="candidate_official_pass",
                ),
                "caveat": (
                    "Post-hoc diagnostic only: this removes the v35 preservation gate from "
                    "the already generated candidates; it is not a formal v36 rerun."
                ),
            },
            "by_stratum": {
                stratum: _comparison(group, reference_key=key, reference_label=label)
                for stratum, group in sorted(strata.items())
            },
            "candidate_only_pass": [
                row["task_id"] for row in rows if row["candidate_pass"] and not row[key]
            ],
            "reference_only_pass": [
                row["task_id"] for row in rows if row[key] and not row["candidate_pass"]
            ],
        }

    candidate_receipt_map = {str(row["task_id"]): row for row in candidate_rows}
    v31_matched = [v31_by_task[task_id] for task_id in candidate_ids]
    v19_matched = [v19_by_task[task_id] for task_id in candidate_ids]
    official_matched = [official_by_task[task_id] for task_id in candidate_ids]
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "inference_scope": {
            "kind": "ordered_manifest_prefix",
            "completed_tasks": len(rows),
            "planned_tasks": len(manifest["tasks"]),
            "completion_rate": _rate(len(rows), len(manifest["tasks"])),
            "first_task_id": candidate_ids[0] if candidate_ids else None,
            "last_task_id": candidate_ids[-1] if candidate_ids else None,
            "stop_reason": "pre-registered weekly quota floor",
            "weekly_remaining_percent_at_stop": quota_stop.get("weekly_remaining_percent"),
            "headline_eligible": False,
            "caveat": (
                "This ordered prefix is valid for exact matched-task comparisons, but it is "
                "not a random sample and must not be reported as the Full332 headline."
            ),
        },
        "inputs": {
            "candidate": str(candidate),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "v31": str(v31),
            "v19": str(v19),
            "official_local": str(official),
            "official_result_source": str(official_source),
            "official_result_source_sha256": _sha256(official_source),
        },
        "candidate": _status_summary(rows),
        "candidate_official_only_diagnostic": {
            **_status_summary(rows, pass_key="candidate_official_pass"),
            "false_preservation_failures": [
                row["task_id"]
                for row in rows
                if row["candidate_official_pass"] and not row["candidate_pass"]
            ],
            "caveat": (
                "Post-hoc diagnostic only; the frozen v35 protocol headline remains 93/141."
            ),
        },
        "controls": {
            "official_verdict_coverage": _rate(
                sum(
                    candidate_receipt_map[task_id]["record"].get("official_gate")
                    in {"PASS", "FAIL"}
                    for task_id in candidate_ids
                ),
                len(candidate_ids),
            ),
            "preservation_passes": sum(
                candidate_receipt_map[task_id]["record"].get("preservation_gate") == "PASS"
                for task_id in candidate_ids
            ),
            "blocked": sum(row["normalized_status"] == "BLOCKED" for row in candidate_rows),
            "errors": sum(row["normalized_status"] == "ERROR" for row in candidate_rows),
            "infrastructure_retries": sum(
                len(row.get("infrastructure_retries", [])) for row in candidate_rows
            ),
            "native_tool_audit": _candidate_tool_audit(candidate_rows),
        },
        "comparisons": comparisons,
        "resources": {
            "candidate_minimal_open": _receipt_resources(candidate_rows),
            "v31_matched": _receipt_resources(v31_matched),
            "v19_matched": _receipt_resources(v19_matched),
            "official_local_matched": _official_resources(official_matched),
            "scope_note": (
                "Harness receipt duration and local Official solver_duration are retained as "
                "their native measurements; interpret cross-protocol latency as operational, "
                "not a microbenchmark of identical code regions."
            ),
        },
        "tasks": rows,
    }
    output = candidate / "partial-matched-analysis.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["validation"], sort_keys=True))
    print(json.dumps(result["candidate"], sort_keys=True))
    for label, comparison in comparisons.items():
        print(label, json.dumps(comparison["raw_protocol"]["headline"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
