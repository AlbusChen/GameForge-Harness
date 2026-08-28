#!/usr/bin/env python3
"""Validate a Fresh-50 candidate iteration against frozen Official and v4 receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_fresh50_official_v4 import (
    SAMPLES,
    SEED,
    _candidate_trace_metrics,
    _mcnemar,
    _percentile,
    _resource,
)

ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-experiment", type=Path, required=True)
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
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evaluator_status(row: dict[str, Any]) -> str:
    if row["condition"] == "official-default":
        return "PASS" if row["normalized_status"] == "PASS" else "FAIL"
    value = str(row["record"].get("official_gate", "NOT_RUN"))
    return value if value in {"PASS", "FAIL"} else "NOT_RUN"


def _bootstrap(deltas: list[float], *, seed_offset: int) -> dict[str, Any]:
    rng = random.Random(SEED + seed_offset)
    samples = [
        statistics.fmean(deltas[rng.randrange(len(deltas))] for _ in deltas)
        for _ in range(SAMPLES)
    ]
    return {
        "estimate": statistics.fmean(deltas),
        "ci95": [_percentile(samples, 0.025), _percentile(samples, 0.975)],
        "method": "paired task bootstrap percentile interval",
        "samples": SAMPLES,
        "seed": SEED + seed_offset,
    }


def _summary(rows: list[dict[str, Any]], strata: list[str]) -> dict[str, Any]:
    evaluator = Counter(_evaluator_status(row) for row in rows)
    terminal = Counter(str(row["normalized_status"]) for row in rows)
    return {
        "attempts": len(rows),
        "evaluator": {
            "status_counts": dict(sorted(evaluator.items())),
            "pass_rate": evaluator["PASS"] / len(rows),
        },
        "terminal": {
            "status_counts": dict(sorted(terminal.items())),
            "pass_rate": terminal["PASS"] / len(rows),
        },
        "by_stratum": {
            stratum: {
                "attempts": len(group),
                "passes": sum(_evaluator_status(row) == "PASS" for row in group),
                "pass_rate": sum(_evaluator_status(row) == "PASS" for row in group)
                / len(group),
            }
            for stratum in strata
            if (group := [row for row in rows if row["stratum"] == stratum])
        },
        "resources": _resource(rows),
    }


def main() -> int:
    arguments = _arguments()
    candidate_experiment = arguments.candidate_experiment.resolve(strict=True)
    baseline_experiment = arguments.baseline_experiment.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    protocol = json.loads(
        (candidate_experiment / "protocol-freeze.json").read_text(encoding="utf-8")
    )
    complete = json.loads(
        (candidate_experiment / "complete.json").read_text(encoding="utf-8")
    )
    candidate_rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((candidate_experiment / "receipts").glob("*.json"))
    ]
    baseline_rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((baseline_experiment / "receipts").glob("*.json"))
    ]
    errors: list[str] = []
    if len(candidate_rows) != 50:
        errors.append(f"expected 50 candidate receipts, observed {len(candidate_rows)}")
    if len(baseline_rows) != 100:
        errors.append(f"expected 100 baseline receipts, observed {len(baseline_rows)}")
    if complete.get("attempts_completed") != 50:
        errors.append("candidate complete marker does not report 50 attempts")
    if protocol.get("manifest_sha256") != _sha256(manifest_path):
        errors.append("candidate manifest digest mismatch")
    if protocol.get("baseline_complete_sha256") != _sha256(
        baseline_experiment / "complete.json"
    ):
        errors.append("frozen baseline complete digest mismatch")
    expected_ids = [str(task["task_id"]) for task in manifest["tasks"]]
    if [str(row["task_id"]) for row in candidate_rows] != expected_ids:
        errors.append("candidate receipt order differs from manifest")
    for row in candidate_rows:
        source = Path(row["result_source"])
        if not source.is_file() or _sha256(source) != row["result_source_sha256"]:
            errors.append(f"candidate result source mismatch: {row['attempt_id']}")

    condition = str(protocol["condition"])
    rows_by_key = {
        (str(row["task_id"]), str(row["condition"])): row
        for row in [*baseline_rows, *candidate_rows]
    }
    official_rows = [
        rows_by_key[(task_id, "official-default")] for task_id in expected_ids
    ]
    v4_rows = [
        rows_by_key[(task_id, "programmable-open-global-diagnostic-v4")]
        for task_id in expected_ids
    ]
    candidate_ordered = [rows_by_key[(task_id, condition)] for task_id in expected_ids]
    official_pass = [_evaluator_status(row) == "PASS" for row in official_rows]
    v4_pass = [_evaluator_status(row) == "PASS" for row in v4_rows]
    candidate_pass = [_evaluator_status(row) == "PASS" for row in candidate_ordered]
    candidate_minus_official = [
        float(candidate) - float(official)
        for candidate, official in zip(candidate_pass, official_pass, strict=True)
    ]
    candidate_minus_v4 = [
        float(candidate) - float(v4)
        for candidate, v4 in zip(candidate_pass, v4_pass, strict=True)
    ]
    traces = [_candidate_trace_metrics(row) for row in candidate_ordered]
    strata = sorted({str(task["stratum"]) for task in manifest["tasks"]})
    tasks = [
        {
            "task_id": task_id,
            "task_name": manifest["tasks"][index]["name"],
            "stratum": manifest["tasks"][index]["stratum"],
            "official": _evaluator_status(official_rows[index]),
            "v4": _evaluator_status(v4_rows[index]),
            "candidate": _evaluator_status(candidate_ordered[index]),
            "candidate_terminal": candidate_ordered[index]["normalized_status"],
            "candidate_trace": traces[index],
        }
        for index, task_id in enumerate(expected_ids)
    ]
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {
            "valid": not errors,
            "errors": errors,
            "candidate_receipts": len(candidate_rows),
            "baseline_receipts": len(baseline_rows),
        },
        "inputs": {
            "candidate_experiment": str(candidate_experiment),
            "baseline_experiment": str(baseline_experiment),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "condition": condition,
            "runtime_tree_sha256": protocol["runtime_tree_sha256"],
        },
        "overall": {
            "official-default": _summary(official_rows, strata),
            "programmable-open-global-diagnostic-v4": _summary(v4_rows, strata),
            condition: _summary(candidate_ordered, strata),
        },
        "paired": {
            "candidate_minus_official": _bootstrap(
                candidate_minus_official, seed_offset=1
            ),
            "candidate_vs_official_mcnemar": _mcnemar(
                candidate_pass, official_pass
            ),
            "candidate_minus_v4": _bootstrap(candidate_minus_v4, seed_offset=2),
            "candidate_vs_v4_mcnemar": _mcnemar(candidate_pass, v4_pass),
        },
        "candidate_controls": {
            "preservation_gate": dict(
                sorted(
                    Counter(
                        str(row["record"].get("preservation_gate", "NOT_RUN"))
                        for row in candidate_ordered
                    ).items()
                )
            ),
            "policy_violations": sum(
                int(row["record"].get("policy_violations", 0))
                for row in candidate_ordered
            ),
            "execution_errors": {
                "program_cell_errors": sum(
                    int(trace["program_cell_errors"]) for trace in traces
                ),
                "decision_errors": sum(int(trace["decision_errors"]) for trace in traces),
                "tasks_with_errors": sum(bool(trace["errors"]) for trace in traces),
                "messages": dict(
                    sorted(
                        Counter(
                            error for trace in traces for error in trace["errors"]
                        ).items()
                    )
                ),
            },
        },
        "transitions": {
            "v4_fail_to_candidate_pass": [
                task["task_id"]
                for task in tasks
                if task["v4"] != "PASS" and task["candidate"] == "PASS"
            ],
            "v4_pass_to_candidate_fail": [
                task["task_id"]
                for task in tasks
                if task["v4"] == "PASS" and task["candidate"] != "PASS"
            ],
        },
        "quota": {
            "minimum_remaining_percent_observed": 100
            - max(float(row["weekly_used_percent_before"]) for row in candidate_ordered)
        },
        "tasks": tasks,
    }
    output = candidate_experiment / "analysis.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["validation"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
