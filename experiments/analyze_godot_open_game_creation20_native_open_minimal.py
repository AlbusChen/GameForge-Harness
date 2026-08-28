#!/usr/bin/env python3
"""Blind-review native-open vs Official and compare all open20 conditions."""

from __future__ import annotations

import argparse
import json
import math
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

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/godot-open-game-creation20-v1.json"
DEFAULT_CANDIDATE_EXPERIMENT = (
    ROOT / "runs/experiments/godot-open-game-creation20-native-open-minimal-run2"
)
DEFAULT_HISTORICAL_EXPERIMENT = (
    ROOT / "runs/experiments/godot-open-game-creation20-v1-run1"
)
CANDIDATE = "native-open-minimal"
BASELINE = "baseline-minimal-open"
OFFICIAL = "official-local"
CRASH_CODES = {134, 139, -6, -11}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--candidate-experiment", type=Path, default=DEFAULT_CANDIDATE_EXPERIMENT
    )
    parser.add_argument(
        "--historical-experiment", type=Path, default=DEFAULT_HISTORICAL_EXPERIMENT
    )
    parser.add_argument("--agent-executable", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--max-new-judges", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _rows(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((directory / "receipts").glob("*.json"))
    ]


def _validate_log(row: dict[str, Any]) -> None:
    if "infrastructure_error" in row:
        raise RuntimeError(f"attempt has infrastructure error: {row['attempt_id']}")
    log = Path(row["solver_log"])
    if not log.is_file() or _sha256_file(log) != row["solver_log_sha256"]:
        raise RuntimeError(f"solver log digest mismatch: {row['attempt_id']}")


def _labels(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        str(task_id): {
            str(label): CANDIDATE if condition == BASELINE else str(condition)
            for label, condition in mapping.items()
        }
        for task_id, mapping in manifest["judge_labels"].items()
    }


def _receipt_digests(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(row["task_id"]): _sha256_file(Path(row["solver_log"])) for row in rows
    }


def _usage(group: list[dict[str, Any]], key: str) -> int:
    return sum(int(row["agent"]["usage"].get(key, 0)) for row in group)


def _aggregate(group: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(row["duration_seconds"]) for row in group]
    return {
        "attempts": len(group),
        "hard_gate_passes": sum(
            row["evaluation"]["hard_gate"] == "PASS" for row in group
        ),
        "solver_successes": sum(row["solver_return_code"] == 0 for row in group),
        "solver_timeouts": sum(bool(row["solver_timed_out"]) for row in group),
        "duration_seconds_total": round(sum(durations), 3),
        "duration_seconds_mean": round(statistics.fmean(durations), 3),
        "duration_seconds_median": round(statistics.median(durations), 3),
        "input_tokens_total": _usage(group, "input_tokens"),
        "cached_input_tokens_total": _usage(group, "cached_input_tokens"),
        "output_tokens_total": _usage(group, "output_tokens"),
        "reasoning_output_tokens_total": _usage(group, "reasoning_output_tokens"),
        "tool_calls_total": sum(int(row["agent"]["tool_calls"]) for row in group),
        "command_calls_total": sum(int(row["agent"]["command_count"]) for row in group),
        "command_crash_exit_count": sum(
            int(row["agent"]["command_crash_exit_count"]) for row in group
        ),
        "crash_exposed_tasks": sum(
            int(row["agent"]["command_crash_exit_count"]) > 0 for row in group
        ),
        "evaluator_retry_count": sum(
            int(row["evaluation"]["import"]["attempt_count"]) - 1
            + int(row["evaluation"]["probe"]["attempt_count"]) - 1
            for row in group
        ),
    }


def _percent_delta(left: float, right: float) -> float:
    return round((left / right - 1) * 100, 3)


