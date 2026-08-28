#!/usr/bin/env python3
"""Compare two complete qualified GameDevBench Harness conditions task by task."""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_gamedevbench_full import _percentile, _rate, _wilson
from experiments.analyze_gamedevbench_full_paired import _exact_mcnemar, _sha256

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TASKS = 332


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-provenance-v19-run1",
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-native-agent-v20-run1",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/gamedevbench-full-qualified332-v20.json",
    )
    parser.add_argument("--output-name", default="paired-v19-analysis.json")
    return parser.parse_args()


def _receipts(experiment: Path) -> dict[str, dict[str, Any]]:
    return {
        str(row["task_id"]): row
        for path in sorted((experiment / "receipts").glob("*.json"))
        if isinstance(row := json.loads(path.read_text(encoding="utf-8")), dict)
    }


def _comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_passes = sum(row["baseline_pass"] for row in rows)
    candidate_passes = sum(row["candidate_pass"] for row in rows)
    candidate_only = sum(row["candidate_pass"] and not row["baseline_pass"] for row in rows)
    baseline_only = sum(row["baseline_pass"] and not row["candidate_pass"] for row in rows)
    both = sum(row["candidate_pass"] and row["baseline_pass"] for row in rows)
    return {
        "attempts": len(rows),
        "baseline": {
            "passes": baseline_passes,
            "pass_rate": _rate(baseline_passes, len(rows)),
            "wilson_ci95": _wilson(baseline_passes, len(rows)),
        },
        "candidate": {
            "passes": candidate_passes,
            "pass_rate": _rate(candidate_passes, len(rows)),
            "wilson_ci95": _wilson(candidate_passes, len(rows)),
        },
        "candidate_minus_baseline": _rate(candidate_passes - baseline_passes, len(rows)),
        "paired_outcomes": {
            "both_pass": both,
            "candidate_only_pass": candidate_only,
            "baseline_only_pass": baseline_only,
            "neither_pass": len(rows) - both - candidate_only - baseline_only,
        },
        "mcnemar_exact_two_sided_p": _exact_mcnemar(candidate_only, baseline_only),
    }


def _family_bootstrap(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["family_key"])].append(row)
    families = list(grouped.values())
    rng = random.Random(20260814)
    differences: list[float] = []
    for _ in range(20_000):
        sample = [rng.choice(families) for _ in families]
        flat = [row for family in sample for row in family]
        differences.append(
            sum(int(row["candidate_pass"]) - int(row["baseline_pass"]) for row in flat) / len(flat)
        )
    return {
        "family_count": len(families),
        "candidate_minus_baseline_ci95": [
            _percentile(differences, 0.025),
            _percentile(differences, 0.975),
        ],
        "replicates": len(differences),
        "seed": 20260814,
    }


def _resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for condition in ("baseline", "candidate"):
        durations = sorted(float(row[f"{condition}_duration_seconds"]) for row in rows)
        tokens = [int(row[f"{condition}_tokens"]) for row in rows]
        result[condition] = {
            "total_tokens": sum(tokens),
            "mean_tokens_per_task": sum(tokens) / len(tokens),
            "total_seconds": sum(durations),
            "median_seconds": _percentile(durations, 0.5),
            "p95_seconds": _percentile(durations, 0.95),
        }
    return result


def main() -> int:
    arguments = _arguments()
    baseline = arguments.baseline.resolve(strict=True)
    candidate = arguments.candidate.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_by_task = {str(task["task_id"]): task for task in manifest["tasks"]}
    baseline_rows = _receipts(baseline)
    candidate_rows = _receipts(candidate)
    errors: list[str] = []
    for name, experiment, receipts in (
        ("baseline", baseline, baseline_rows),
        ("candidate", candidate, candidate_rows),
    ):
        complete = json.loads((experiment / "complete.json").read_text(encoding="utf-8"))
        if len(receipts) != EXPECTED_TASKS or complete.get("attempts_completed") != EXPECTED_TASKS:
            errors.append(f"{name} is not a complete 332-task run")
    expected = set(manifest_by_task)
    if set(baseline_rows) != expected or set(candidate_rows) != expected:
        errors.append("task identities differ across conditions or manifest")

    rows: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        task_id = str(task["task_id"])
        baseline_row = baseline_rows[task_id]
        candidate_row = candidate_rows[task_id]
        baseline_record = baseline_row["record"]
        candidate_record = candidate_row["record"]
        rows.append(
            {
                "task_id": task_id,
                "stratum": task["stratum"],
                "family_key": task["family_key"],
                "baseline_status": baseline_row["normalized_status"],
                "candidate_status": candidate_row["normalized_status"],
                "baseline_pass": baseline_row["normalized_status"] == "PASS",
                "candidate_pass": candidate_row["normalized_status"] == "PASS",
                "baseline_duration_seconds": baseline_record.get("duration_seconds", 0),
                "candidate_duration_seconds": candidate_record.get("duration_seconds", 0),
                "baseline_tokens": int(baseline_record.get("input_tokens", 0))
                + int(baseline_record.get("output_tokens", 0)),
                "candidate_tokens": int(candidate_record.get("input_tokens", 0))
                + int(candidate_record.get("output_tokens", 0)),
            }
        )

    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[str(row["stratum"])].append(row)
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "inputs": {
            "baseline": str(baseline),
            "candidate": str(candidate),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
        },
        "headline": _comparison(rows),
        "family_bootstrap": _family_bootstrap(rows),
        "by_stratum": {stratum: _comparison(group) for stratum, group in sorted(strata.items())},
        "resources": _resources(rows),
        "disagreements": {
            "candidate_only_pass": [
                row["task_id"] for row in rows if row["candidate_pass"] and not row["baseline_pass"]
            ],
            "baseline_only_pass": [
                row["task_id"] for row in rows if row["baseline_pass"] and not row["candidate_pass"]
            ],
        },
        "tasks": rows,
    }
    output_name = Path(arguments.output_name)
    if output_name.name != arguments.output_name or output_name.suffix != ".json":
        raise ValueError("output name must be a JSON basename")
    output = candidate / output_name
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["validation"], sort_keys=True))
    print(json.dumps(result["headline"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
