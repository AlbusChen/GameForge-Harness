#!/usr/bin/env python3
"""Validate and summarize the frozen 30-task Harness maturity holdout.

The unit of inference is the task. Each task contributes one pass fraction per
condition (three repetitions), so uncertainty estimates preserve within-task
clustering instead of treating all 270 attempts as independent observations.
"""

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
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/gamedevbench-unseen30-maturity-v1"
DEFAULT_MANIFEST = ROOT / "benchmarks/gamedevbench-unseen30-maturity-v1.json"
CONDITIONS = ("official-default", "legacy-tool-v1", "programmable-v1")
EXPECTED_STATUSES = {"PASS", "FAIL", "BLOCKED", "ERROR", "INFRASTRUCTURE_ERROR"}
BOOTSTRAP_SEED = 20260810
BOOTSTRAP_SAMPLES = 100_000


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _duration(record: dict[str, Any]) -> float | None:
    value = record.get("duration_seconds", record.get("solver_duration"))
    return float(value) if isinstance(value, int | float) else None


def _numeric(record: dict[str, Any], key: str) -> float:
    value = record.get(key, 0)
    return float(value) if isinstance(value, int | float) else 0.0


def _status_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["normalized_status"] for row in rows)
    total = len(rows)
    return {
        "attempts": total,
        "status_counts": dict(sorted(counts.items())),
        "terminal_pass_rate": _rate(counts["PASS"], total),
        "completion_rate": _rate(
            counts["PASS"] + counts["FAIL"] + counts["BLOCKED"], total
        ),
    }


def _evaluator_status(row: dict[str, Any]) -> str:
    if row["condition"] == "official-default":
        return "PASS" if row["normalized_status"] == "PASS" else "FAIL"
    status = str(row["record"].get("official_gate", "NOT_RUN"))
    return status if status in {"PASS", "FAIL"} else "NOT_RUN"


def _evaluator_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(_evaluator_status(row) for row in rows)
    total = len(rows)
    return {
        "attempts": total,
        "status_counts": dict(sorted(counts.items())),
        "pass_rate": _rate(counts["PASS"], total),
    }


def _resource_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [value for row in rows if (value := _duration(row["record"])) is not None]
    return {
        "duration_seconds": {
            "observed": len(durations),
            "total": sum(durations),
            "mean": statistics.fmean(durations) if durations else None,
            "median": statistics.median(durations) if durations else None,
            "p95": _percentile(durations, 0.95),
            "max": max(durations) if durations else None,
        },
        "tokens": {
            key: int(sum(_numeric(row["record"], key) for row in rows))
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
        },
        "reported_cost_usd": sum(_numeric(row["record"], "cost_usd") for row in rows),
        "tool_calls": int(sum(_numeric(row["record"], "tool_calls") for row in rows)),
        "model_turns": int(sum(_numeric(row["record"], "model_turns") for row in rows)),
    }


def _group_summaries(
    rows: list[dict[str, Any]], key: str, *, evaluator: bool = False
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[key])].append(row)
    summarize = _evaluator_summary if evaluator else _status_summary
    return {group: summarize(group_rows) for group, group_rows in sorted(groups.items())}


