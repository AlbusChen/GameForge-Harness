#!/usr/bin/env python3
"""Run one candidate-only iteration on the frozen Fresh-50 task manifest."""

from __future__ import annotations

import argparse
import json
import selectors
import shutil
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from gameforge.telemetry.subscription import (
    _sanitize_snapshot,
    _sanitized_environment,
    _terminate,
    capture_subscription_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PROTOCOL = "programmable-v1"
BASELINE = ROOT / "runs/experiments/gamedevbench-fresh50-official-v4-v1"
QUOTA_CAPTURE_ATTEMPTS = 6
QUOTA_CAPTURE_RETRY_SECONDS = 10


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
        "--baseline-experiment",
        type=Path,
        default=BASELINE,
    )
    parser.add_argument("--condition", default="programmable-open-adaptive-v5")
    parser.add_argument("--experiment-directory", type=Path)
    parser.add_argument(
        "--agent-executable",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _schedule(tasks: list[dict[str, Any]], condition: str) -> dict[str, object]:
    attempts = [
        {
            "ordinal": index,
            "attempt_id": f"fresh50-{task['task_id']}-{condition}",
            "task_id": task["task_id"],
            "task_name": task["name"],
            "stratum": task["stratum"],
            "manifest_order": index,
            "condition": condition,
        }
        for index, task in enumerate(tasks, start=1)
    ]
    return {
        "schema_version": 1,
        "algorithm": "candidate-only manifest order",
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
                "id": f"gamedevbench-fresh50-{task_id}-candidate-iteration",
                "benchmark_commit": manifest["benchmark_commit"],
                "tasks": [task],
            },
        )
        paths[task_id] = path
    return paths


def _receipt_path(experiment: Path, attempt: dict[str, Any]) -> Path:
    return experiment / "receipts" / f"{int(attempt['ordinal']):03d}.json"


def _capture_quota_with_retry(agent: Path, *, ordinal: int) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for capture_attempt in range(1, QUOTA_CAPTURE_ATTEMPTS + 1):
        snapshot = capture_subscription_snapshot(agent)
        if _weekly_used_percent(snapshot) is None:
            snapshot = _capture_rate_limit_only_snapshot(agent)
        if _weekly_used_percent(snapshot) is not None:
            return {**snapshot, "capture_attempts": capture_attempt}
        if capture_attempt < QUOTA_CAPTURE_ATTEMPTS:
            print(
                f"quota snapshot unavailable before attempt {ordinal}; "
                f"retry {capture_attempt}/{QUOTA_CAPTURE_ATTEMPTS}",
                flush=True,
            )
            time.sleep(QUOTA_CAPTURE_RETRY_SECONDS)
    return {**snapshot, "capture_attempts": QUOTA_CAPTURE_ATTEMPTS}


