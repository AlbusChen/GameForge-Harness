#!/usr/bin/env python3
"""Run one diagnostic GameDevBench task through the current Programmable Harness."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from experiments.run_unseen30_candidate_v2 import _ensure_benchmark_sources
from experiments.run_unseen30_maturity import (
    _harness_attempt,
    _runtime_digest,
    _sha256_file,
    _single_task_manifests,
    _weekly_used_percent,
    _write_json,
)
from gameforge.telemetry.subscription import capture_subscription_snapshot


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("/private/tmp/gamedevbench-e3868-pinned"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "benchmarks/gamedevbench-unseen30-maturity-v1.json",
    )
    parser.add_argument("--experiment-directory", type=Path, required=True)
    parser.add_argument(
        "--agent-executable",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    arguments = parser.parse_args()

    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(
        (item for item in manifest["tasks"] if item["task_id"] == arguments.task_id),
        None,
    )
    if entry is None:
        raise ValueError(f"task is not in the frozen manifest: {arguments.task_id}")
    experiment = arguments.experiment_directory.resolve()
    experiment.mkdir(parents=True, exist_ok=True)
    runtime_digest, runtime_count = _runtime_digest(root)
    manifests = _single_task_manifests(experiment, manifest)
    _ensure_benchmark_sources(benchmark_root, [arguments.task_id])
    quota = capture_subscription_snapshot(arguments.agent_executable.resolve(strict=True))
    used = _weekly_used_percent(quota)
    if used is None or 100 - used < 50:
        raise RuntimeError("weekly subscription remaining is unavailable or below 50%")
    _write_json(experiment / "quota-before.json", quota)
    _write_json(
        experiment / "protocol-freeze.json",
        {
            "schema_version": 1,
            "design": "targeted mechanism replay; excluded from final outcome score",
            "task_id": arguments.task_id,
            "runtime_tree_sha256": runtime_digest,
            "runtime_file_count": runtime_count,
            "model": arguments.model,
            "reasoning_effort": arguments.reasoning_effort,
            "benchmark_root": str(benchmark_root),
        },
    )
    attempt = {
        "attempt_id": f"targeted-{arguments.task_id}-programmable-v1",
        "task_id": arguments.task_id,
        "task_name": entry["name"],
        "condition": "programmable-v1",
    }
    record, exit_code, source = _harness_attempt(
        uv=uv,
        root=root,
        benchmark_root=benchmark_root,
        experiment=experiment,
        manifest_path=manifests[arguments.task_id],
        agent_executable=arguments.agent_executable.resolve(strict=True),
        attempt=attempt,
        model=arguments.model,
        effort=arguments.reasoning_effort,
    )
    _write_json(
        experiment / "receipt.json",
        {
            **attempt,
            "schema_version": 1,
            "runner_exit_code": exit_code,
            "result_source": str(source),
            "result_source_sha256": _sha256_file(source),
            "weekly_used_percent_before": used,
            "record": record,
        },
    )
    print(json.dumps({"status": record.get("status"), "result": str(source)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