def _task_metrics(rows: list[dict[str, Any]], task_ids: list[str]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["task_id"], row["condition"])].append(row)

    task_rows: list[dict[str, Any]] = []
    for task_id in task_ids:
        sample = next(row for row in rows if row["task_id"] == task_id)
        item: dict[str, Any] = {
            "task_id": task_id,
            "task_name": sample["task_name"],
            "stratum": sample["stratum"],
            "conditions": {},
        }
        for condition in CONDITIONS:
            cell = sorted(
                grouped[(task_id, condition)], key=lambda row: row["repetition"]
            )
            evaluator_passes = sum(_evaluator_status(row) == "PASS" for row in cell)
            terminal_passes = sum(row["normalized_status"] == "PASS" for row in cell)
            item["conditions"][condition] = {
                "evaluator_passes": evaluator_passes,
                "attempts": len(cell),
                "evaluator_pass_fraction": _rate(evaluator_passes, len(cell)),
                "majority_evaluator_pass": evaluator_passes >= 2,
                "unanimous_evaluator_pass": evaluator_passes == 3,
                "terminal_passes": terminal_passes,
                "terminal_pass_fraction": _rate(terminal_passes, len(cell)),
                "majority_terminal_pass": terminal_passes >= 2,
                "attempts_detail": [
                    {
                        "repetition": row["repetition"],
                        "status": row["normalized_status"],
                        "detail": row["record"].get(
                            "failure_detail", row["record"].get("message")
                        ),
                        "duration_seconds": _duration(row["record"]),
                    }
                    for row in cell
                ],
            }
        task_rows.append(item)

    by_condition: dict[str, Any] = {}
    for condition in CONDITIONS:
        cells = [item["conditions"][condition] for item in task_rows]
        fractions = [cell["evaluator_pass_fraction"] for cell in cells]
        terminal_fractions = [cell["terminal_pass_fraction"] for cell in cells]
        by_condition[condition] = {
            "tasks": len(cells),
            "mean_task_evaluator_pass_fraction": statistics.fmean(fractions),
            "majority_evaluator_pass_tasks": sum(
                cell["majority_evaluator_pass"] for cell in cells
            ),
            "majority_evaluator_pass_rate": _rate(
                sum(cell["majority_evaluator_pass"] for cell in cells), len(cells)
            ),
            "unanimous_evaluator_pass_tasks": sum(
                cell["unanimous_evaluator_pass"] for cell in cells
            ),
            "never_evaluator_pass_tasks": sum(cell["evaluator_passes"] == 0 for cell in cells),
            "mean_task_terminal_pass_fraction": statistics.fmean(terminal_fractions),
            "majority_terminal_pass_tasks": sum(
                cell["majority_terminal_pass"] for cell in cells
            ),
        }

    by_stratum: dict[str, Any] = {}
    for stratum in sorted({item["stratum"] for item in task_rows}):
        stratum_rows = [item for item in task_rows if item["stratum"] == stratum]
        by_stratum[stratum] = {"tasks": len(stratum_rows), "conditions": {}}
        for condition in CONDITIONS:
            cells = [item["conditions"][condition] for item in stratum_rows]
            majority_passes = sum(cell["majority_evaluator_pass"] for cell in cells)
            by_stratum[stratum]["conditions"][condition] = {
                "mean_task_evaluator_pass_fraction": statistics.fmean(
                    cell["evaluator_pass_fraction"] for cell in cells
                ),
                "majority_evaluator_pass_tasks": majority_passes,
                "majority_evaluator_pass_rate": _rate(majority_passes, len(cells)),
            }
    return {"by_condition": by_condition, "by_stratum": by_stratum, "tasks": task_rows}


def _cluster_bootstrap_ci(task_deltas: list[float]) -> dict[str, Any]:
    rng = random.Random(BOOTSTRAP_SEED)
    count = len(task_deltas)
    samples = [
        sum(task_deltas[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(BOOTSTRAP_SAMPLES)
    ]
    return {
        "method": "task-clustered nonparametric bootstrap percentile CI",
        "seed": BOOTSTRAP_SEED,
        "samples": BOOTSTRAP_SAMPLES,
        "estimate": statistics.fmean(task_deltas),
        "ci95": [_percentile(samples, 0.025), _percentile(samples, 0.975)],
    }


def _mcnemar_exact(first: list[bool], second: list[bool]) -> dict[str, Any]:
    first_only = sum(a and not b for a, b in zip(first, second, strict=True))
    second_only = sum(b and not a for a, b in zip(first, second, strict=True))
    discordant = first_only + second_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, value) for value in range(min(first_only, second_only) + 1)
        ) / (2**discordant)
        p_value = min(1.0, 2 * tail)
    return {
        "first_only": first_only,
        "second_only": second_only,
        "discordant": discordant,
        "two_sided_exact_p": p_value,
    }


