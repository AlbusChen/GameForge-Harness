#!/usr/bin/env python3
"""Validate and summarize the versioned open-request proxy receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/unity-open-request-proxy-v2.json",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def main() -> int:
    arguments = _arguments()
    experiment = arguments.experiment.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    protocol = json.loads((experiment / "protocol-freeze.json").read_text(encoding="utf-8"))
    complete = json.loads((experiment / "complete.json").read_text(encoding="utf-8"))
    receipt_paths = sorted((experiment / "receipts").glob("*.json"))
    rows: list[dict[str, Any]] = [
        json.loads(path.read_text(encoding="utf-8")) for path in receipt_paths
    ]
    errors: list[str] = []
    cases = manifest["cases"]
    if len(rows) != len(cases):
        errors.append(f"expected {len(cases)} receipts, observed {len(rows)}")
    if complete.get("cases_completed") != len(cases):
        errors.append("complete marker case count mismatch")
    if protocol.get("manifest_sha256") != _sha256(manifest_path):
        errors.append("manifest digest mismatch")
    expected_ids = [str(case["id"]) for case in cases]
    observed_ids = [str(row["case"]["id"]) for row in rows]
    if observed_ids != expected_ids:
        errors.append("receipt order or case identity differs from manifest")
    for row in rows:
        log_path = Path(row["log_path"])
        if not log_path.is_file() or _sha256(log_path) != row["log_sha256"]:
            errors.append(f"log digest mismatch: {row['case']['id']}")
        result_path_value = row.get("result_path")
        if result_path_value:
            result_path = Path(result_path_value)
            if not result_path.is_file() or _sha256(result_path) != row["result_sha256"]:
                errors.append(f"result digest mismatch: {row['case']['id']}")

    by_class: dict[str, Any] = {}
    for intent_class in sorted({str(row["case"]["intent_class"]) for row in rows}):
        group = [row for row in rows if row["case"]["intent_class"] == intent_class]
        passed = sum(row["status"] == "PASS" for row in group)
        by_class[intent_class] = {
            "cases": len(group),
            "passes": passed,
            "pass_rate": _rate(passed, len(group)),
        }

    executed = [row for row in rows if row["case"]["mode"] != "unapproved"]
    underspecified = [
        row for row in rows if row["case"]["intent_class"] == "underspecified_mutation"
    ]
    first_passes = sum(row.get("first_pass") is True for row in executed)
    safe_dispositions = sum(
        row.get("clarification_or_approval_required") is True for row in underspecified
    )
    preserved = sum(row.get("unrelated_files_preserved") is True for row in rows)
    rework_values = [int(row.get("rework_rounds", 0)) for row in executed]
    pass_count = sum(row["status"] == "PASS" for row in rows)
    analysis = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {
            "valid": not errors,
            "errors": errors,
            "expected_receipts": len(cases),
            "observed_receipts": len(rows),
        },
        "inputs": {
            "experiment": str(experiment),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "runtime_tree_sha256": protocol["runtime_tree_sha256"],
            "condition": protocol["condition"],
        },
        "overall": {
            "cases": len(rows),
            "passes": pass_count,
            "pass_rate": _rate(pass_count, len(rows)),
            "by_intent_class": by_class,
        },
        "mechanism_metrics": {
            "underspecified_safe_disposition": {
                "eligible_cases": len(underspecified),
                "observed": safe_dispositions,
                "rate": _rate(safe_dispositions, len(underspecified)),
                "interpretation": (
                    "Safe refusal/approval response among deliberately unapproved scenarios; "
                    "not a natural user clarification rate."
                ),
            },
            "first_pass": {
                "executed_cases": len(executed),
                "observed": first_passes,
                "rate": _rate(first_passes, len(executed)),
                "definition": (
                    "Case passed without program-cell or structured-decision error events."
                ),
            },
            "rework_rounds": {
                "executed_cases": len(executed),
                "total": sum(rework_values),
                "mean": statistics.fmean(rework_values) if rework_values else None,
                "median": statistics.median(rework_values) if rework_values else None,
                "maximum": max(rework_values, default=0),
                "distribution": dict(sorted(Counter(rework_values).items())),
                "definition": "Program-cell plus structured-decision error events.",
            },
            "unrelated_file_preservation": {
                "cases": len(rows),
                "preserved": preserved,
                "rate": _rate(preserved, len(rows)),
            },
        },
        "resources": {
            "duration_seconds_total": sum(float(row["duration_seconds"]) for row in rows),
            "duration_seconds_median": statistics.median(
                float(row["duration_seconds"]) for row in rows
            ),
            "minimum_quota_remaining_percent": 100
            - max(float(row["weekly_used_percent_before"]) for row in rows),
        },
        "cases": [
            {
                "id": row["case"]["id"],
                "intent_class": row["case"]["intent_class"],
                "mode": row["case"]["mode"],
                "status": row["status"],
                "first_pass": row.get("first_pass"),
                "rework_rounds": row.get("rework_rounds", 0),
                "unrelated_files_preserved": row.get("unrelated_files_preserved"),
                "changed_paths": row.get("changed_paths", []),
                "duration_seconds": row["duration_seconds"],
            }
            for row in rows
        ],
    }
    output = experiment / "analysis.json"
    output.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(analysis["validation"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
