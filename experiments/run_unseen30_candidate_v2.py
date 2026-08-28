#!/usr/bin/env python3
"""Run only the revised Programmable Harness on the frozen unseen-30 schedule.

Official Default and Legacy results are immutable controls from the maturity-v1
experiment. This coordinator preserves the same task/repetition order for the
Programmable condition, freezes the revised runtime, checks quota before every
attempt, and writes resumable per-attempt receipts.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.run_unseen30_maturity import (
    _ensure_task_sources,
    _harness_attempt,
    _replace_json,
    _runtime_digest,
    _sha256,
    _sha256_file,
    _single_task_manifests,
    _weekly_used_percent,
    _write_json,
)
from gameforge.telemetry.subscription import capture_subscription_snapshot

CANDIDATE_CONDITION = "programmable-open-scope-v2"
RUNTIME_PROTOCOL = "programmable-v1"


def _arguments() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("/private/tmp/gamedevbench-e3868"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "benchmarks/gamedevbench-unseen30-maturity-v1.json",
    )
    parser.add_argument(
        "--baseline-experiment",
        type=Path,
        default=root / "runs/experiments/gamedevbench-unseen30-maturity-v1",
    )
    parser.add_argument(
        "--experiment-directory",
        type=Path,
        default=root / "runs/experiments/gamedevbench-unseen30-open-scope-v2",
    )
    parser.add_argument(
        "--agent-executable",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _candidate_schedule(baseline_schedule: dict[str, Any]) -> dict[str, object]:
    source_attempts = [
        attempt
        for attempt in baseline_schedule["attempts"]
        if attempt["condition"] == RUNTIME_PROTOCOL
    ]
    attempts: list[dict[str, object]] = []
    for ordinal, source in enumerate(source_attempts, start=1):
        attempts.append(
            {
                "ordinal": ordinal,
                "source_schedule_ordinal": source["ordinal"],
                "source_attempt_id": source["attempt_id"],
                "attempt_id": (
                    f"r{int(source['repetition']):02d}-{source['task_id']}-"
                    f"{CANDIDATE_CONDITION}"
                ),
                "repetition": source["repetition"],
                "task_id": source["task_id"],
                "task_name": source["task_name"],
                "stratum": source["stratum"],
                "manifest_order": source["manifest_order"],
                "condition": CANDIDATE_CONDITION,
                "runtime_protocol": RUNTIME_PROTOCOL,
                "within_task_position": source["within_task_position"],
            }
        )
    if len(attempts) != 90:
        raise RuntimeError("frozen baseline schedule must contain 90 Programmable attempts")
    repetitions = Counter((item["task_id"], item["repetition"]) for item in attempts)
    if len(repetitions) != 90 or any(count != 1 for count in repetitions.values()):
        raise RuntimeError("candidate schedule does not preserve one attempt per task/repetition")
    return {
        "schema_version": 1,
        "algorithm": (
            "filter programmable-v1 attempts from frozen maturity-v1 schedule; preserve source "
            "order, repetition, task, stratum, and within-task position"
        ),
        "condition": CANDIDATE_CONDITION,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "repetitions": 3,
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def _receipt_path(experiment: Path, attempt: dict[str, Any]) -> Path:
    return experiment / "receipts" / f"{int(attempt['ordinal']):03d}.json"


def _ensure_benchmark_sources(benchmark_root: Path, task_ids: list[str]) -> None:
    archives_available = all(
        (benchmark_root / collection / f"{task_id}.zip").is_file()
        for task_id in task_ids
        for collection in ("tasks", "tasks_gt")
    )
    if archives_available:
        _ensure_task_sources(benchmark_root, task_ids)
        return
    for task_id in task_ids:
        public = benchmark_root / "tasks" / task_id / "project.godot"
        ground_truth = benchmark_root / "tasks_gt" / task_id
        if not public.is_file() or not ground_truth.is_dir():
            raise RuntimeError(
                f"benchmark source is neither archived nor fully materialized: {task_id}"
            )


def _update_progress(experiment: Path, schedule: dict[str, Any]) -> None:
    receipts = []
    for attempt in schedule["attempts"]:
        path = _receipt_path(experiment, attempt)
        if path.exists():
            receipts.append(json.loads(path.read_text(encoding="utf-8")))
    counts = Counter(str(receipt["normalized_status"]) for receipt in receipts)
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "condition": CANDIDATE_CONDITION,
            "attempts_completed": len(receipts),
            "attempts_total": len(schedule["attempts"]),
            "next_ordinal": len(receipts) + 1,
            "status_counts": dict(sorted(counts.items())),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    root = Path(__file__).resolve().parents[1]
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    baseline = arguments.baseline_experiment.resolve(strict=True)
    experiment = arguments.experiment_directory.resolve()
    agent = arguments.agent_executable.resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 30:
        raise RuntimeError("the frozen manifest must contain exactly 30 tasks")
    if arguments.model != manifest["experiment"]["model"]:
        raise RuntimeError("model differs from the frozen comparison protocol")
    if arguments.reasoning_effort != manifest["experiment"]["reasoning_effort"]:
        raise RuntimeError("reasoning effort differs from the frozen comparison protocol")

    baseline_schedule_path = baseline / "schedule.json"
    baseline_protocol_path = baseline / "protocol-freeze.json"
    baseline_analysis_path = baseline / "analysis.json"
    baseline_schedule = json.loads(baseline_schedule_path.read_text(encoding="utf-8"))
    schedule = _candidate_schedule(baseline_schedule)
    runtime_digest, runtime_count = _runtime_digest(root)

    experiment.mkdir(parents=True, exist_ok=True)
    schedule_path = experiment / "schedule.json"
    _write_json(schedule_path, schedule)
    task_manifests = _single_task_manifests(experiment, manifest)
    _ensure_benchmark_sources(benchmark_root, [str(item["task_id"]) for item in tasks])
    protocol = {
        "schema_version": 1,
        "design": "candidate-only rerun against immutable maturity-v1 controls",
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_bytes),
        "schedule_sha256": _sha256_file(schedule_path),
        "source_schedule_path": str(baseline_schedule_path),
        "source_schedule_sha256": _sha256_file(baseline_schedule_path),
        "frozen_baseline_protocol_path": str(baseline_protocol_path),
        "frozen_baseline_protocol_sha256": _sha256_file(baseline_protocol_path),
        "frozen_baseline_analysis_path": str(baseline_analysis_path),
        "frozen_baseline_analysis_sha256": _sha256_file(baseline_analysis_path),
        "benchmark_root": str(benchmark_root),
        "benchmark_commit": manifest["benchmark_commit"],
        "runtime_tree_sha256": runtime_digest,
        "runtime_file_count": runtime_count,
        "agent_executable": str(agent),
        "agent_executable_sha256": _sha256_file(agent),
        "model": arguments.model,
        "reasoning_effort": arguments.reasoning_effort,
        "condition": CANDIDATE_CONDITION,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "attempts": len(schedule["attempts"]),
        "quota_stop_remaining_percent": 50,
    }
    _write_json(experiment / "protocol-freeze.json", protocol)
    _update_progress(experiment, schedule)
    print(
        f"Prepared 30 tasks and {len(schedule['attempts'])} candidate attempts; "
        f"runtime sha256={runtime_digest}",
        flush=True,
    )
    if arguments.prepare_only:
        return 0

    for attempt in schedule["attempts"]:
        receipt_path = _receipt_path(experiment, attempt)
        if receipt_path.exists():
            continue
        current_digest, current_count = _runtime_digest(root)
        if current_digest != runtime_digest or current_count != runtime_count:
            raise RuntimeError("Harness runtime changed after experiment start")

        ordinal = int(attempt["ordinal"])
        quota_path = experiment / "quota" / f"{ordinal:03d}-before.json"
        quota = capture_subscription_snapshot(agent)
        _replace_json(quota_path, quota)
        used_percent = _weekly_used_percent(quota)
        if used_percent is None:
            raise RuntimeError(f"weekly quota is unavailable before attempt {ordinal}")
        remaining = 100.0 - used_percent
        if remaining < 50.0:
            _write_json(
                experiment / "quota-stop.json",
                {
                    "schema_version": 1,
                    "stopped_before_ordinal": ordinal,
                    "weekly_used_percent": used_percent,
                    "weekly_remaining_percent": remaining,
                    "quota_snapshot": str(quota_path),
                },
            )
            print(f"Quota stop before attempt {ordinal}: {remaining:.1f}% remaining", flush=True)
            return 0

        print(
            f"[{ordinal:03d}/090] r{attempt['repetition']} {attempt['task_id']} "
            f"{CANDIDATE_CONDITION} ({remaining:.1f}% quota remaining)",
            flush=True,
        )
        execution_attempt = {**attempt, "condition": RUNTIME_PROTOCOL}
        record, exit_code, source = _harness_attempt(
            uv=uv,
            root=root,
            benchmark_root=benchmark_root,
            experiment=experiment,
            manifest_path=task_manifests[str(attempt["task_id"])],
            agent_executable=agent,
            attempt=execution_attempt,
            model=arguments.model,
            effort=arguments.reasoning_effort,
        )
        status_value = str(record.get("status", "ERROR"))
        normalized = (
            status_value if status_value in {"PASS", "FAIL", "BLOCKED"} else "ERROR"
        )
        if record.get("status") == "INFRASTRUCTURE_ERROR" or normalized == "ERROR":
            raise RuntimeError(f"Harness infrastructure/error result retained at {source}")
        _write_json(
            receipt_path,
            {
                **attempt,
                "schema_version": 1,
                "normalized_status": normalized,
                "runner_exit_code": exit_code,
                "result_source": str(source),
                "result_source_sha256": _sha256_file(source),
                "quota_snapshot": str(quota_path),
                "weekly_used_percent_before": used_percent,
                "record": record,
            },
        )
        _update_progress(experiment, schedule)
        print(f"  -> {normalized}", flush=True)

    _write_json(
        experiment / "complete.json",
        {
            "schema_version": 1,
            "attempts_completed": len(schedule["attempts"]),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_tree_sha256": runtime_digest,
            "schedule_sha256": _sha256_file(schedule_path),
        },
    )
    print("All 90 candidate attempts completed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