def _paired_efficiency(
    candidate: list[dict[str, Any]],
    comparator: list[dict[str, Any]],
) -> dict[str, Any]:
    by_task = {str(row["task_id"]): row for row in comparator}
    deltas = [
        float(row["duration_seconds"])
        - float(by_task[str(row["task_id"])]["duration_seconds"])
        for row in candidate
    ]
    return {
        "candidate_faster_tasks": sum(delta < 0 for delta in deltas),
        "comparator_faster_tasks": sum(delta > 0 for delta in deltas),
        "tied_tasks": sum(delta == 0 for delta in deltas),
        "paired_duration_delta_seconds_mean": round(statistics.fmean(deltas), 3),
        "paired_duration_delta_seconds_median": round(statistics.median(deltas), 3),
        "task_duration_deltas": {
            str(row["task_id"]): round(
                float(row["duration_seconds"])
                - float(by_task[str(row["task_id"])]["duration_seconds"]),
                3,
            )
            for row in candidate
        },
    }


def _crash_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        for line in Path(row["solver_log"]).read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            code = item.get("exit_code")
            if not isinstance(code, int) or code not in CRASH_CODES:
                continue
            command = str(item.get("command", ""))
            output = str(item.get("aggregated_output", ""))
            headless_movie = "--headless" in command and "--write-movie" in command
            records.append(
                {
                    "task_id": row["task_id"],
                    "exit_code": code,
                    "command": command,
                    "classification": (
                        "godot-4.4-headless-moviewriter-incompatibility"
                        if headless_movie
                        else "unclassified"
                    ),
                    "movie_writer_signature_visible": (
                        "MovieWriter" in output or "Movie Maker" in output
                    ),
                }
            )
    return records


def _sign_test_two_sided(left: int, right: int) -> float | None:
    total = left + right
    if total == 0:
        return None
    tail = sum(math.comb(total, value) for value in range(min(left, right) + 1))
    return round(min(1.0, 2 * tail / (2**total)), 8)


