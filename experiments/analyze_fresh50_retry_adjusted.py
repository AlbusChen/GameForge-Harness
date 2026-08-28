#!/usr/bin/env python3
"""Analyze Fresh-50 after one prespecified retry of zero-turn CLI failures."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_fresh50_candidate_iteration import (
    _bootstrap,
    _evaluator_status,
    _mcnemar,
    _summary,
)
from experiments.analyze_fresh50_official_v4 import _resource

ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-experiment", type=Path, required=True)
    parser.add_argument(
        "--baseline-experiment",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-fresh50-official-v4-v1",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/gamedevbench-fresh50-v1.json",
    )
    parser.add_argument(
        "--v11-analysis",
        type=Path,
        default=(
            ROOT
            / "runs/experiments/gamedevbench-fresh50-programmable-open-adaptive-v11-run1"
            / "analysis.json"
        ),
    )
    return parser.parse_args()


def _read_receipts(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((directory / "receipts").glob("*.json"))
    ]


def main() -> int:
    arguments = _arguments()
    primary = arguments.primary_experiment.resolve(strict=True)
    supplemental = (primary / "supplemental-cli-retry1").resolve(strict=True)
    baseline = arguments.baseline_experiment.resolve(strict=True)
    manifest = json.loads(arguments.manifest.resolve(strict=True).read_text(encoding="utf-8"))
    v11 = json.loads(arguments.v11_analysis.resolve(strict=True).read_text(encoding="utf-8"))
    primary_rows = _read_receipts(primary)
    retry_rows = _read_receipts(supplemental)
    baseline_rows = _read_receipts(baseline)
    expected_ids = [str(task["task_id"]) for task in manifest["tasks"]]
    official_by_id = {
        str(row["task_id"]): row
        for row in baseline_rows
        if row["condition"] == "official-default"
    }
    primary_by_id = {str(row["task_id"]): row for row in primary_rows}
    retry_by_id = {str(row["task_id"]): row for row in retry_rows}
    errors: list[str] = []
    if len(primary_rows) != 50 or set(primary_by_id) != set(expected_ids):
        errors.append("primary receipts do not cover the frozen 50 tasks")
    if len(official_by_id) != 50:
        errors.append("Official baseline does not cover the frozen 50 tasks")
    retry_eligible = {
        task_id
        for task_id, row in primary_by_id.items()
        if row["normalized_status"] == "BLOCKED"
        and row["record"].get("failure_category") == "ModelError"
        and row["record"].get("failure_detail") == "agent CLI returned exit code 1"
        and row["record"].get("model_turns") == 0
    }
    if set(retry_by_id) != retry_eligible:
        errors.append("supplemental receipts do not exactly match eligible CLI failures")

    effective: list[dict[str, Any]] = []
    for task_id in expected_ids:
        row = retry_by_id.get(task_id, primary_by_id[task_id])
        effective.append({**row, "condition": "retry-adjusted-v16"})
    official = [official_by_id[task_id] for task_id in expected_ids]
    candidate_pass = [_evaluator_status(row) == "PASS" for row in effective]
    official_pass = [_evaluator_status(row) == "PASS" for row in official]
    deltas = [
        float(candidate) - float(reference)
        for candidate, reference in zip(candidate_pass, official_pass, strict=True)
    ]
    v11_by_id = {
        str(row["task_id"]): row["candidate"] == "PASS" for row in v11["tasks"]
    }
    v11_pass = [v11_by_id[task_id] for task_id in expected_ids]
    strict_pass = [
        _evaluator_status(primary_by_id[task_id]) == "PASS" for task_id in expected_ids
    ]
    v11_deltas = [
        float(candidate) - float(reference)
        for candidate, reference in zip(candidate_pass, v11_pass, strict=True)
    ]
    strata = sorted({str(task["stratum"]) for task in manifest["tasks"]})
    primary_statuses = Counter(str(row["normalized_status"]) for row in primary_rows)
    retry_statuses = Counter(str(row["normalized_status"]) for row in retry_rows)
    operational_statuses = Counter(str(row["normalized_status"]) for row in effective)
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "retry_policy": {
            "eligible": len(retry_eligible),
            "attempted": len(retry_rows),
            "statuses": dict(sorted(retry_statuses.items())),
            "task_ids": sorted(retry_eligible),
            "remaining_blocked": sorted(
                str(row["task_id"])
                for row in effective
                if row["normalized_status"] == "BLOCKED"
            ),
        },
        "strict_first_attempt": {
            "statuses": dict(sorted(primary_statuses.items())),
            "pass_rate": primary_statuses["PASS"] / 50,
        },
        "retry_adjusted": {
            "summary": _summary(effective, strata),
            "statuses": dict(sorted(operational_statuses.items())),
        },
        "official": _summary(official, strata),
        "paired_vs_official": {
            "difference": _bootstrap(deltas, seed_offset=31),
            "mcnemar": _mcnemar(candidate_pass, official_pass),
            "candidate_only_pass": [
                task_id
                for task_id, candidate, reference in zip(
                    expected_ids, candidate_pass, official_pass, strict=True
                )
                if candidate and not reference
            ],
            "official_only_pass": [
                task_id
                for task_id, candidate, reference in zip(
                    expected_ids, candidate_pass, official_pass, strict=True
                )
                if reference and not candidate
            ],
        },
        "paired_vs_v11": {
            "v11_pass_rate": sum(v11_pass) / len(v11_pass),
            "difference": _bootstrap(v11_deltas, seed_offset=41),
            "mcnemar": _mcnemar(candidate_pass, v11_pass),
            "candidate_only_pass": [
                task_id
                for task_id, candidate, reference in zip(
                    expected_ids, candidate_pass, v11_pass, strict=True
                )
                if candidate and not reference
            ],
            "v11_only_pass": [
                task_id
                for task_id, candidate, reference in zip(
                    expected_ids, candidate_pass, v11_pass, strict=True
                )
                if reference and not candidate
            ],
        },
        "strict_first_attempt_paired_vs_v11": {
            "difference": _bootstrap(
                [
                    float(candidate) - float(reference)
                    for candidate, reference in zip(strict_pass, v11_pass, strict=True)
                ],
                seed_offset=51,
            ),
            "mcnemar": _mcnemar(strict_pass, v11_pass),
        },
        "resources": {
            "effective_task_attempts": _resource(effective),
            "retry_overhead": _resource(retry_rows),
            "all_executed_attempts": _resource([*primary_rows, *retry_rows]),
        },
        "tasks": [
            {
                "task_id": task_id,
                "stratum": manifest["tasks"][index]["stratum"],
                "official": _evaluator_status(official[index]),
                "first_attempt": _evaluator_status(primary_by_id[task_id]),
                "retry": (
                    _evaluator_status(retry_by_id[task_id])
                    if task_id in retry_by_id
                    else None
                ),
                "retry_adjusted": _evaluator_status(effective[index]),
            }
            for index, task_id in enumerate(expected_ids)
        ],
    }
    output = primary / "retry-adjusted-analysis.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["validation"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