def _pairwise(task_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs = (
        ("programmable-v1", "official-default"),
        ("legacy-tool-v1", "official-default"),
        ("programmable-v1", "legacy-tool-v1"),
    )
    output = []
    for first, second in pairs:
        first_cells = [row["conditions"][first] for row in task_rows]
        second_cells = [row["conditions"][second] for row in task_rows]
        deltas = [
            first_cell["evaluator_pass_fraction"]
            - second_cell["evaluator_pass_fraction"]
            for first_cell, second_cell in zip(first_cells, second_cells, strict=True)
        ]
        output.append(
            {
                "first": first,
                "second": second,
                "pass_fraction_delta": _cluster_bootstrap_ci(deltas),
                "majority_pass_mcnemar": _mcnemar_exact(
                    [cell["majority_evaluator_pass"] for cell in first_cells],
                    [cell["majority_evaluator_pass"] for cell in second_cells],
                ),
                "task_deltas": [
                    {"task_id": row["task_id"], "delta": delta}
                    for row, delta in zip(task_rows, deltas, strict=True)
                ],
            }
        )
    return output


def _harness_controls(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for condition in ("legacy-tool-v1", "programmable-v1"):
        condition_rows = [row for row in rows if row["condition"] == condition]
        output[condition] = {
            "lineage_complete": Counter(
                str(row["record"].get("lineage_complete")) for row in condition_rows
            ),
            "preservation_gate": Counter(
                str(row["record"].get("preservation_gate", "MISSING")) for row in condition_rows
            ),
            "official_gate": Counter(
                str(row["record"].get("official_gate", "MISSING")) for row in condition_rows
            ),
            "policy_violations": int(
                sum(_numeric(row["record"], "policy_violations") for row in condition_rows)
            ),
            "mean_required_evidence_coverage": statistics.fmean(
                _numeric(row["record"], "required_evidence_coverage") for row in condition_rows
            ),
            "failure_categories": dict(
                sorted(
                    Counter(
                        str(row["record"].get("failure_category", "none"))
                        for row in condition_rows
                    ).items()
                )
            ),
        }
        for key in ("lineage_complete", "preservation_gate", "official_gate"):
            output[condition][key] = dict(output[condition][key])
    return output


def _programmable_safety_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    programmable = [row for row in rows if row["condition"] == "programmable-v1"]
    preservation_failures = [
        row for row in programmable if row["record"].get("preservation_gate") == "FAIL"
    ]
    evidence_rows: list[dict[str, Any]] = []
    missing_evidence: list[int] = []
    for row in preservation_failures:
        evidence_path = Path(row["record"]["run_directory"]) / "resource-safety.json"
        if not evidence_path.exists():
            missing_evidence.append(row["ordinal"])
            continue
        evidence_rows.append(_load_json(evidence_path))

    changed_paths = Counter(
        path for evidence in evidence_rows for path in evidence["changed_protected_paths"]
    )
    broken_references = [
        reference for evidence in evidence_rows for reference in evidence["new_broken_references"]
    ]
    missing_identities = [
        identity
        for evidence in evidence_rows
        for identity in evidence["missing_protected_identities"]
    ]
    non_sidecar_paths = sorted(
        path for path in changed_paths if not path.endswith((".import", ".uid"))
    )
    shape_counts = Counter(
        (
            tuple(evidence["changed_protected_paths"]),
            len(evidence["missing_protected_identities"]),
            len(evidence["new_broken_references"]),
        )
        for evidence in evidence_rows
    )
    policy_rows = [
        row for row in programmable if _numeric(row["record"], "policy_violations") > 0
    ]
    return {
        "preservation_failures": len(preservation_failures),
        "resource_safety_evidence_rows": len(evidence_rows),
        "missing_resource_safety_evidence_ordinals": missing_evidence,
        "evaluator_pass_downgraded_by_terminal_gates": sum(
            _evaluator_status(row) == "PASS" and row["normalized_status"] != "PASS"
            for row in programmable
        ),
        "changed_protected_path_counts": [
            {"path": path, "count": count}
            for path, count in changed_paths.most_common()
        ],
        "failure_shapes": [
            {
                "changed_protected_paths": list(shape[0]),
                "missing_identity_count": shape[1],
                "broken_reference_count": shape[2],
                "attempts": count,
            }
            for shape, count in shape_counts.most_common()
        ],
        "all_changed_protected_paths_are_engine_sidecars": not non_sidecar_paths,
        "non_sidecar_changed_protected_paths": non_sidecar_paths,
        "missing_protected_identity_count": len(missing_identities),
        "new_broken_reference_count": len(broken_references),
        "policy_guard": {
            "violation_events": int(
                sum(_numeric(row["record"], "policy_violations") for row in programmable)
            ),
            "affected_attempts": len(policy_rows),
            "affected_tasks": sorted({row["task_id"] for row in policy_rows}),
        },
    }


def _validate(
    rows: list[dict[str, Any]], schedule: dict[str, Any], task_ids: list[str], experiment: Path
) -> dict[str, Any]:
    scheduled = schedule["attempts"]
    receipt_by_ordinal = {row["ordinal"]: row for row in rows}
    duplicate_ordinals = [
        ordinal for ordinal, count in Counter(row["ordinal"] for row in rows).items() if count > 1
    ]
    duplicate_attempt_ids = [
        attempt_id
        for attempt_id, count in Counter(row["attempt_id"] for row in rows).items()
        if count > 1
    ]
    missing_ordinals = [
        item["ordinal"] for item in scheduled if item["ordinal"] not in receipt_by_ordinal
    ]
    schedule_mismatches = []
    for item in scheduled:
        receipt = receipt_by_ordinal.get(item["ordinal"])
        if receipt is None:
            continue
        for key in (
            "attempt_id",
            "condition",
            "task_id",
            "repetition",
            "within_task_position",
        ):
            if receipt.get(key) != item.get(key):
                schedule_mismatches.append(
                    {
                        "ordinal": item["ordinal"],
                        "field": key,
                        "scheduled": item.get(key),
                        "receipt": receipt.get(key),
                    }
                )
    source_hash_mismatches = []
    missing_sources = []
    for row in rows:
        source = Path(row["result_source"])
        if not source.exists():
            missing_sources.append(row["ordinal"])
        elif _sha256(source) != row["result_source_sha256"]:
            source_hash_mismatches.append(row["ordinal"])
    cell_counts = Counter((row["task_id"], row["condition"]) for row in rows)
    invalid_cell_counts = [
        {"task_id": task_id, "condition": condition, "count": cell_counts[(task_id, condition)]}
        for task_id in task_ids
        for condition in CONDITIONS
        if cell_counts[(task_id, condition)] != 3
    ]
    unexpected_statuses = sorted(
        {row["normalized_status"] for row in rows} - EXPECTED_STATUSES
    )
    runner_nonzero = [row["ordinal"] for row in rows if row.get("runner_exit_code") != 0]
    rate_limited = [
        row["ordinal"] for row in rows if row["record"].get("is_rate_limited") is True
    ]
    complete_marker = experiment / "complete.json"
    errors = {
        "duplicate_ordinals": duplicate_ordinals,
        "duplicate_attempt_ids": duplicate_attempt_ids,
        "missing_ordinals": missing_ordinals,
        "schedule_mismatches": schedule_mismatches,
        "missing_result_sources": missing_sources,
        "result_source_hash_mismatches": source_hash_mismatches,
        "invalid_task_condition_counts": invalid_cell_counts,
        "unexpected_statuses": unexpected_statuses,
        "runner_nonzero_ordinals": runner_nonzero,
        "rate_limited_ordinals": rate_limited,
        "complete_marker_missing": not complete_marker.exists(),
    }
    valid = all(not value for value in errors.values())
    return {
        "valid": valid,
        "expected_attempts": schedule["attempt_count"],
        "observed_receipts": len(rows),
        "errors": errors,
    }


def build_analysis(experiment: Path, manifest_path: Path) -> dict[str, Any]:
    schedule_path = experiment / "schedule.json"
    schedule = _load_json(schedule_path)
    manifest = _load_json(manifest_path)
    receipt_paths = sorted((experiment / "receipts").glob("*.json"))
    rows = [_load_json(path) for path in receipt_paths]
    task_ids = [task["task_id"] for task in manifest["tasks"]]
    validation = _validate(rows, schedule, task_ids, experiment)
    if not validation["valid"]:
        raise RuntimeError(json.dumps(validation, indent=2, sort_keys=True))

    task_metrics = _task_metrics(rows, task_ids)
    overall: dict[str, Any] = {}
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
            "primary_by_within_task_position": _group_summaries(
                condition_rows, "within_task_position", evaluator=True
            ),
            "resources": _resource_summary(condition_rows),
        }

    quota_used = [float(row["weekly_used_percent_before"]) for row in rows]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "inference_unit": "task",
        "experiment": str(experiment),
        "inputs": {
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "schedule": str(schedule_path),
            "schedule_sha256": _sha256(schedule_path),
            "protocol_freeze": str(experiment / "protocol-freeze.json"),
            "protocol_freeze_sha256": _sha256(experiment / "protocol-freeze.json"),
        },
        "validation": validation,
        "quota": {
            "weekly_used_percent_before_first": quota_used[0],
            "weekly_used_percent_before_last": quota_used[-1],
            "maximum_weekly_used_percent_observed": max(quota_used),
            "minimum_remaining_percent_observed": 100 - max(quota_used),
        },
        "overall": overall,
        "task_metrics": task_metrics,
        "pairwise": _pairwise(task_metrics["tasks"]),
        "harness_controls": _harness_controls(rows),
        "programmable_safety_diagnostics": _programmable_safety_diagnostics(rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    output = args.output or args.experiment / "analysis.json"
    analysis = build_analysis(args.experiment.resolve(), args.manifest.resolve())
    output.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