def _capture_rate_limit_only_snapshot(
    executable: Path,
    *,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    """Fallback when nonessential account usage telemetry is unavailable."""
    captured_at = datetime.now(UTC).isoformat()
    process: subprocess.Popen[str] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            [str(executable.resolve(strict=True)), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_sanitized_environment(),
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        for message in (
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "gameforge_experiment_control",
                        "title": "GameForge Experiment Control",
                        "version": "0.1.0",
                    }
                },
            },
            {"method": "initialized", "params": {}},
            {"method": "account/rateLimits/read", "id": 1},
        ):
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        responses: dict[int, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline and not {0, 1}.issubset(responses):
            remaining = max(0.0, deadline - time.monotonic())
            if not selector.select(timeout=min(1.0, remaining)):
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            identifier = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(identifier, int) and identifier in {0, 1}:
                if "error" in payload:
                    raise RuntimeError(
                        f"rate-limit telemetry request {identifier} failed"
                    )
                responses[identifier] = payload
        if not {0, 1}.issubset(responses):
            raise TimeoutError("rate-limit telemetry responses were incomplete")
        sanitized = _sanitize_snapshot(
            {**responses, 2: {"result": {}}},
            captured_at=captured_at,
        )
        return {**sanitized, "source": "rate-limits-only-fallback"}
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        return {
            "schema_version": 1,
            "captured_at": captured_at,
            "status": "unavailable",
            "error_category": type(error).__name__,
            "source": "rate-limits-only-fallback",
        }
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            _terminate(process)


def _update_progress(experiment: Path, schedule: dict[str, Any]) -> None:
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for attempt in schedule["attempts"]
        if (path := _receipt_path(experiment, attempt)).exists()
    ]
    statuses = Counter(str(row["normalized_status"]) for row in rows)
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "attempts_completed": len(rows),
            "attempts_total": len(schedule["attempts"]),
            "next_ordinal": len(rows) + 1,
            "status_counts": dict(sorted(statuses.items())),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    baseline = arguments.baseline_experiment.resolve(strict=True)
    if not (baseline / "complete.json").is_file():
        raise RuntimeError("Fresh-50 Official/v4 baseline is not complete")
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

    experiment = (
        arguments.experiment_directory
        or ROOT / f"runs/experiments/gamedevbench-fresh50-{arguments.condition}-run1"
    ).resolve()
    runtime_digest, runtime_count = _runtime_digest(ROOT)
    schedule = _schedule(tasks, arguments.condition)
    experiment.mkdir(parents=True, exist_ok=True)
    schedule_path = experiment / "schedule.json"
    _write_json(schedule_path, schedule)
    manifests = _task_manifests(experiment, manifest)
    _ensure_benchmark_sources(benchmark_root, [str(task["task_id"]) for task in tasks])
    protocol = {
        "schema_version": 1,
        "design": "candidate-only Fresh-50 iteration against frozen Official/v4 receipts",
        "condition": arguments.condition,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_bytes),
        "schedule_sha256": _sha256_file(schedule_path),
        "baseline_experiment": str(baseline),
        "baseline_complete_sha256": _sha256_file(baseline / "complete.json"),
        "baseline_protocol_sha256": _sha256_file(baseline / "protocol-freeze.json"),
        "benchmark_root": str(benchmark_root),
        "benchmark_commit": manifest["benchmark_commit"],
        "runtime_tree_sha256": runtime_digest,
        "runtime_file_count": runtime_count,
        "agent_executable": str(agent),
        "agent_executable_sha256": _sha256_file(agent),
        "model": arguments.model,
        "reasoning_effort": arguments.reasoning_effort,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "attempts": 50,
        "quota_stop_remaining_percent": 50,
        "interpretation": (
            "This rerun is retrospective on the now-observed Fresh-50 set; mechanism tests and "
            "the open-request proxy are separate validation evidence."
        ),
    }
    protocol_path = experiment / "protocol-freeze.json"
    if protocol_path.exists():
        existing = json.loads(protocol_path.read_text(encoding="utf-8"))
        if existing != protocol:
            raise RuntimeError("candidate protocol differs from frozen protocol")
    else:
        _write_json(protocol_path, protocol)
    _update_progress(experiment, schedule)
    print(
        f"Prepared 50 candidate attempts; condition={arguments.condition}; "
        f"runtime sha256={runtime_digest}",
        flush=True,
    )
    if arguments.prepare_only:
        return 0

    for attempt in schedule["attempts"]:
        receipt_path = _receipt_path(experiment, attempt)
        if receipt_path.exists():
            continue
        current_digest, current_count = _runtime_digest(ROOT)
        if current_digest != runtime_digest or current_count != runtime_count:
            raise RuntimeError("Harness runtime changed after candidate iteration start")
        ordinal = int(attempt["ordinal"])
        quota_path = experiment / "quota" / f"{ordinal:03d}-before.json"
        quota = _capture_quota_with_retry(agent, ordinal=ordinal)
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

        print(
            f"[{ordinal:03d}/050] {attempt['task_id']} {arguments.condition} "
            f"({remaining:.1f}% quota remaining)",
            flush=True,
        )
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
                "runtime_protocol": RUNTIME_PROTOCOL,
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
            "attempts_completed": 50,
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_tree_sha256": runtime_digest,
            "schedule_sha256": _sha256_file(schedule_path),
        },
    )
    print("All 50 candidate iteration attempts completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
