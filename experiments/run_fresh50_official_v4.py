#!/usr/bin/env python3
"""Run the frozen paired Official Default versus v4 fresh-50 experiment."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.run_unseen30_candidate_v2 import _ensure_benchmark_sources
from experiments.run_unseen30_maturity import (
    _harness_attempt,
    _normalize_result,
    _official_attempt,
    _replace_json,
    _runtime_digest,
    _sha256,
    _sha256_file,
    _weekly_used_percent,
    _write_json,
)
from gameforge.telemetry.subscription import capture_subscription_snapshot

ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = ("official-default", "programmable-open-global-diagnostic-v4")
RUNTIME_PROTOCOL = "programmable-v1"


def _deterministic_harness_block(record: dict[str, Any]) -> bool:
    """Separate a reproducible Harness admission limit from transient infrastructure."""
    detail = str(record.get("failure_detail", ""))
    return (
        record.get("status") == "INFRASTRUCTURE_ERROR"
        and int(record.get("model_turns", 0)) == 0
        and detail.startswith("public task context is too large")
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("/private/tmp/gamedevbench-e3868-pinned"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/gamedevbench-fresh50-v1.json",
    )
    parser.add_argument(
        "--experiment-directory",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-fresh50-official-v4-v1",
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


def _schedule(tasks: list[dict[str, Any]]) -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    ordinal = 0
    for index, task in enumerate(tasks):
        order = CONDITIONS if index % 2 == 0 else tuple(reversed(CONDITIONS))
        for position, condition in enumerate(order, start=1):
            ordinal += 1
            attempts.append(
                {
                    "ordinal": ordinal,
                    "attempt_id": f"fresh50-{task['task_id']}-{condition}",
                    "task_id": task["task_id"],
                    "task_name": task["name"],
                    "stratum": task["stratum"],
                    "manifest_order": index + 1,
                    "condition": condition,
                    "within_task_position": position,
                }
            )
    return {
        "schema_version": 1,
        "algorithm": "adjacent paired tasks with alternating condition-first position",
        "conditions": list(CONDITIONS),
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def _task_manifests(
    experiment: Path, manifest: dict[str, Any]
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for task in manifest["tasks"]:
        task_id = str(task["task_id"])
        path = experiment / "control-manifests" / f"{task_id}.json"
        _write_json(
            path,
            {
                "schema_version": 1,
                "id": f"gamedevbench-fresh50-{task_id}-v1",
                "benchmark_commit": manifest["benchmark_commit"],
                "tasks": [task],
            },
        )
        paths[task_id] = path
    return paths


def _receipt_path(experiment: Path, attempt: dict[str, Any]) -> Path:
    return experiment / "receipts" / f"{int(attempt['ordinal']):03d}.json"


def _update_progress(experiment: Path, schedule: dict[str, Any]) -> None:
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for attempt in schedule["attempts"]
        if (path := _receipt_path(experiment, attempt)).exists()
    ]
    by_condition: dict[str, Counter[str]] = {item: Counter() for item in CONDITIONS}
    for row in rows:
        by_condition[str(row["condition"])][str(row["normalized_status"])] += 1
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "attempts_completed": len(rows),
            "attempts_total": len(schedule["attempts"]),
            "next_ordinal": len(rows) + 1,
            "condition_status_counts": {
                key: dict(sorted(value.items())) for key, value in by_condition.items()
            },
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    experiment = arguments.experiment_directory.resolve()
    agent = arguments.agent_executable.resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 50:
        raise RuntimeError("fresh50 manifest must contain exactly 50 tasks")
    if arguments.model != manifest["experiment"]["model"]:
        raise RuntimeError("model differs from frozen manifest")
    if arguments.reasoning_effort != manifest["experiment"]["reasoning_effort"]:
        raise RuntimeError("reasoning effort differs from frozen manifest")
    runtime_digest, runtime_count = _runtime_digest(ROOT)
    frozen = manifest["code_freeze"]
    if (
        runtime_digest != frozen["runtime_tree_sha256"]
        or runtime_count != frozen["runtime_file_count"]
    ):
        raise RuntimeError("Harness runtime differs from the fresh50 code freeze")

    schedule = _schedule(tasks)
    experiment.mkdir(parents=True, exist_ok=True)
    schedule_path = experiment / "schedule.json"
    _write_json(schedule_path, schedule)
    manifests = _task_manifests(experiment, manifest)
    _ensure_benchmark_sources(benchmark_root, [str(task["task_id"]) for task in tasks])
    _write_json(
        experiment / "protocol-freeze.json",
        {
            "schema_version": 1,
            "design": "fresh paired Official Default versus v4, one attempt per cell",
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_bytes),
            "schedule_sha256": _sha256_file(schedule_path),
            "benchmark_root": str(benchmark_root),
            "benchmark_commit": manifest["benchmark_commit"],
            "runtime_tree_sha256": runtime_digest,
            "runtime_file_count": runtime_count,
            "agent_executable": str(agent),
            "agent_executable_sha256": _sha256_file(agent),
            "model": arguments.model,
            "reasoning_effort": arguments.reasoning_effort,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "attempts": 100,
            "quota_stop_remaining_percent": 50,
        },
    )
    _update_progress(experiment, schedule)
    print(f"Prepared 50 tasks and 100 paired attempts; runtime sha256={runtime_digest}", flush=True)
    if arguments.prepare_only:
        return 0

    for attempt in schedule["attempts"]:
        receipt_path = _receipt_path(experiment, attempt)
        if receipt_path.exists():
            continue
        current_digest, current_count = _runtime_digest(ROOT)
        if current_digest != runtime_digest or current_count != runtime_count:
            raise RuntimeError("Harness runtime changed after fresh50 start")
        ordinal = int(attempt["ordinal"])
        quota_path = experiment / "quota" / f"{ordinal:03d}-before.json"
        quota = capture_subscription_snapshot(agent)
        _replace_json(quota_path, quota)
        used = _weekly_used_percent(quota)
        if used is None:
            raise RuntimeError(f"weekly quota unavailable before attempt {ordinal}")
        remaining = 100.0 - used
        if remaining < 50.0:
            _write_json(
                experiment / "quota-stop.json",
                {
                    "schema_version": 1,
                    "stopped_before_ordinal": ordinal,
                    "weekly_used_percent": used,
                    "weekly_remaining_percent": remaining,
                    "quota_snapshot": str(quota_path),
                },
            )
            print(f"Quota stop before attempt {ordinal}: {remaining:.1f}% remaining", flush=True)
            return 0

        condition = str(attempt["condition"])
        print(
            f"[{ordinal:03d}/100] {attempt['task_id']} {condition} "
            f"({remaining:.1f}% quota remaining)",
            flush=True,
        )
        if condition == "official-default":
            record, exit_code, source = _official_attempt(
                uv=uv,
                benchmark_root=benchmark_root,
                experiment=experiment,
                attempt=attempt,
                model=arguments.model,
                effort=arguments.reasoning_effort,
            )
            normalized = _normalize_result(condition, record)
        else:
            execution_attempt = {**attempt, "condition": RUNTIME_PROTOCOL}
            record, exit_code, source = _harness_attempt(
                uv=uv,
                root=ROOT,
                benchmark_root=benchmark_root,
                experiment=experiment,
                manifest_path=manifests[str(attempt["task_id"])],
                agent_executable=agent,
                attempt=execution_attempt,
                model=arguments.model,
                effort=arguments.reasoning_effort,
            )
            normalized = str(record.get("status", "ERROR"))
            if _deterministic_harness_block(record):
                normalized = "BLOCKED"
        if normalized not in {"PASS", "FAIL", "BLOCKED"}:
            raise RuntimeError(f"infrastructure/error result retained at {source}")
        _write_json(
            receipt_path,
            {
                **attempt,
                "schema_version": 1,
                "runtime_protocol": RUNTIME_PROTOCOL if condition != "official-default" else None,
                "normalized_status": normalized,
                "runner_exit_code": exit_code,
                "result_source": str(source),
                "result_source_sha256": _sha256_file(source),
                "quota_snapshot": str(quota_path),
                "weekly_used_percent_before": used,
                "record": record,
            },
        )
        _update_progress(experiment, schedule)
        print(f"  -> {normalized}", flush=True)

    _write_json(
        experiment / "complete.json",
        {
            "schema_version": 1,
            "attempts_completed": 100,
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_tree_sha256": runtime_digest,
            "schedule_sha256": _sha256_file(schedule_path),
        },
    )
    print("All 100 fresh50 attempts completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
