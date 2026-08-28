#!/usr/bin/env python3
"""Run a frozen paired multi-genre open Godot creation suite."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.run_godot_open_game_creation6 import (
    DEFAULT_AGENT,
    DEFAULT_BASELINE,
    DEFAULT_GODOT,
    PROBE,
    _progress,
    _run_attempt,
    _sha256_file,
    _write_json,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/godot-open-game-creation20-v1.json"
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/godot-open-game-creation20-v1-run1"
DEPENDENCY_RUNNER = ROOT / "experiments/run_godot_open_game_creation6.py"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--experiment-directory", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--baseline-version", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--agent-executable", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--godot-executable", type=Path, default=DEFAULT_GODOT)
    parser.add_argument("--max-new-attempts", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _validate_manifest(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    design = manifest["design"]
    expected_tasks = int(design["task_count"])
    conditions = tuple(str(value) for value in design["conditions"])
    if conditions != ("baseline-minimal-open", "official-local"):
        raise ValueError("open creation suite requires the frozen two-condition comparison")
    if int(design["repetitions_per_task"]) != 1:
        raise ValueError("open creation suite currently requires one repetition per condition")
    task_entries = manifest["tasks"]
    tasks = {str(task["task_id"]): task for task in task_entries}
    schedule = manifest["schedule"]
    expected_attempts = expected_tasks * len(conditions)
    if len(task_entries) != expected_tasks or len(tasks) != expected_tasks:
        raise ValueError("task count or unique task IDs differ from the frozen design")
    if len(schedule) != expected_attempts:
        raise ValueError("schedule length differs from the frozen paired design")
    if [int(item["ordinal"]) for item in schedule] != list(range(1, expected_attempts + 1)):
        raise ValueError("schedule ordinals must be exact and contiguous")
    for task_id in tasks:
        paired = [str(item["condition"]) for item in schedule if item["task_id"] == task_id]
        if sorted(paired) != sorted(conditions):
            raise ValueError(f"task is not paired exactly once per condition: {task_id}")
    labels = manifest["judge_labels"]
    if set(labels) != set(tasks):
        raise ValueError("judge label map differs from the frozen task IDs")
    for task_id, mapping in labels.items():
        if set(mapping) != {"A", "B"} or set(mapping.values()) != set(conditions):
            raise ValueError(f"invalid blind labels for task: {task_id}")
    return schedule, tasks


def main() -> int:
    arguments = _arguments()
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schedule, tasks = _validate_manifest(manifest)
    experiment = arguments.experiment_directory.resolve()
    baseline_version = arguments.baseline_version.resolve(strict=True)
    baseline_source = (baseline_version / "source").resolve(strict=True)
    agent = arguments.agent_executable.resolve(strict=True)
    godot = arguments.godot_executable.resolve(strict=True)
    probe = PROBE.resolve(strict=True)
    version = json.loads((baseline_version / "VERSION.json").read_text(encoding="utf-8"))
    protocol = {
        "schema_version": 1,
        "design": manifest["design"],
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "shared_runner_dependency": str(DEPENDENCY_RUNNER.resolve(strict=True)),
        "shared_runner_dependency_sha256": _sha256_file(DEPENDENCY_RUNNER.resolve(strict=True)),
        "probe": str(probe),
        "probe_sha256": _sha256_file(probe),
        "baseline_version": baseline_version.name,
        "baseline_legacy_version": version["version"],
        "baseline_runtime_tree_sha256": version["runtime_tree_sha256"],
        "baseline_runtime_file_count": version["runtime_file_count"],
        "baseline_source": str(baseline_source),
        "agent_executable": str(agent),
        "agent_executable_sha256": _sha256_file(agent),
        "godot_executable": str(godot),
        "godot_executable_sha256": _sha256_file(godot),
        "model": manifest["execution"]["model"],
        "reasoning_effort": manifest["execution"]["reasoning_effort"],
        "solver_timeout_seconds": manifest["execution"]["solver_timeout_seconds"],
        "parallelism": manifest["execution"]["parallelism"],
        "conditions": {
            "baseline-minimal-open": {
                "prompt": "minimal-open free-work prompt",
                "sandbox": "workspace-write",
                "ephemeral": True,
                "godot": "per-attempt self-contained app clone through baseline process mutex",
            },
            "official-local": {
                "prompt": "public GameDevBench Codex prompt suffix",
                "sandbox": "danger-full-access",
                "ephemeral": False,
                "godot": "same pinned source executable directly on PATH",
            },
        },
        "schedule": schedule,
    }
    experiment.mkdir(parents=True, exist_ok=True)
    _write_json(experiment / "protocol-freeze.json", protocol)
    _progress(experiment, len(schedule))
    if arguments.prepare_only:
        print(experiment / "protocol-freeze.json")
        return 0

    completed_ordinals = {
        int(json.loads(path.read_text(encoding="utf-8"))["ordinal"])
        for path in (experiment / "receipts").glob("*.json")
    }
    pending = [item for item in schedule if int(item["ordinal"]) not in completed_ordinals]
    if arguments.max_new_attempts is not None:
        if arguments.max_new_attempts < 0:
            raise ValueError("max-new-attempts must be nonnegative")
        pending = pending[: arguments.max_new_attempts]
    parallelism = int(manifest["execution"]["parallelism"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {
            executor.submit(
                _run_attempt,
                experiment=experiment,
                attempt=attempt,
                task=tasks[str(attempt["task_id"])],
                model=str(manifest["execution"]["model"]),
                effort=str(manifest["execution"]["reasoning_effort"]),
                timeout_seconds=int(manifest["execution"]["solver_timeout_seconds"]),
                agent=agent,
                godot_source=godot,
                baseline_source=baseline_source,
            ): attempt
            for attempt in pending
        }
        for future in concurrent.futures.as_completed(futures):
            attempt = futures[future]
            ordinal = int(attempt["ordinal"])
            receipt_path = experiment / "receipts" / f"{ordinal:02d}.json"
            try:
                receipt = future.result()
            except Exception as error:
                receipt = {
                    "schema_version": 1,
                    "ordinal": ordinal,
                    "attempt_id": (
                        f"{ordinal:02d}-{attempt['task_id']}-{attempt['condition']}"
                    ),
                    "task_id": attempt["task_id"],
                    "condition": attempt["condition"],
                    "infrastructure_error": f"{type(error).__name__}: {error}",
                }
            _write_json(receipt_path, receipt)
            _progress(experiment, len(schedule))
            status = receipt.get("evaluation", {}).get("hard_gate", "INFRASTRUCTURE_ERROR")
            print(
                f"[{ordinal:02d}/{len(schedule):02d}] {attempt['task_id']} "
                f"{attempt['condition']} -> {status}",
                flush=True,
            )
    receipt_count = len(tuple((experiment / "receipts").glob("*.json")))
    if receipt_count == len(schedule):
        _write_json(
            experiment / "complete.json",
            {
                "schema_version": 1,
                "attempts_completed": receipt_count,
                "completed_at": datetime.now(UTC).isoformat(),
                "manifest_sha256": protocol["manifest_sha256"],
                "baseline_runtime_tree_sha256": protocol["baseline_runtime_tree_sha256"],
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
