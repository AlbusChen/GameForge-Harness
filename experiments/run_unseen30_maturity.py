#!/usr/bin/env python3
"""Run the preregistered unseen-30 maturity experiment without tuning the Harness.

The coordinator materializes the frozen balanced schedule, checks the Harness runtime
digest and subscription quota before every model-backed attempt, and records one
immutable receipt per condition/task/repetition. It is safe to resume after interruption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gameforge.telemetry.subscription import capture_subscription_snapshot

CONDITIONS = ("official-default", "legacy-tool-v1", "programmable-v1")
RUNTIME_PATHS = ("gameforge", "pyproject.toml", "uv.lock")


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
        "--experiment-directory",
        type=Path,
        default=root / "runs/experiments/gamedevbench-unseen30-maturity-v1",
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


def _canonical(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256(path.read_bytes())


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical(payload)
    if path.exists():
        if path.read_bytes() != encoded:
            raise RuntimeError(f"refusing to change frozen artifact: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def _replace_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_canonical(payload))
    temporary.replace(path)


def _runtime_digest(root: Path) -> tuple[str, int]:
    files: list[Path] = []
    for relative in RUNTIME_PATHS:
        candidate = root / relative
        if candidate.is_file():
            files.append(candidate)
            continue
        files.extend(
            path
            for path in candidate.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        digest.update(f"{relative}\0{_sha256_file(path)}\n".encode())
    return digest.hexdigest(), len(files)


def _schedule(tasks: list[dict[str, Any]]) -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    ordinal = 0
    for repetition_index in range(3):
        offset = repetition_index * 10
        ordered = tasks[offset:] + tasks[:offset]
        for entry in ordered:
            manifest_index = tasks.index(entry)
            start = (manifest_index + repetition_index) % len(CONDITIONS)
            condition_order = CONDITIONS[start:] + CONDITIONS[:start]
            for position, condition in enumerate(condition_order, start=1):
                ordinal += 1
                attempts.append(
                    {
                        "ordinal": ordinal,
                        "attempt_id": (
                            f"r{repetition_index + 1:02d}-{entry['task_id']}-{condition}"
                        ),
                        "repetition": repetition_index + 1,
                        "task_id": entry["task_id"],
                        "task_name": entry["name"],
                        "stratum": entry["stratum"],
                        "manifest_order": manifest_index + 1,
                        "condition": condition,
                        "within_task_position": position,
                    }
                )
    return {
        "schema_version": 1,
        "algorithm": (
            "repeat task rotation 0/10/20; condition cyclic start "
            "(manifest_index + repetition_index) mod 3"
        ),
        "conditions": list(CONDITIONS),
        "repetitions": 3,
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def _archive_members(archive: Path, benchmark_root: Path) -> dict[Path, bytes]:
    members: dict[Path, bytes] = {}
    with zipfile.ZipFile(archive) as source:
        corrupt = source.testzip()
        if corrupt is not None:
            raise RuntimeError(f"corrupt archive member {corrupt!r} in {archive}")
        for info in source.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"archive symlink is not allowed: {info.filename}")
            destination = (benchmark_root / info.filename).resolve()
            if benchmark_root.resolve() not in destination.parents:
                raise RuntimeError(f"archive traversal is not allowed: {info.filename}")
            if not info.is_dir():
                members[destination] = source.read(info)
    return members


def _ensure_task_sources(benchmark_root: Path, task_ids: list[str]) -> None:
    for task_id in task_ids:
        expected: dict[Path, bytes] = {}
        for collection in ("tasks", "tasks_gt"):
            expected.update(
                _archive_members(
                    benchmark_root / collection / f"{task_id}.zip", benchmark_root
                )
            )
        for destination, content in expected.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != content:
                    raise RuntimeError(f"extracted benchmark source drifted: {destination}")
            else:
                destination.write_bytes(content)


def _single_task_manifests(
    experiment: Path, manifest: dict[str, Any]
) -> dict[str, Path]:
    output: dict[str, Path] = {}
    for entry in manifest["tasks"]:
        task_id = str(entry["task_id"])
        path = experiment / "control-manifests" / f"{task_id}.json"
        payload = {
            "schema_version": 1,
            "id": f"gamedevbench-unseen30-{task_id}-v1",
            "benchmark_commit": manifest["benchmark_commit"],
            "tasks": [entry],
        }
        _write_json(path, payload)
        output[task_id] = path
    return output


def _weekly_used_percent(snapshot: dict[str, object]) -> float | None:
    buckets = snapshot.get("buckets")
    if not isinstance(buckets, list):
        return None
    weekly = [
        bucket
        for bucket in buckets
        if isinstance(bucket, dict) and bucket.get("window_kind") == "weekly"
    ]
    if not weekly:
        return None
    selected = next((item for item in weekly if item.get("label") is None), weekly[0])
    used = selected.get("used_percent")
    return float(used) if isinstance(used, (int, float)) else None


def _run_logged(command: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("COMMAND " + json.dumps(command) + "\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name == "posix",
        )
        try:
            return_code = process.wait(timeout=2_100)
        except subprocess.TimeoutExpired as error:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
            log.write(f"\nOUTER_TIMEOUT after {error.timeout} seconds\n")
            raise RuntimeError(f"attempt exceeded outer timeout: {log_path}") from error
        log.write(f"\nEXIT {return_code}\n")
        log.write(f"ELAPSED {time.monotonic() - started:.3f}\n")
    return return_code


def _official_attempt(
    *,
    uv: str,
    benchmark_root: Path,
    experiment: Path,
    attempt: dict[str, Any],
    model: str,
    effort: str,
) -> tuple[dict[str, Any], int, Path]:
    attempt_id = str(attempt["attempt_id"])
    task_id = str(attempt["task_id"])
    run_name = f"unseen30-maturity-{attempt_id}"
    attempt_root = experiment / "attempts" / attempt_id
    task_list = attempt_root / "task-list.yaml"
    _write_text_once(task_list, f"tasks:\n- {task_id}\n")
    result_path = benchmark_root / "results" / run_name / "final_results.json"
    log_path = attempt_root / "runner.log"
    exit_code = 0
    if not result_path.exists():
        command = [
            uv,
            "run",
            "python",
            "gamedevbench/src/benchmark_runner.py",
            "--agent",
            "codex",
            "--model",
            model,
            "--effort",
            effort,
            "--run-name",
            run_name,
            "--parallel",
            "1",
            "--solver-timeout",
            "900",
            "run",
            "--task-list",
            str(task_list),
        ]
        exit_code = _run_logged(command, benchmark_root, log_path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    records = payload.get("tasks")
    if not isinstance(records, list) or len(records) != 1:
        raise RuntimeError(f"official result is not a one-task result: {result_path}")
    record = records[0]
    if record.get("task_name") != task_id:
        raise RuntimeError(f"official result task mismatch: {result_path}")
    return record, exit_code, result_path


def _harness_attempt(
    *,
    uv: str,
    root: Path,
    benchmark_root: Path,
    experiment: Path,
    manifest_path: Path,
    agent_executable: Path,
    attempt: dict[str, Any],
    model: str,
    effort: str,
    runtime_source: Path | None = None,
    python_executable: Path | None = None,
) -> tuple[dict[str, Any], int, Path]:
    attempt_id = str(attempt["attempt_id"])
    condition = str(attempt["condition"])
    attempt_root = experiment / "attempts" / attempt_id
    batch = attempt_root / "batch"
    progress_path = batch / "progress.json"
    log_path = attempt_root / "runner.log"
    exit_code = 0
    if not progress_path.exists():
        launcher = (
            [
                "/usr/bin/env",
                f"PYTHONPATH={runtime_source}",
                str(python_executable),
                str(runtime_source / "gameforge" / "cli.py"),
            ]
            if runtime_source is not None and python_executable is not None
            else [uv, "run", "gameforge"]
        )
        command = [
            *launcher,
            "benchmark-gamedevbench-batch",
            "--benchmark-root",
            str(benchmark_root),
            "--manifest",
            str(manifest_path),
            "--agent-executable",
            str(agent_executable),
            "--model",
            model,
            "--reasoning-effort",
            effort,
            "--runtime-protocol",
            condition,
            "--batch-directory",
            str(batch.relative_to(root)),
        ]
        exit_code = _run_logged(command, root, log_path)
    if not progress_path.exists():
        detail = f"Harness batch exited {exit_code} before writing progress.json"
        if log_path.is_file():
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:].strip()
            if log_tail:
                detail += f": {log_tail}"
        return (
            {
                "task_id": attempt["task_id"],
                "status": "INFRASTRUCTURE_ERROR",
                "failure_category": "infrastructure",
                "failure_detail": detail,
                "model_turns": 0,
                "lineage_complete": False,
            },
            exit_code,
            log_path,
        )
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    records = payload.get("tasks")
    if not isinstance(records, list) or len(records) != 1:
        raise RuntimeError(f"Harness result is not a one-task result: {progress_path}")
    record = records[0]
    if record.get("task_id") != attempt["task_id"]:
        raise RuntimeError(f"Harness result task mismatch: {progress_path}")
    return record, exit_code, progress_path


def _write_text_once(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise RuntimeError(f"refusing to change frozen artifact: {path}")
        return
    path.write_text(content, encoding="utf-8")


def _receipt_path(experiment: Path, attempt: dict[str, Any]) -> Path:
    return experiment / "receipts" / f"{int(attempt['ordinal']):03d}.json"


def _update_progress(experiment: Path, schedule: dict[str, Any]) -> None:
    receipts = []
    for attempt in schedule["attempts"]:
        path = _receipt_path(experiment, attempt)
        if path.exists():
            receipts.append(json.loads(path.read_text(encoding="utf-8")))
    condition_counts: dict[str, Counter[str]] = {
        condition: Counter() for condition in CONDITIONS
    }
    for receipt in receipts:
        condition_counts[str(receipt["condition"])][str(receipt["normalized_status"])] += 1
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "attempts_completed": len(receipts),
            "attempts_total": len(schedule["attempts"]),
            "next_ordinal": len(receipts) + 1,
            "condition_status_counts": {
                key: dict(sorted(value.items())) for key, value in condition_counts.items()
            },
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _normalize_result(condition: str, record: dict[str, Any]) -> str:
    if condition == "official-default":
        if record.get("skipped"):
            return "SKIPPED"
        if record.get("success") is True:
            return "PASS"
        if record.get("success") is False:
            return "FAIL"
        return "ERROR"
    status_value = str(record.get("status", "ERROR"))
    return status_value if status_value in {"PASS", "FAIL", "BLOCKED"} else "ERROR"


def main() -> int:
    arguments = _arguments()
    root = Path(__file__).resolve().parents[1]
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
    if not isinstance(tasks, list) or len(tasks) != 30:
        raise RuntimeError("the preregistered manifest must contain exactly 30 tasks")
    if arguments.model != manifest["experiment"]["model"]:
        raise RuntimeError("model differs from the preregistered protocol")
    if arguments.reasoning_effort != manifest["experiment"]["reasoning_effort"]:
        raise RuntimeError("reasoning effort differs from the preregistered protocol")

    runtime_digest, runtime_count = _runtime_digest(root)
    frozen = manifest["code_freeze"]
    if (
        runtime_digest != frozen["runtime_tree_sha256"]
        or runtime_count != frozen["runtime_file_count"]
    ):
        raise RuntimeError("Harness runtime differs from the preregistered code freeze")

    experiment.mkdir(parents=True, exist_ok=True)
    schedule = _schedule(tasks)
    schedule_path = experiment / "schedule.json"
    _write_json(schedule_path, schedule)
    schedule_hash = _sha256_file(schedule_path)
    task_manifests = _single_task_manifests(experiment, manifest)
    _ensure_task_sources(benchmark_root, [str(item["task_id"]) for item in tasks])
    _write_json(
        experiment / "protocol-freeze.json",
        {
            "schema_version": 1,
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_bytes),
            "schedule_sha256": schedule_hash,
            "benchmark_root": str(benchmark_root),
            "benchmark_commit": manifest["benchmark_commit"],
            "runtime_tree_sha256": runtime_digest,
            "runtime_file_count": runtime_count,
            "agent_executable": str(agent),
            "agent_executable_sha256": _sha256_file(agent),
            "model": arguments.model,
            "reasoning_effort": arguments.reasoning_effort,
            "attempts": len(schedule["attempts"]),
            "quota_stop_remaining_percent": 50,
        },
    )
    _update_progress(experiment, schedule)
    print(
        f"Prepared {len(tasks)} tasks and {len(schedule['attempts'])} attempts; "
        f"schedule sha256={schedule_hash}",
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

        condition = str(attempt["condition"])
        print(
            f"[{ordinal:03d}/270] r{attempt['repetition']} {attempt['task_id']} "
            f"{condition} ({remaining:.1f}% quota remaining)",
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
        else:
            record, exit_code, source = _harness_attempt(
                uv=uv,
                root=root,
                benchmark_root=benchmark_root,
                experiment=experiment,
                manifest_path=task_manifests[str(attempt["task_id"])],
                agent_executable=agent,
                attempt=attempt,
                model=arguments.model,
                effort=arguments.reasoning_effort,
            )
        normalized = _normalize_result(condition, record)
        if condition != "official-default" and record.get("status") == "INFRASTRUCTURE_ERROR":
            raise RuntimeError(f"Harness infrastructure failure retained at {source}")
        if condition == "official-default" and normalized == "ERROR":
            raise RuntimeError(f"official infrastructure/error result retained at {source}")
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

    complete_path = experiment / "complete.json"
    if not complete_path.exists():
        _write_json(
            complete_path,
            {
                "schema_version": 1,
                "attempts_completed": len(schedule["attempts"]),
                "completed_at": datetime.now(UTC).isoformat(),
                "runtime_tree_sha256": runtime_digest,
                "schedule_sha256": schedule_hash,
            },
        )
    print("All 270 preregistered attempts completed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
