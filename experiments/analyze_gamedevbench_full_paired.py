#!/usr/bin/env python3
"""Analyze the complete paired v19 Harness and local Official GameDevBench runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_gamedevbench_full import _percentile, _rate, _wilson

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TASKS = 332


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-provenance-v19-run1",
    )
    parser.add_argument(
        "--official",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-official-run1",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/gamedevbench-full-qualified332-v1.json",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_mcnemar(candidate_only: int, official_only: int) -> float | None:
    discordant = candidate_only + official_only
    if not discordant:
        return None
    lower = min(candidate_only, official_only)
    tail = sum(math.comb(discordant, value) for value in range(lower + 1)) / 2**discordant
    return min(1.0, 2 * tail)


def _comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_passes = sum(row["candidate_pass"] for row in rows)
    official_passes = sum(row["official_pass"] for row in rows)
    both = sum(row["candidate_pass"] and row["official_pass"] for row in rows)
    candidate_only = sum(row["candidate_pass"] and not row["official_pass"] for row in rows)
    official_only = sum(row["official_pass"] and not row["candidate_pass"] for row in rows)
    neither = len(rows) - both - candidate_only - official_only
    return {
        "attempts": len(rows),
        "candidate": {
            "passes": candidate_passes,
            "pass_rate": _rate(candidate_passes, len(rows)),
            "wilson_ci95": _wilson(candidate_passes, len(rows)),
        },
        "official_local": {
            "passes": official_passes,
            "pass_rate": _rate(official_passes, len(rows)),
            "wilson_ci95": _wilson(official_passes, len(rows)),
        },
        "candidate_minus_official": _rate(candidate_passes - official_passes, len(rows)),
        "paired_outcomes": {
            "both_pass": both,
            "candidate_only_pass": candidate_only,
            "official_only_pass": official_only,
            "neither_pass": neither,
        },
        "mcnemar_exact_two_sided_p": _exact_mcnemar(candidate_only, official_only),
    }


def _cluster_bootstrap(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["family_key"])].append(row)
    families = list(groups.values())
    rng = random.Random(20260813)
    differences: list[float] = []
    for _ in range(20_000):
        sample = [rng.choice(families) for _ in families]
        flat = [row for family in sample for row in family]
        difference = sum(
            int(row["candidate_pass"]) - int(row["official_pass"]) for row in flat
        ) / len(flat)
        differences.append(difference)
    return {
        "family_count": len(families),
        "candidate_minus_official_ci95": [
            _percentile(differences, 0.025),
            _percentile(differences, 0.975),
        ],
        "replicates": len(differences),
        "seed": 20260813,
        "resampling_unit": "complete public-derived family",
    }


def _resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_durations = sorted(float(row["candidate_duration_seconds"]) for row in rows)
    official_durations = sorted(float(row["official_duration_seconds"]) for row in rows)
    candidate_tokens = [int(row["candidate_tokens"]) for row in rows]
    official_tokens = [int(row["official_tokens"]) for row in rows]
    return {
        "candidate": {
            "total_tokens": sum(candidate_tokens),
            "mean_tokens_per_task": sum(candidate_tokens) / len(candidate_tokens),
            "total_solver_seconds": sum(candidate_durations),
            "median_solver_seconds": _percentile(candidate_durations, 0.5),
            "p95_solver_seconds": _percentile(candidate_durations, 0.95),
        },
        "official_local": {
            "total_tokens": sum(official_tokens),
            "mean_tokens_per_task": sum(official_tokens) / len(official_tokens),
            "total_solver_seconds": sum(official_durations),
            "median_solver_seconds": _percentile(official_durations, 0.5),
            "p95_solver_seconds": _percentile(official_durations, 0.95),
        },
    }


def main() -> int:
    arguments = _arguments()
    candidate = arguments.candidate.resolve(strict=True)
    official = arguments.official.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_task = {str(task["task_id"]): task for task in manifest["tasks"]}

    candidate_complete = json.loads((candidate / "complete.json").read_text(encoding="utf-8"))
    candidate_rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((candidate / "receipts").glob("*.json"))
    ]
    official_complete = json.loads((official / "complete.json").read_text(encoding="utf-8"))
    declared_official_source = Path(str(official_complete["result_source"]))
    if declared_official_source.is_file():
        official_source = declared_official_source.resolve(strict=True)
    else:
        renamed_sources = sorted(
            declared_official_source.parent.glob("*_final_results.json")
        )
        if len(renamed_sources) != 1:
            raise FileNotFoundError(
                "Official result source is missing and no unique renamed result exists: "
                f"{declared_official_source}"
            )
        official_source = renamed_sources[0].resolve(strict=True)
    official_payload = json.loads(official_source.read_text(encoding="utf-8"))
    official_by_task = {str(row["task_name"]): row for row in official_payload.get("tasks", [])}
    errors: list[str] = []
    if len(candidate_rows) != EXPECTED_TASKS:
        errors.append("candidate does not contain 332 receipts")
    if candidate_complete.get("attempts_completed") != EXPECTED_TASKS:
        errors.append("candidate complete marker is invalid")
    if official_complete.get("attempts_completed") != EXPECTED_TASKS:
        errors.append("Official complete marker is invalid")
    if _sha256(official_source) != official_complete.get("result_source_sha256"):
        errors.append("Official result source digest mismatch")
    expected_ids = set(by_task)
    if set(official_by_task) != expected_ids:
        errors.append("Official task identities differ from the manifest")

    rows: list[dict[str, Any]] = []
    for candidate_row in candidate_rows:
        task_id = str(candidate_row["task_id"])
        official_row = official_by_task.get(task_id, {})
        candidate_record = candidate_row["record"]
        rows.append(
            {
                "task_id": task_id,
                "stratum": candidate_row["stratum"],
                "family_key": by_task[task_id]["family_key"],
                "candidate_pass": candidate_row["normalized_status"] == "PASS",
                "official_pass": official_row.get("success") is True,
                "candidate_status": candidate_row["normalized_status"],
                "official_solver_success": official_row.get("solver_success"),
                "official_message": official_row.get("message"),
                "candidate_duration_seconds": float(candidate_record.get("duration_seconds", 0)),
                "official_duration_seconds": float(official_row.get("solver_duration", 0)),
                "candidate_tokens": int(candidate_record.get("input_tokens", 0))
                + int(candidate_record.get("output_tokens", 0)),
                "official_tokens": int(official_row.get("total_tokens", 0)),
            }
        )

    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[str(row["stratum"])].append(row)
    reference = manifest["official_reference"]
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "inputs": {
            "candidate": str(candidate),
            "official": str(official),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "official_result_source": str(official_source),
            "official_result_source_sha256": _sha256(official_source),
        },
        "headline": _comparison(rows),
        "cluster_bootstrap": _cluster_bootstrap(rows),
        "by_stratum": {stratum: _comparison(group) for stratum, group in sorted(strata.items())},
        "published_official_reference": reference,
        "resources": _resources(rows),
        "disagreements": {
            "candidate_only_pass": [
                row["task_id"] for row in rows if row["candidate_pass"] and not row["official_pass"]
            ],
            "official_only_pass": [
                row["task_id"] for row in rows if row["official_pass"] and not row["candidate_pass"]
            ],
        },
        "tasks": rows,
    }
    output = candidate / "paired-official-analysis.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["validation"], sort_keys=True))
    print(json.dumps(result["headline"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
