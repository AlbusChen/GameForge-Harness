#!/usr/bin/env python3
"""Validate revised-candidate receipts and compare them with frozen maturity-v1 controls."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_unseen30_maturity import (
    _cluster_bootstrap_ci,
    _duration,
    _evaluator_status,
    _evaluator_summary,
    _group_summaries,
    _mcnemar_exact,
    _numeric,
    _resource_summary,
    _sha256,
    _status_summary,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE = ROOT / "runs/experiments/gamedevbench-unseen30-open-scope-v2-run1"
DEFAULT_BASELINE = ROOT / "runs/experiments/gamedevbench-unseen30-maturity-v1"
DEFAULT_MANIFEST = ROOT / "benchmarks/gamedevbench-unseen30-maturity-v1.json"
CANDIDATE = "programmable-open-scope-v2"
CONDITIONS = ("official-default", "legacy-tool-v1", "programmable-v1", CANDIDATE)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _receipts(experiment: Path) -> list[dict[str, Any]]:
    return [_load(path) for path in sorted((experiment / "receipts").glob("*.json"))]


def _validate_candidate(
    candidate: Path,
    baseline: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    schedule = _load(candidate / "schedule.json")
    protocol = _load(candidate / "protocol-freeze.json")
    errors: dict[str, Any] = {}
    errors["receipt_count"] = (
        None if len(rows) == schedule["attempt_count"] == 90 else len(rows)
    )
    expected_ordinals = set(range(1, 91))
    errors["missing_ordinals"] = sorted(
        expected_ordinals - {int(row["ordinal"]) for row in rows}
    )
    errors["duplicate_ordinals"] = sorted(
        ordinal
        for ordinal, count in Counter(int(row["ordinal"]) for row in rows).items()
        if count > 1
    )
    errors["nonzero_runner_ordinals"] = [
        row["ordinal"] for row in rows if row.get("runner_exit_code") != 0
    ]
    errors["result_hash_mismatches"] = [
        row["ordinal"]
        for row in rows
        if not Path(row["result_source"]).is_file()
        or _sha256(Path(row["result_source"])) != row["result_source_sha256"]
    ]
    errors["schedule_mismatches"] = [
        item["ordinal"]
        for item, row in zip(schedule["attempts"], rows, strict=False)
        if any(
            item[key] != row.get(key)
            for key in ("ordinal", "attempt_id", "task_id", "repetition", "condition")
        )
    ]
    errors["candidate_condition_mismatches"] = [
        row["ordinal"] for row in rows if row.get("condition") != CANDIDATE
    ]
    errors["baseline_analysis_hash"] = (
        None
        if _sha256(baseline / "analysis.json")
        == protocol["frozen_baseline_analysis_sha256"]
        else "mismatch"
    )
    errors["baseline_schedule_hash"] = (
        None
        if _sha256(baseline / "schedule.json") == protocol["source_schedule_sha256"]
        else "mismatch"
    )
    errors["complete_marker"] = None if (candidate / "complete.json").is_file() else "missing"
    populated = {key: value for key, value in errors.items() if value not in (None, [], {})}
    return {
        "valid": not populated,
        "expected_attempts": 90,
        "observed_receipts": len(rows),
        "errors": populated,
    }


def _task_metrics(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["task_id"], row["condition"])].append(row)
    tasks = []
    for manifest_task in manifest["tasks"]:
        task_id = manifest_task["task_id"]
        item: dict[str, Any] = {
            "task_id": task_id,
            "task_name": manifest_task["name"],
            "stratum": manifest_task["stratum"],
            "conditions": {},
        }
        for condition in CONDITIONS:
            cell = sorted(grouped[(task_id, condition)], key=lambda row: row["repetition"])
            evaluator = [_evaluator_status(row) == "PASS" for row in cell]
            terminal = [row["normalized_status"] == "PASS" for row in cell]
            item["conditions"][condition] = {
                "attempts": len(cell),
                "evaluator_passes": sum(evaluator),
                "evaluator_pass_fraction": sum(evaluator) / len(cell),
                "majority_evaluator_pass": sum(evaluator) >= 2,
                "terminal_passes": sum(terminal),
                "terminal_pass_fraction": sum(terminal) / len(cell),
                "attempts_detail": [
                    {
                        "repetition": row["repetition"],
                        "terminal_status": row["normalized_status"],
                        "evaluator_status": _evaluator_status(row),
                        "failure_category": row["record"].get("failure_category"),
                        "failure_detail": row["record"].get("failure_detail"),
                        "duration_seconds": _duration(row["record"]),
                    }
                    for row in cell
                ],
            }
        tasks.append(item)
    by_condition = {}
    for condition in CONDITIONS:
        cells = [task["conditions"][condition] for task in tasks]
        by_condition[condition] = {
            "tasks": len(cells),
            "mean_task_evaluator_pass_fraction": statistics.fmean(
                cell["evaluator_pass_fraction"] for cell in cells
            ),
            "majority_evaluator_pass_tasks": sum(
                cell["majority_evaluator_pass"] for cell in cells
            ),
            "never_evaluator_pass_tasks": sum(cell["evaluator_passes"] == 0 for cell in cells),
            "unanimous_evaluator_pass_tasks": sum(cell["evaluator_passes"] == 3 for cell in cells),
        }
    return {"by_condition": by_condition, "tasks": tasks}


def _pairwise(task_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for comparator in ("official-default", "programmable-v1", "legacy-tool-v1"):
        first = [row["conditions"][CANDIDATE] for row in task_rows]
        second = [row["conditions"][comparator] for row in task_rows]
        deltas = [
            a["evaluator_pass_fraction"] - b["evaluator_pass_fraction"]
            for a, b in zip(first, second, strict=True)
        ]
        output.append(
            {
                "first": CANDIDATE,
                "second": comparator,
                "pass_fraction_delta": _cluster_bootstrap_ci(deltas),
                "majority_pass_mcnemar": _mcnemar_exact(
                    [cell["majority_evaluator_pass"] for cell in first],
                    [cell["majority_evaluator_pass"] for cell in second],
                ),
                "tasks_improved": [
                    row["task_id"]
                    for row, delta in zip(task_rows, deltas, strict=True)
                    if delta > 0
                ],
                "tasks_regressed": [
                    row["task_id"]
                    for row, delta in zip(task_rows, deltas, strict=True)
                    if delta < 0
                ],
                "task_deltas": [
                    {"task_id": row["task_id"], "delta": delta}
                    for row, delta in zip(task_rows, deltas, strict=True)
                ],
            }
        )
    return output


def _attempt_transitions(
    baseline_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    candidate = {
        (row["task_id"], row["repetition"]): _evaluator_status(row) == "PASS"
        for row in candidate_rows
    }
    output = {}
    for comparator in ("official-default", "programmable-v1", "legacy-tool-v1"):
        comparison = {
            (row["task_id"], row["repetition"]): _evaluator_status(row) == "PASS"
            for row in baseline_rows
            if row["condition"] == comparator
        }
        counts = Counter()
        details = []
        for key, candidate_pass in candidate.items():
            comparator_pass = comparison[key]
            transition = (
                "both_pass"
                if candidate_pass and comparator_pass
                else "candidate_only"
                if candidate_pass
                else "comparator_only"
                if comparator_pass
                else "both_fail"
            )
            counts[transition] += 1
            if transition in {"candidate_only", "comparator_only"}:
                details.append(
                    {"task_id": key[0], "repetition": key[1], "transition": transition}
                )
        output[comparator] = {"counts": dict(counts), "discordant_attempts": details}
    return output


def _candidate_controls(rows: list[dict[str, Any]]) -> dict[str, Any]:
    manifests = []
    missing = []
    for row in rows:
        path = Path(row["record"]["run_directory"]) / "output-manifest.json"
        if not path.is_file():
            missing.append(row["ordinal"])
            continue
        manifests.append((row, _load(path)))
    declared = [(row, item) for row, item in manifests if item["declarations"]]
    added_paths = Counter(
        path
        for _, manifest in declared
        for declaration in manifest["declarations"]
        for path in declaration["added_paths"]
    )
    return {
        "lineage_complete": dict(
            Counter(str(row["record"].get("lineage_complete")) for row in rows)
        ),
        "preservation_gate": dict(
            Counter(str(row["record"].get("preservation_gate", "MISSING")) for row in rows)
        ),
        "official_gate": dict(
            Counter(str(row["record"].get("official_gate", "MISSING")) for row in rows)
        ),
        "policy_violations": int(
            sum(_numeric(row["record"], "policy_violations") for row in rows)
        ),
        "mean_required_evidence_coverage": statistics.fmean(
            _numeric(row["record"], "required_evidence_coverage") for row in rows
        ),
        "failure_categories": dict(
            sorted(
                Counter(str(row["record"].get("failure_category", "none")) for row in rows).items()
            )
        ),
        "output_manifests": {
            "observed": len(manifests),
            "missing_ordinals": missing,
            "attempts_with_declarations": len(declared),
            "declaration_count": sum(len(item["declarations"]) for _, item in declared),
            "added_path_counts": [
                {"path": path, "count": count} for path, count in added_paths.most_common()
            ],
        },
    }


def build(candidate: Path, baseline: Path, manifest_path: Path) -> dict[str, Any]:
    candidate_rows = _receipts(candidate)
    baseline_rows = _receipts(baseline)
    validation = _validate_candidate(candidate, baseline, candidate_rows)
    if not validation["valid"]:
        raise RuntimeError(json.dumps(validation, indent=2, sort_keys=True))
    rows = [*baseline_rows, *candidate_rows]
    manifest = _load(manifest_path)
    task_metrics = _task_metrics(rows, manifest)
    overall = {}
    for condition in CONDITIONS:
        condition_rows = [row for row in rows if row["condition"] == condition]
        overall[condition] = {
            "primary_evaluator": _evaluator_summary(condition_rows),
            "terminal": _status_summary(condition_rows),
            "primary_by_repetition": _group_summaries(
                condition_rows, "repetition", evaluator=True
            ),
            "primary_by_stratum": _group_summaries(
                condition_rows, "stratum", evaluator=True
            ),
            "resources": _resource_summary(condition_rows),
        }
    quota = [float(row["weekly_used_percent_before"]) for row in candidate_rows]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "inference_unit": "task; three repetitions retained within each task",
        "candidate_experiment": str(candidate),
        "frozen_baseline_experiment": str(baseline),
        "inputs": {
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "candidate_schedule_sha256": _sha256(candidate / "schedule.json"),
            "candidate_protocol_sha256": _sha256(candidate / "protocol-freeze.json"),
            "frozen_baseline_analysis_sha256": _sha256(baseline / "analysis.json"),
        },
        "validation": validation,
        "quota": {
            "weekly_used_percent_before_first": quota[0],
            "weekly_used_percent_before_last": quota[-1],
            "minimum_remaining_percent_observed": 100 - max(quota),
        },
        "overall": overall,
        "task_metrics": task_metrics,
        "pairwise": _pairwise(task_metrics["tasks"]),
        "attempt_transitions": _attempt_transitions(baseline_rows, candidate_rows),
        "candidate_controls": _candidate_controls(candidate_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    output = arguments.output or arguments.candidate / "analysis.json"
    payload = build(
        arguments.candidate.resolve(),
        arguments.baseline.resolve(),
        arguments.manifest.resolve(),
    )
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
