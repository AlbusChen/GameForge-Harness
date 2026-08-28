#!/usr/bin/env python3
"""Blind-review and summarize the paired 20-genre open Godot creation suite."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_godot_open_game_creation6 import (
    DIMENSIONS,
    _mean,
    _run_judge,
)
from experiments.run_godot_open_game_creation6 import (
    DEFAULT_AGENT,
    _replace_json,
    _sha256_file,
    _write_json,
)
from experiments.run_godot_open_game_creation20 import (
    DEFAULT_EXPERIMENT,
    DEFAULT_MANIFEST,
)

ROOT = Path(__file__).resolve().parents[1]
SHARED_ANALYZER = ROOT / "experiments/analyze_godot_open_game_creation6.py"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--agent-executable", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--max-new-judges", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _build_analysis(
    manifest: dict[str, Any],
    protocol: dict[str, Any],
    rows: list[dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    labels = manifest["judge_labels"]
    conditions = tuple(str(value) for value in manifest["design"]["conditions"])
    task_count = int(manifest["design"]["task_count"])
    expected_receipts = task_count * len(conditions)
    by_task_condition = {(row["task_id"], row["condition"]): row for row in rows}
    condition_scores: dict[str, dict[str, list[float]]] = {
        condition: {dimension: [] for dimension in (*DIMENSIONS, "total")}
        for condition in conditions
    }
    preference_counts: Counter[str] = Counter()
    task_records: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        task_id = str(task["task_id"])
        judgment = judgments[task_id]["judgment"]
        mapped_scores: dict[str, Any] = {}
        for label in ("A", "B"):
            condition = labels[task_id][label]
            score = judgment[label]
            total = sum(int(score[dimension]) for dimension in DIMENSIONS)
            mapped_scores[condition] = {**score, "total": total, "judge_label": label}
            for dimension in DIMENSIONS:
                condition_scores[condition][dimension].append(float(score[dimension]))
            condition_scores[condition]["total"].append(float(total))
        preference_label = judgment["pairwise_preference"]
        preferred_condition = (
            labels[task_id][preference_label] if preference_label in {"A", "B"} else "TIE"
        )
        preference_counts[preferred_condition] += 1
        task_records.append(
            {
                "task_id": task_id,
                "genre": task["genre"],
                "hard_gates": {
                    condition: by_task_condition[(task_id, condition)]["evaluation"]["hard_gate"]
                    for condition in conditions
                },
                "scores": mapped_scores,
                "pairwise_preference": preferred_condition,
                "preference_reason": judgment["preference_reason"],
                "confidence": judgment["confidence"],
            }
        )
    aggregates: dict[str, Any] = {}
    for condition in conditions:
        group = [row for row in rows if row["condition"] == condition]
        aggregates[condition] = {
            "attempts": len(group),
            "hard_gate_passes": sum(row["evaluation"]["hard_gate"] == "PASS" for row in group),
            "solver_successes": sum(row["solver_return_code"] == 0 for row in group),
            "duration_seconds_total": round(sum(row["duration_seconds"] for row in group), 3),
            "duration_seconds_median": round(
                statistics.median(row["duration_seconds"] for row in group), 3
            ),
            "input_tokens_total": sum(row["agent"]["usage"]["input_tokens"] for row in group),
            "output_tokens_total": sum(row["agent"]["usage"]["output_tokens"] for row in group),
            "tool_calls_total": sum(row["agent"]["tool_calls"] for row in group),
            "command_crash_exit_count": sum(
                row["agent"]["command_crash_exit_count"] for row in group
            ),
            "judge_score_means": {
                dimension: _mean(values)
                for dimension, values in condition_scores[condition].items()
            },
        }
    baseline = condition_scores["baseline-minimal-open"]["total"]
    official = condition_scores["official-local"]["total"]
    paired_deltas = [left - right for left, right in zip(baseline, official, strict=True)]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {
            "valid": len(rows) == expected_receipts and len(judgments) == task_count,
            "receipt_count": len(rows),
            "judgment_count": len(judgments),
            "manifest_sha256": protocol["manifest_sha256"],
            "baseline_runtime_tree_sha256": protocol["baseline_runtime_tree_sha256"],
        },
        "design_warning": manifest["design"]["interpretation"],
        "aggregate": aggregates,
        "pairwise": {
            "preference_counts": dict(sorted(preference_counts.items())),
            "baseline_minus_official_total_score_deltas": paired_deltas,
            "mean_total_score_delta": _mean(paired_deltas),
            "median_total_score_delta": round(statistics.median(paired_deltas), 6),
        },
        "tasks": task_records,
    }


def main() -> int:
    arguments = _arguments()
    experiment = arguments.experiment.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    protocol = json.loads((experiment / "protocol-freeze.json").read_text(encoding="utf-8"))
    complete = json.loads((experiment / "complete.json").read_text(encoding="utf-8"))
    agent = arguments.agent_executable.resolve(strict=True)
    task_count = int(manifest["design"]["task_count"])
    expected_receipts = task_count * len(manifest["design"]["conditions"])
    if protocol["manifest_sha256"] != _sha256_file(manifest_path):
        raise RuntimeError("manifest differs from frozen execution protocol")
    if complete["attempts_completed"] != expected_receipts:
        raise RuntimeError("open creation experiment is incomplete")
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    if len(rows) != expected_receipts or [row["ordinal"] for row in rows] != list(
        range(1, expected_receipts + 1)
    ):
        raise RuntimeError("receipt count/order differs from frozen schedule")
    for row in rows:
        if "infrastructure_error" in row:
            raise RuntimeError(f"attempt has infrastructure error: {row['attempt_id']}")
        log = Path(row["solver_log"])
        if not log.is_file() or _sha256_file(log) != row["solver_log_sha256"]:
            raise RuntimeError(f"solver log digest mismatch: {row['attempt_id']}")
    judge_protocol = {
        "schema_version": 1,
        "manifest_sha256": protocol["manifest_sha256"],
        "execution_protocol_sha256": _sha256_file(experiment / "protocol-freeze.json"),
        "analyzer_sha256": _sha256_file(Path(__file__).resolve()),
        "shared_analyzer_dependency": str(SHARED_ANALYZER.resolve(strict=True)),
        "shared_analyzer_dependency_sha256": _sha256_file(
            SHARED_ANALYZER.resolve(strict=True)
        ),
        "judge": str(agent),
        "judge_sha256": _sha256_file(agent),
        "model": manifest["execution"]["model"],
        "reasoning_effort": manifest["execution"]["reasoning_effort"],
        "dimensions": list(DIMENSIONS),
        "labels": manifest["judge_labels"],
        "condition_blinded": True,
        "trajectory_and_efficiency_blinded": True,
    }
    _write_json(experiment / "judge-protocol-freeze.json", judge_protocol)
    if arguments.prepare_only:
        print(experiment / "judge-protocol-freeze.json")
        return 0
    by_task_condition = {(row["task_id"], row["condition"]): row for row in rows}
    existing = {path.stem for path in (experiment / "judgments").glob("*.json")}
    pending = [task for task in manifest["tasks"] if task["task_id"] not in existing]
    if arguments.max_new_judges is not None:
        if arguments.max_new_judges < 0:
            raise ValueError("max-new-judges must be nonnegative")
        pending = pending[: arguments.max_new_judges]
    for index, task in enumerate(pending, start=1):
        task_id = str(task["task_id"])
        label_rows = {
            label: by_task_condition[(task_id, condition)]
            for label, condition in manifest["judge_labels"][task_id].items()
        }
        record = _run_judge(
            task=task,
            label_rows=label_rows,
            destination=experiment / "judge-artifacts" / task_id,
            agent=agent,
            model=str(manifest["execution"]["model"]),
            effort=str(manifest["execution"]["reasoning_effort"]),
        )
        _write_json(experiment / "judgments" / f"{task_id}.json", record)
        print(f"[{index}/{len(pending)}] judged {task_id}", flush=True)
    judgments = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "judgments").glob("*.json"))
    }
    if len(judgments) == task_count:
        analysis = _build_analysis(manifest, protocol, rows, judgments)
        _replace_json(experiment / "analysis.json", analysis)
        print(experiment / "analysis.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
