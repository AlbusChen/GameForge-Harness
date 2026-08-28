#!/usr/bin/env python3
"""Replay task_0009 to verify authoritative verdict precedence over import diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.run_unseen30_maturity import _sha256_file, _write_json
from gameforge.benchmarking.gamedevbench import (
    GameDevBenchOfficialEvaluator,
    GameDevBenchSource,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "runs/gamedevbench-harness/20260814T083355Z-gamedevbench-task_0009"
DEFAULT_WORKSPACE = (
    ROOT / "runs/gamedevbench-workspaces/20260814T083355Z-gamedevbench-task_0009"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("/private/tmp/gamedevbench-e3868-pinned"),
    )
    parser.add_argument(
        "--godot",
        type=Path,
        default=Path("/private/tmp/godot-4.4.1-bin/godot"),
    )
    parser.add_argument("--source-run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=(
            ROOT
            / "runs/experiments"
            / "gamedevbench-v23-evaluator-precedence-replay-task0009"
        ),
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    godot = arguments.godot.resolve(strict=True)
    source_run = arguments.source_run.resolve(strict=True)
    workspace = arguments.workspace.resolve(strict=True)
    output_directory = arguments.output_directory.resolve()
    old_log = source_run / "official-evaluator.log"
    old_output = old_log.read_text(encoding="utf-8")
    if "SCRIPT ERROR:" not in old_output or "VALIDATION_PASSED" not in old_output:
        raise RuntimeError("source run does not contain the diagnosed mixed verdict evidence")
    manifest = json.loads((source_run / "output-manifest.json").read_text(encoding="utf-8"))
    editable = tuple(str(path) for path in manifest["writable_paths"])
    source = GameDevBenchSource(benchmark_root, "task_0009", editable)
    replay_directory = output_directory / "evaluator"
    gate = GameDevBenchOfficialEvaluator(
        source=source,
        godot=godot,
        run_directory=replay_directory,
    )._official(workspace)
    replay_log = replay_directory / "official-evaluator.log"
    payload = {
        "schema_version": 1,
        "task_id": "task_0009",
        "source_run": str(source_run),
        "source_log_sha256": _sha256_file(old_log),
        "source_contains_import_script_error": "SCRIPT ERROR:" in old_output,
        "source_contains_explicit_pass": "VALIDATION_PASSED" in old_output,
        "godot": str(godot),
        "replay_log": str(replay_log),
        "replay_log_sha256": _sha256_file(replay_log),
        "official_gate": gate.status.value,
        "official_detail": gate.detail,
    }
    output = output_directory / "result.json"
    _write_json(output, payload)
    print(output)
    print(json.dumps(payload, sort_keys=True))
    return 0 if gate.status.value == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