def _quality_analysis(
    manifest: dict[str, Any],
    labels: dict[str, dict[str, str]],
    rows: list[dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_task_condition = {
        (str(row["task_id"]), str(row["condition"])): row for row in rows
    }
    scores: dict[str, dict[str, list[float]]] = {
        condition: {dimension: [] for dimension in (*DIMENSIONS, "total")}
        for condition in (CANDIDATE, OFFICIAL)
    }
    preferences: Counter[str] = Counter()
    task_records: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        task_id = str(task["task_id"])
        judgment = judgments[task_id]["judgment"]
        mapped: dict[str, Any] = {}
        for label in ("A", "B"):
            condition = labels[task_id][label]
            record = judgment[label]
            total = sum(int(record[dimension]) for dimension in DIMENSIONS)
            mapped[condition] = {**record, "total": total, "judge_label": label}
            for dimension in DIMENSIONS:
                scores[condition][dimension].append(float(record[dimension]))
            scores[condition]["total"].append(float(total))
        preferred_label = judgment["pairwise_preference"]
        preferred = (
            labels[task_id][preferred_label]
            if preferred_label in {"A", "B"}
            else "TIE"
        )
        preferences[preferred] += 1
        task_records.append(
            {
                "task_id": task_id,
                "genre": task["genre"],
                "hard_gates": {
                    condition: by_task_condition[(task_id, condition)]["evaluation"][
                        "hard_gate"
                    ]
                    for condition in (CANDIDATE, OFFICIAL)
                },
                "scores": mapped,
                "pairwise_preference": preferred,
                "preference_reason": judgment["preference_reason"],
                "confidence": judgment["confidence"],
            }
        )
    deltas = [
        left - right
        for left, right in zip(
            scores[CANDIDATE]["total"], scores[OFFICIAL]["total"], strict=True
        )
    ]
    return {
        "judge_score_means": {
            condition: {
                dimension: _mean(values)
                for dimension, values in dimensions.items()
            }
            for condition, dimensions in scores.items()
        },
        "preference_counts": dict(sorted(preferences.items())),
        "preference_sign_test_two_sided_p": _sign_test_two_sided(
            preferences[CANDIDATE], preferences[OFFICIAL]
        ),
        "candidate_minus_official_total_score_deltas": deltas,
        "mean_total_score_delta": _mean(deltas),
        "median_total_score_delta": round(statistics.median(deltas), 6),
        "tasks": task_records,
    }


def _build_analysis(
    manifest: dict[str, Any],
    candidate_protocol: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
    historical_rows: list[dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
    labels: dict[str, dict[str, str]],
    historical_analysis: dict[str, Any],
) -> dict[str, Any]:
    groups = {
        CANDIDATE: candidate_rows,
        BASELINE: [row for row in historical_rows if row["condition"] == BASELINE],
        OFFICIAL: [row for row in historical_rows if row["condition"] == OFFICIAL],
    }
    aggregate = {condition: _aggregate(group) for condition, group in groups.items()}
    candidate_aggregate = aggregate[CANDIDATE]
    comparisons: dict[str, Any] = {}
    for comparator in (BASELINE, OFFICIAL):
        other = aggregate[comparator]
        comparisons[comparator] = {
            "duration_total_percent_delta": _percent_delta(
                candidate_aggregate["duration_seconds_total"],
                other["duration_seconds_total"],
            ),
            "duration_median_percent_delta": _percent_delta(
                candidate_aggregate["duration_seconds_median"],
                other["duration_seconds_median"],
            ),
            "input_tokens_percent_delta": _percent_delta(
                candidate_aggregate["input_tokens_total"], other["input_tokens_total"]
            ),
            "output_tokens_percent_delta": _percent_delta(
                candidate_aggregate["output_tokens_total"], other["output_tokens_total"]
            ),
            "tool_calls_percent_delta": _percent_delta(
                candidate_aggregate["tool_calls_total"], other["tool_calls_total"]
            ),
            **_paired_efficiency(candidate_rows, groups[comparator]),
        }
    crash_records = _crash_records(candidate_rows)
    logs = "\n".join(
        Path(row["solver_log"]).read_text(encoding="utf-8") for row in candidate_rows
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {
            "valid": len(candidate_rows) == 20
            and len(historical_rows) == 40
            and len(judgments) == 20,
            "candidate_receipt_count": len(candidate_rows),
            "historical_receipt_count": len(historical_rows),
            "judgment_count": len(judgments),
            "source_manifest_sha256": candidate_protocol["source_manifest_sha256"],
            "candidate_runtime_tree_sha256": candidate_protocol[
                "candidate_runtime_tree_sha256"
            ],
        },
        "design_warning": (
            "All conditions use identical briefs/model/effort and frozen artifacts, but the "
            "candidate is a later single run rather than a concurrent randomized repetition."
        ),
        "aggregate": aggregate,
        "candidate_comparisons": comparisons,
        "crash_audit": {
            "candidate_crash_command_count": len(crash_records),
            "classified_headless_moviewriter_count": sum(
                record["classification"]
                == "godot-4.4-headless-moviewriter-incompatibility"
                for record in crash_records
            ),
            "unclassified_count": sum(
                record["classification"] == "unclassified" for record in crash_records
            ),
            "permission_error_present": "PermissionError" in logs
            or "Operation not permitted" in logs,
            "unintended_godot_4_7_present": "4.7.1" in logs,
            "records": crash_records,
        },
        "quality_candidate_vs_official": _quality_analysis(
            manifest,
            labels,
            [*candidate_rows, *groups[OFFICIAL]],
            judgments,
        ),
        "historical_quality_baseline_vs_official": historical_analysis["pairwise"],
    }


def main() -> int:
    arguments = _arguments()
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate_experiment = arguments.candidate_experiment.resolve(strict=True)
    historical_experiment = arguments.historical_experiment.resolve(strict=True)
    agent = arguments.agent_executable.resolve(strict=True)
    candidate_protocol_path = candidate_experiment / "protocol-freeze.json"
    historical_protocol_path = historical_experiment / "protocol-freeze.json"
    candidate_protocol = json.loads(candidate_protocol_path.read_text(encoding="utf-8"))
    candidate_complete = json.loads(
        (candidate_experiment / "complete.json").read_text(encoding="utf-8")
    )
    historical_complete = json.loads(
        (historical_experiment / "complete.json").read_text(encoding="utf-8")
    )
    if candidate_protocol["source_manifest_sha256"] != _sha256_file(manifest_path):
        raise RuntimeError("candidate source manifest differs from the frozen protocol")
    if candidate_complete["attempts_completed"] != 20:
        raise RuntimeError("candidate experiment is incomplete")
    if historical_complete["attempts_completed"] != 40:
        raise RuntimeError("historical experiment is incomplete")
    candidate_rows = _rows(candidate_experiment)
    historical_rows = _rows(historical_experiment)
    if len(candidate_rows) != 20 or [row["ordinal"] for row in candidate_rows] != list(
        range(1, 21)
    ):
        raise RuntimeError("candidate receipt count/order differs from protocol")
    if len(historical_rows) != 40 or [row["ordinal"] for row in historical_rows] != list(
        range(1, 41)
    ):
        raise RuntimeError("historical receipt count/order differs from protocol")
    for row in (*candidate_rows, *historical_rows):
        _validate_log(row)
    labels = _labels(manifest)
    official_rows = [row for row in historical_rows if row["condition"] == OFFICIAL]
    judge_protocol = {
        "schema_version": 1,
        "source_manifest_sha256": _sha256_file(manifest_path),
        "candidate_execution_protocol_sha256": _sha256_file(candidate_protocol_path),
        "historical_execution_protocol_sha256": _sha256_file(historical_protocol_path),
        "analyzer_sha256": _sha256_file(Path(__file__).resolve()),
        "judge": str(agent),
        "judge_sha256": _sha256_file(agent),
        "model": manifest["execution"]["model"],
        "reasoning_effort": manifest["execution"]["reasoning_effort"],
        "dimensions": list(DIMENSIONS),
        "labels": labels,
        "candidate_solver_logs": _receipt_digests(candidate_rows),
        "official_solver_logs": _receipt_digests(official_rows),
        "condition_blinded": True,
        "trajectory_and_efficiency_blinded": True,
        "comparison": [CANDIDATE, OFFICIAL],
    }
    _write_json(candidate_experiment / "judge-protocol-freeze.json", judge_protocol)
    if arguments.prepare_only:
        print(candidate_experiment / "judge-protocol-freeze.json")
        return 0

    by_task_condition = {
        (str(row["task_id"]), str(row["condition"])): row
        for row in (*candidate_rows, *official_rows)
    }
    existing = {
        path.stem for path in (candidate_experiment / "judgments").glob("*.json")
    }
    pending = [task for task in manifest["tasks"] if task["task_id"] not in existing]
    if arguments.max_new_judges is not None:
        if arguments.max_new_judges < 0:
            raise ValueError("max-new-judges must be nonnegative")
        pending = pending[: arguments.max_new_judges]
    for index, task in enumerate(pending, start=1):
        task_id = str(task["task_id"])
        label_rows = {
            label: by_task_condition[(task_id, condition)]
            for label, condition in labels[task_id].items()
        }
        record = _run_judge(
            task=task,
            label_rows=label_rows,
            destination=candidate_experiment / "judge-artifacts" / task_id,
            agent=agent,
            model=str(manifest["execution"]["model"]),
            effort=str(manifest["execution"]["reasoning_effort"]),
        )
        _write_json(candidate_experiment / "judgments" / f"{task_id}.json", record)
        print(f"[{index}/{len(pending)}] judged {task_id}", flush=True)
    judgments = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((candidate_experiment / "judgments").glob("*.json"))
    }
    if len(judgments) == 20:
        historical_analysis = json.loads(
            (historical_experiment / "analysis.json").read_text(encoding="utf-8")
        )
        analysis = _build_analysis(
            manifest,
            candidate_protocol,
            candidate_rows,
            historical_rows,
            judgments,
            labels,
            historical_analysis,
        )
        _replace_json(candidate_experiment / "analysis.json", analysis)
        print(candidate_experiment / "analysis.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
