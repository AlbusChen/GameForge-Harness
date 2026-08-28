#!/usr/bin/env python3
"""Run the frozen full qualified GameDevBench candidate condition."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.run_fresh50_candidate_iteration import _capture_quota_with_retry
from experiments.run_fresh50_official_v4 import _deterministic_harness_block
from experiments.run_unseen30_candidate_v2 import _ensure_benchmark_sources
from experiments.run_unseen30_maturity import (
    _harness_attempt,
    _replace_json,
    _runtime_digest,
    _sha256,
    _sha256_file,
    _weekly_used_percent,
    _write_json,
)
from gameforge.harness.game_tasks import RuntimeProtocol

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PROTOCOL = "programmable-v1"
INFRASTRUCTURE_RETRY_DELAY_SECONDS = 5.0


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
        default=ROOT / "runs/experiments/gamedevbench-full-qualified332-provenance-v19-run1",
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
    parser.add_argument(
        "--runtime-source",
        type=Path,
        default=ROOT,
        help="Complete Harness runtime source tree to execute and hash.",
    )
    parser.add_argument(
        "--python-executable",
        type=Path,
        default=ROOT / ".venv" / "bin" / "python",
        help="Pinned Python environment used to execute the selected runtime source.",
    )
    parser.add_argument(
        "--max-new-attempts",
        type=int,
        help="Run at most this many not-yet-completed attempts, then exit cleanly for rotation.",
    )
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _schedule(
    tasks: list[dict[str, Any]], condition: str, repetitions: int = 1
) -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    for repetition in range(1, repetitions + 1):
        for manifest_order, task in enumerate(tasks, start=1):
            ordinal = len(attempts) + 1
            suffix = f"-rep{repetition}" if repetitions > 1 else ""
            attempts.append(
                {
                    "ordinal": ordinal,
                    "attempt_id": f"full332-{task['task_id']}-{condition}{suffix}",
                    "task_id": task["task_id"],
                    "task_name": task["name"],
                    "stratum": task["stratum"],
                    "manifest_order": manifest_order,
                    "repetition": repetition,
                    "condition": condition,
                }
            )
    return {
        "schema_version": 1,
        "algorithm": "manifest task order, complete repetitions in repetition-major order",
        "condition": condition,
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def _task_manifests(experiment: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for task in manifest["tasks"]:
        task_id = str(task["task_id"])
        path = experiment / "control-manifests" / f"{task_id}.json"
        _write_json(
            path,
            {
                "schema_version": 1,
                "id": f"gamedevbench-full-qualified-{task_id}",
                "benchmark_commit": manifest["benchmark_commit"],
                "tasks": [task],
            },
        )
        paths[task_id] = path
    return paths


def _receipt_path(experiment: Path, ordinal: int) -> Path:
    return experiment / "receipts" / f"{ordinal:03d}.json"


def _outer_timeout_record(
    experiment: Path, attempt: dict[str, Any], error: BaseException
) -> tuple[dict[str, Any], int, Path, list[dict[str, object]]]:
    source = experiment / "attempts" / str(attempt["attempt_id"]) / "runner.log"
    snapshot = source.with_name("runner-timeout-snapshot.log")
    if not snapshot.exists():
        snapshot.write_bytes(source.read_bytes())
    return (
        {
            "status": "BLOCKED",
            "task_id": attempt["task_id"],
            "stratum": attempt["stratum"],
            "failure_category": "outer_attempt_timeout",
            "failure_detail": str(error),
            "duration_seconds": 2100.0,
            "model_turns": None,
            "tool_calls": None,
            "policy_violations": None,
            "lineage_complete": None,
            "official_gate": "NOT_RUN",
            "preservation_gate": "NOT_RUN",
            "required_evidence_coverage": None,
        },
        124,
        snapshot,
        [],
    )


def _update_progress(experiment: Path, total: int) -> None:
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    statuses = Counter(str(row["normalized_status"]) for row in rows)
    completed_ordinals = {int(row["ordinal"]) for row in rows}
    next_ordinal = next(
        (ordinal for ordinal in range(1, total + 1) if ordinal not in completed_ordinals),
        None,
    )
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "attempts_completed": len(rows),
            "attempts_total": total,
            "next_ordinal": next_ordinal,
            "status_counts": dict(sorted(statuses.items())),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _zero_turn_infrastructure(record: dict[str, Any]) -> bool:
    return (
        record.get("status") == "INFRASTRUCTURE_ERROR"
        and int(record.get("model_turns", 0)) == 0
        and not _deterministic_harness_block(record)
    )


def _run_attempt(
    *,
    uv: str,
    benchmark_root: Path,
    experiment: Path,
    manifest_path: Path,
    agent: Path,
    attempt: dict[str, Any],
    model: str,
    effort: str,
    runtime_protocol: str,
    runtime_source: Path,
    python_executable: Path,
) -> tuple[dict[str, Any], int, Path, list[dict[str, object]]]:
    execution_attempt = {**attempt, "condition": runtime_protocol}
    record, exit_code, source = _harness_attempt(
        uv=uv,
        root=ROOT,
        benchmark_root=benchmark_root,
        experiment=experiment,
        manifest_path=manifest_path,
        agent_executable=agent,
        attempt=execution_attempt,
        model=model,
        effort=effort,
        runtime_source=runtime_source,
        python_executable=python_executable,
    )
    retries: list[dict[str, object]] = []
    if _zero_turn_infrastructure(record):
        retries.append(
            {
                "attempt_id": execution_attempt["attempt_id"],
                "status": record.get("status"),
                "failure_detail": record.get("failure_detail"),
                "source": str(source),
                "source_sha256": _sha256_file(source),
            }
        )
        retry_attempt = {
            **execution_attempt,
            "attempt_id": f"{execution_attempt['attempt_id']}-infra-retry1",
        }
        time.sleep(INFRASTRUCTURE_RETRY_DELAY_SECONDS)
        record, exit_code, source = _harness_attempt(
            uv=uv,
            root=ROOT,
            benchmark_root=benchmark_root,
            experiment=experiment,
            manifest_path=manifest_path,
            agent_executable=agent,
            attempt=retry_attempt,
            model=model,
            effort=effort,
            runtime_source=runtime_source,
            python_executable=python_executable,
        )
    return record, exit_code, source, retries


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    experiment = arguments.experiment_directory.resolve()
    agent = arguments.agent_executable.resolve(strict=True)
    runtime_source = arguments.runtime_source.resolve(strict=True)
    python_executable = arguments.python_executable.absolute()
    if not python_executable.is_file():
        raise RuntimeError(f"Python executable is unavailable: {python_executable}")
    runtime_cli = runtime_source / "gameforge" / "cli.py"
    if not runtime_cli.is_file():
        raise RuntimeError(f"runtime source has no Gameforge CLI: {runtime_cli}")
    godot_bin = arguments.godot_bin_directory.resolve(strict=True)
    expected_godot = (godot_bin / "godot").resolve(strict=True)
    os.environ["PATH"] = f"{godot_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    discovered_godot = Path(shutil.which("godot") or "").resolve(strict=True)
    if discovered_godot != expected_godot:
        raise RuntimeError(f"wrong Godot executable on PATH: {discovered_godot}")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > 332:
        raise RuntimeError("candidate manifest must contain between 1 and 332 tasks")
    task_ids = [str(task.get("task_id", "")) for task in tasks if isinstance(task, dict)]
    if len(task_ids) != len(tasks) or len(set(task_ids)) != len(tasks):
        raise RuntimeError("candidate manifest tasks must have unique task IDs")
    condition = str(manifest["experiment"]["condition"])
    model = str(manifest["experiment"]["model"])
    effort = str(manifest["experiment"]["reasoning_effort"])
    runtime_protocol = str(
        manifest["experiment"].get("runtime_protocol", RUNTIME_PROTOCOL)
    )
    RuntimeProtocol(runtime_protocol)
    parallelism = int(manifest["experiment"].get("parallelism", 1))
    if not 1 <= parallelism <= 8:
        raise RuntimeError("full candidate parallelism must be between 1 and 8")
    repetitions = int(manifest["experiment"].get("repetitions_per_task", 1))
    if not 1 <= repetitions <= 10:
        raise RuntimeError("candidate repetitions per task must be between 1 and 10")
    expected_task_count = int(manifest["experiment"].get("task_count", len(tasks)))
    if expected_task_count != len(tasks):
        raise RuntimeError("candidate manifest task count does not match its experiment")
    quota_stop_remaining_percent = float(
        manifest["experiment"].get("quota_stop_remaining_percent", 50)
    )
    if not 0 <= quota_stop_remaining_percent <= 100:
        raise RuntimeError("quota stop remaining percent must be between 0 and 100")
    runtime_digest, runtime_count = _runtime_digest(runtime_source)
    agent_digest = _sha256_file(agent)
    expected_agent_digest = str(manifest["experiment"].get("agent_executable_sha256", ""))
    if expected_agent_digest and agent_digest != expected_agent_digest:
        raise RuntimeError(
            "Codex executable differs from the preregistered condition: "
            f"expected {expected_agent_digest}, got {agent_digest}"
        )
    frozen = manifest["code_freeze"]
    if (
        runtime_digest != frozen["runtime_tree_sha256"]
        or runtime_count != frozen["runtime_file_count"]
    ):
        raise RuntimeError("Harness runtime differs from the full-benchmark freeze")

    schedule = _schedule(tasks, condition, repetitions)
    attempt_count = len(schedule["attempts"])
    experiment.mkdir(parents=True, exist_ok=True)
    schedule_path = experiment / "schedule.json"
    _write_json(schedule_path, schedule)
    task_manifests = _task_manifests(experiment, manifest)
    _ensure_benchmark_sources(benchmark_root, [str(task["task_id"]) for task in tasks])
    protocol = {
        "schema_version": 1,
        "design": "frozen GameDevBench candidate run over a preregistered manifest",
        "condition": condition,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_bytes),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "schedule_sha256": _sha256_file(schedule_path),
        "benchmark_root": str(benchmark_root),
        "benchmark_commit": manifest["benchmark_commit"],
        "runtime_tree_sha256": runtime_digest,
        "runtime_file_count": runtime_count,
        "runtime_source": str(runtime_source),
        "python_executable": str(python_executable),
        "python_executable_sha256": _sha256_file(python_executable),
        "agent_executable": str(agent),
        "agent_executable_sha256": agent_digest,
        "godot_executable": str(expected_godot),
        "godot_executable_sha256": _sha256_file(expected_godot),
        "godot_version": manifest["qualification"]["godot_version"],
        "model": model,
        "reasoning_effort": effort,
        "runtime_protocol": runtime_protocol,
        "tasks": len(tasks),
        "repetitions_per_task": repetitions,
        "attempts": attempt_count,
        "parallelism": parallelism,
        "quota_stop_remaining_percent": quota_stop_remaining_percent,
        "official_reference": manifest["official_reference"],
        "interpretation": (
            "This is a complete benchmark run of every preregistered eligible task. The "
            "published Official comparison is aggregate, not paired, because its public "
            "leaderboard artifact does not contain per-task outcomes."
        ),
    }
    protocol_path = experiment / "protocol-freeze.json"
    if protocol_path.exists():
        frozen_protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        comparable_protocol = {**protocol, "runner_sha256": frozen_protocol["runner_sha256"]}
        if frozen_protocol != comparable_protocol:
            raise RuntimeError("full-benchmark protocol differs from frozen protocol")
        if frozen_protocol["runner_sha256"] != protocol["runner_sha256"]:
            _replace_json(
                experiment / "runner-resume-hotfix.json",
                {
                    "schema_version": 1,
                    "scope": "orchestration-only outer-timeout continuation",
                    "frozen_runner_sha256": frozen_protocol["runner_sha256"],
                    "resume_runner_sha256": protocol["runner_sha256"],
                    "model_runtime_changed": False,
                },
            )
    else:
        _write_json(protocol_path, protocol)
    _update_progress(experiment, attempt_count)
    print(
        f"Prepared {attempt_count} candidate attempts over {len(tasks)} tasks; "
        f"condition={condition}; "
        f"runtime sha256={runtime_digest}",
        flush=True,
    )
    if arguments.prepare_only:
        return 0
    if arguments.max_new_attempts is not None and arguments.max_new_attempts <= 0:
        raise RuntimeError("max-new-attempts must be positive")

    pending = [
        attempt
        for attempt in schedule["attempts"]
        if not _receipt_path(experiment, int(attempt["ordinal"])).exists()
    ]
    if arguments.max_new_attempts is not None:
        pending = pending[: arguments.max_new_attempts]
    next_pending = 0
    stopped_for_quota = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as executor:
        inflight: dict[
            concurrent.futures.Future[
                tuple[dict[str, Any], int, Path, list[dict[str, object]]]
            ],
            tuple[dict[str, Any], Path, float],
        ] = {}
        while next_pending < len(pending) or inflight:
            while (
                not stopped_for_quota
                and next_pending < len(pending)
                and len(inflight) < parallelism
            ):
                attempt = pending[next_pending]
                next_pending += 1
                ordinal = int(attempt["ordinal"])
                current_digest, current_count = _runtime_digest(runtime_source)
                if (current_digest, current_count) != (runtime_digest, runtime_count):
                    raise RuntimeError("Harness runtime changed after full-benchmark start")
                if _sha256_file(agent) != agent_digest:
                    raise RuntimeError("Codex executable changed after experiment start")
                quota_path = experiment / "quota" / f"{ordinal:03d}-before.json"
                quota = _capture_quota_with_retry(agent, ordinal=ordinal)
                _replace_json(quota_path, quota)
                used = _weekly_used_percent(quota)
                if used is None:
                    raise RuntimeError(f"weekly quota unavailable before attempt {ordinal}")
                remaining = 100.0 - used
                if remaining < quota_stop_remaining_percent:
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
                    print(
                        f"Quota stop before attempt {ordinal}: {remaining:.1f}% remaining"
                    )
                    stopped_for_quota = True
                    break
                print(
                    f"[{ordinal:03d}/{attempt_count:03d}] {attempt['task_id']} {condition} "
                    f"({remaining:.1f}% quota remaining)",
                    flush=True,
                )
                future = executor.submit(
                    _run_attempt,
                    uv=uv,
                    benchmark_root=benchmark_root,
                    experiment=experiment,
                    manifest_path=task_manifests[str(attempt["task_id"])],
                    agent=agent,
                    attempt=attempt,
                    model=model,
                    effort=effort,
                    runtime_protocol=runtime_protocol,
                    runtime_source=runtime_source,
                    python_executable=python_executable,
                )
                inflight[future] = (attempt, quota_path, used)
            if not inflight:
                break
            done, _ = concurrent.futures.wait(
                inflight,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in sorted(done, key=lambda item: int(inflight[item][0]["ordinal"])):
                attempt, quota_path, used = inflight.pop(future)
                try:
                    record, exit_code, source, retries = future.result()
                except RuntimeError as error:
                    if "attempt exceeded outer timeout:" not in str(error):
                        raise
                    record, exit_code, source, retries = _outer_timeout_record(
                        experiment, attempt, error
                    )
                status = str(record.get("status", "ERROR"))
                if _deterministic_harness_block(record):
                    normalized = "BLOCKED"
                elif status in {"PASS", "FAIL", "BLOCKED"}:
                    normalized = status
                elif _zero_turn_infrastructure(record):
                    normalized = "ERROR"
                else:
                    raise RuntimeError(f"unclassified result retained at {source}: {status}")
                _write_json(
                    _receipt_path(experiment, int(attempt["ordinal"])),
                    {
                        **attempt,
                        "schema_version": 1,
                        "runtime_protocol": runtime_protocol,
                        "normalized_status": normalized,
                        "runner_exit_code": exit_code,
                        "result_source": str(source),
                        "result_source_sha256": _sha256_file(source),
                        "quota_snapshot": str(quota_path),
                        "weekly_used_percent_before": used,
                        "infrastructure_retries": retries,
                        "record": record,
                    },
                )
                _update_progress(experiment, attempt_count)
                print(
                    f"[{int(attempt['ordinal']):03d}] {attempt['task_id']} -> {normalized}",
                    flush=True,
                )
    if stopped_for_quota:
        return 0
    completed_attempts = len(tuple((experiment / "receipts").glob("*.json")))
    if completed_attempts < attempt_count:
        print(
            f"Rotation pause: {completed_attempts}/{attempt_count} attempts completed.",
            flush=True,
        )
        return 0

    _write_json(
        experiment / "complete.json",
        {
            "schema_version": 1,
            "attempts_completed": attempt_count,
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_tree_sha256": runtime_digest,
            "schedule_sha256": _sha256_file(schedule_path),
        },
    )
    print(f"All {attempt_count} candidate attempts completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
