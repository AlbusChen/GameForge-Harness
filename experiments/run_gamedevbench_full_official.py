#!/usr/bin/env python3
"""Run Official Codex on the complete locally qualified GameDevBench set."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from experiments.run_fresh50_candidate_iteration import _capture_quota_with_retry
from experiments.run_unseen30_candidate_v2 import _ensure_benchmark_sources
from experiments.run_unseen30_maturity import (
    _replace_json,
    _sha256_file,
    _weekly_used_percent,
    _write_json,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_NAME = "gamedevbench-full-qualified332-official-local-run1"
EXPECTED_TASKS = 332


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
        default=ROOT / "benchmarks/gamedevbench-full-qualified332-v1.json",
    )
    parser.add_argument(
        "--experiment-directory",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-official-run1",
    )
    parser.add_argument(
        "--agent-executable",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument(
        "--godot-bin-directory",
        type=Path,
        default=Path("/private/tmp/godot-4.4.1-bin"),
    )
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    experiment = arguments.experiment_directory.resolve()
    agent = arguments.agent_executable.resolve(strict=True)
    godot_bin = arguments.godot_bin_directory.resolve(strict=True)
    if not 1 <= arguments.parallel <= 8:
        raise ValueError("parallelism must be between one and eight")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != EXPECTED_TASKS:
        raise RuntimeError(f"qualified manifest must contain {EXPECTED_TASKS} tasks")
    task_ids = [str(task["task_id"]) for task in tasks]
    _ensure_benchmark_sources(benchmark_root, task_ids)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=benchmark_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != manifest["benchmark_commit"]:
        raise RuntimeError(f"benchmark checkout is not pinned: {head}")

    experiment.mkdir(parents=True, exist_ok=True)
    task_list = experiment / "task-list.yaml"
    task_list_payload = {"tasks": task_ids}
    encoded = yaml.safe_dump(task_list_payload, sort_keys=False)
    if task_list.exists() and task_list.read_text(encoding="utf-8") != encoded:
        raise RuntimeError("Official task list differs from frozen task list")
    task_list.write_text(encoded, encoding="utf-8")

    expected_result = benchmark_root / "results" / RUN_NAME / "final_results.json"
    command = [
        uv,
        "run",
        "python",
        "gamedevbench/src/benchmark_runner.py",
        "--agent",
        "codex",
        "--model",
        str(manifest["experiment"]["model"]),
        "--effort",
        str(manifest["experiment"]["reasoning_effort"]),
        "--run-name",
        RUN_NAME,
        "--parallel",
        str(arguments.parallel),
        "--solver-timeout",
        "900",
    ]
    protocol_command = [*command, "run", "--task-list", str(task_list)]
    progress = benchmark_root / "results" / RUN_NAME / "progress_codex_gpt-5.6-sol.json"
    completion_marker = experiment / "complete.json"
    if progress.is_file() and not completion_marker.is_file():
        command.append("--resume")
    command.extend(["run", "--task-list", str(task_list)])
    protocol = {
        "schema_version": 1,
        "design": "complete locally qualified Official Codex comparator",
        "benchmark_commit": head,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "task_list": str(task_list),
        "task_list_sha256": _sha256_file(task_list),
        "excluded_tasks": manifest["qualification"]["excluded_tasks"],
        "agent": "codex",
        "model": manifest["experiment"]["model"],
        "reasoning_effort": manifest["experiment"]["reasoning_effort"],
        "parallel": arguments.parallel,
        "solver_timeout_seconds": 900,
        "agent_executable": str(agent),
        "agent_executable_sha256": _sha256_file(agent),
        "godot_bin_directory": str(godot_bin),
        "run_name": RUN_NAME,
        "command": protocol_command,
        "interpretation": (
            "This local Official run provides per-task paired outcomes. The frozen public "
            "leaderboard remains the primary external aggregate reference."
        ),
    }
    _write_json(experiment / "protocol-freeze.json", protocol)
    if arguments.prepare_only:
        print(experiment / "protocol-freeze.json")
        return 0

    before = _capture_quota_with_retry(agent, ordinal=1)
    _replace_json(experiment / "quota-before.json", before)
    used = _weekly_used_percent(before)
    if used is None or 100.0 - used < 50.0:
        raise RuntimeError("at least 50% weekly quota must remain before the Official full run")

    log_path = experiment / "runner.log"
    environment = os.environ.copy()
    environment["PATH"] = f"{godot_bin}{os.pathsep}{environment.get('PATH', '')}"
    started = time.monotonic()
    if not completion_marker.is_file():
        with log_path.open("a", encoding="utf-8") as log:
            log.write("COMMAND " + json.dumps(command) + "\n")
            log.flush()
            completed = subprocess.run(
                command,
                cwd=benchmark_root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            log.write(f"\nEXIT {completed.returncode}\n")
        if completed.returncode != 0:
            raise RuntimeError(f"Official runner failed; inspect {log_path}")
    elapsed = round(time.monotonic() - started, 3)
    payload = json.loads(expected_result.read_text(encoding="utf-8"))
    records = payload.get("tasks")
    if not isinstance(records, list) or len(records) != EXPECTED_TASKS:
        raise RuntimeError("Official full result is incomplete")
    observed = {str(record.get("task_name")) for record in records}
    if observed != set(task_ids):
        raise RuntimeError("Official result task identities differ from the frozen manifest")
    if payload.get("rate_limited") or payload.get("incomplete"):
        raise RuntimeError("Official result stopped before benchmark completion")

    after = _capture_quota_with_retry(agent, ordinal=2)
    _replace_json(experiment / "quota-after.json", after)
    _write_json(
        experiment / "complete.json",
        {
            "schema_version": 1,
            "completed_at": datetime.now(UTC).isoformat(),
            "attempts_completed": len(records),
            "passes": sum(bool(record.get("success")) for record in records),
            "result_source": str(expected_result),
            "result_source_sha256": _sha256_file(expected_result),
            "wall_seconds_this_invocation": elapsed,
        },
    )
    print(expected_result)
    print(f"Official pass count: {sum(bool(record.get('success')) for record in records)}/332")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
