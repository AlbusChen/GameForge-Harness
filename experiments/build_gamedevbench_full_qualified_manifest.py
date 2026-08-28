#!/usr/bin/env python3
"""Freeze the full locally qualified GameDevBench evaluation manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from experiments.build_fresh50_manifest import _family, _public_config, _stratum
from experiments.run_unseen30_maturity import _runtime_digest
from gameforge.harness.game_tasks import RuntimeProtocol

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "e3868bccbb88e86a3eb2d62154f1e9e0f3fd489a"
TOTAL_TASKS = 333
MODEL = "gpt-5.6-sol"
EFFORT = "medium"
OFFICIAL_HARNESS = "Codex"
OFFICIAL_PASS_RATE_PERCENT = 58.6
EXPECTED_ENGINE_VERSION = "4.4.1.stable.official.49a5bc7b6"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("/private/tmp/gamedevbench-e3868-pinned"),
    )
    parser.add_argument(
        "--godot-executable",
        type=Path,
        default=Path("/private/tmp/godot-4.4.1/Godot.app/Contents/MacOS/Godot"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "benchmarks/gamedevbench-full-qualified332-v1.json",
    )
    parser.add_argument(
        "--condition",
        default="programmable-open-provenance-v19-full332",
    )
    parser.add_argument(
        "--manifest-id",
        default="gamedevbench-full-qualified332-v1",
    )
    parser.add_argument(
        "--purpose-version",
        default="v19 open-agency Harness",
    )
    parser.add_argument(
        "--pre-freeze-verification",
        default=(
            "Full pytest suite, Ruff, git diff --check, and v19 comparative-analysis "
            "replay passed on 2026-08-13."
        ),
    )
    parser.add_argument("--parallelism", type=int, default=1, choices=range(1, 9))
    parser.add_argument(
        "--quota-stop-remaining-percent",
        type=float,
        default=50,
    )
    parser.add_argument(
        "--runtime-protocol",
        choices=tuple(protocol.value for protocol in RuntimeProtocol),
        default=RuntimeProtocol.PROGRAMMABLE_V1.value,
    )
    return parser.parse_args()


def _canonical(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _engine_version(executable: Path) -> str:
    completed = subprocess.run(
        [str(executable), "--headless", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _official_reference(leaderboard: Path) -> dict[str, object]:
    with leaderboard.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    matches = [
        row
        for row in rows
        if row["model"] == f"{MODEL} ({EFFORT})" and row["harness"] == OFFICIAL_HARNESS
    ]
    if len(matches) != 1:
        raise RuntimeError("Official full-benchmark reference row is not unique")
    row = matches[0]
    rate = float(row["pass_at_1_percent"])
    if rate != OFFICIAL_PASS_RATE_PERCENT:
        raise RuntimeError("Official full-benchmark reference rate changed")
    possible_passes = [
        value for value in range(TOTAL_TASKS + 1) if round(100.0 * value / TOTAL_TASKS, 1) == rate
    ]
    if len(possible_passes) != 1:
        raise RuntimeError("cannot recover a unique Official pass count")
    return {
        "source": str(leaderboard),
        "source_sha256": _sha256_file(leaderboard),
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "harness": OFFICIAL_HARNESS,
        "passes": possible_passes[0],
        "attempts": TOTAL_TASKS,
        "pass_rate": possible_passes[0] / TOTAL_TASKS,
        "reported_pass_at_1_percent": rate,
        "reported_ci_95_percent": float(row["ci_95_percent"]),
        "qualified_denominator_pass_range": [
            possible_passes[0] - 1,
            possible_passes[0],
        ],
        "qualified_denominator_attempts": TOTAL_TASKS - 1,
        "comparison_note": (
            "The public leaderboard does not include per-task outcomes. Because exactly one "
            "ground-truth-invalid task is excluded, Official's qualified score is reported as "
            "the two-value range obtained by assuming that task either passed or failed."
        ),
    }


def main() -> int:
    arguments = _arguments()
    if not 0 <= arguments.quota_stop_remaining_percent <= 100:
        raise ValueError("quota stop remaining percent must be between 0 and 100")
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    godot = arguments.godot_executable.resolve(strict=True)
    output = arguments.output.resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=benchmark_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != COMMIT:
        raise RuntimeError(f"benchmark checkout is not pinned: {head}")
    version = _engine_version(godot)
    if version != EXPECTED_ENGINE_VERSION:
        raise RuntimeError(f"unexpected Godot version: {version}")

    task_order = yaml.safe_load((benchmark_root / "tasks.yaml").read_text(encoding="utf-8"))[
        "tasks"
    ]
    if len(task_order) != TOTAL_TASKS or len(set(task_order)) != TOTAL_TASKS:
        raise RuntimeError("official task list must contain 333 unique tasks")
    summary_path = benchmark_root / "results/gt_validation_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("total") != TOTAL_TASKS:
        raise RuntimeError("ground-truth qualification did not cover all tasks")
    failed = [str(value) for value in summary.get("failed", [])]
    if failed != ["task_0097"] or summary.get("passed") != TOTAL_TASKS - 1:
        raise RuntimeError(f"unexpected ground-truth qualification result: {summary}")

    tasks: list[dict[str, object]] = []
    strata: Counter[str] = Counter()
    for task_id in task_order:
        public_archive = benchmark_root / "tasks" / f"{task_id}.zip"
        ground_truth_archive = benchmark_root / "tasks_gt" / f"{task_id}.zip"
        for archive in (public_archive, ground_truth_archive):
            with zipfile.ZipFile(archive) as source:
                corrupt = source.testzip()
            if corrupt is not None:
                raise RuntimeError(f"corrupt benchmark archive: {archive}:{corrupt}")
        if task_id in failed:
            continue
        config: dict[str, Any] = _public_config(public_archive, task_id)
        stratum = _stratum(config)
        strata[stratum] += 1
        instruction = str(config.get("instruction", ""))
        tasks.append(
            {
                "task_id": task_id,
                "name": str(config.get("name", task_id)),
                "stratum": stratum,
                "family_key": _family(config),
                "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                "public_archive_sha256": _sha256_file(public_archive),
                "ground_truth_archive_sha256": _sha256_file(ground_truth_archive),
            }
        )
    if len(tasks) != 332:
        raise RuntimeError(f"expected 332 qualified tasks, found {len(tasks)}")

    runtime_digest, runtime_count = _runtime_digest(ROOT)
    payload = {
        "schema_version": 1,
        "id": arguments.manifest_id,
        "benchmark_commit": COMMIT,
        "purpose": (
            f"Complete GameDevBench evaluation of the cumulative {arguments.purpose_version} "
            "against the public same-model Official Codex aggregate."
        ),
        "qualification": {
            "method": (
                "Before any full-run model outcome, execute the benchmark's official validator "
                "on every ground-truth project using the benchmark-specified engine. Exclude "
                "only tasks whose ground truth fails both the parallel run and sequential retry."
            ),
            "summary_source": str(summary_path),
            "summary_source_sha256": _sha256_file(summary_path),
            "total_tasks": TOTAL_TASKS,
            "eligible_tasks": len(tasks),
            "excluded_tasks": [
                {
                    "task_id": "task_0097",
                    "reason": (
                        "Official ground truth failed twice: VisualShader must include "
                        "distortionUV.gdshaderinc"
                    ),
                    "classification": "benchmark_ground_truth_invalid",
                }
            ],
            "godot_executable": str(godot),
            "godot_executable_sha256": _sha256_file(godot),
            "godot_version": version,
            "archive_counts": {"public": TOTAL_TASKS, "ground_truth": TOTAL_TASKS},
        },
        "stratum_method": (
            "The repository's frozen public-text regex taxonomy is retained for continuity; "
            "these are analysis strata, not the benchmark's headline categories."
        ),
        "stratum_counts": dict(sorted(strata.items())),
        "experiment": {
            "condition": arguments.condition,
            "runtime_protocol": arguments.runtime_protocol,
            "repetitions_per_task": 1,
            "model": MODEL,
            "reasoning_effort": EFFORT,
            "provider_sampling": "provider-default",
            "parallelism": arguments.parallelism,
            "quota_stop_remaining_percent": arguments.quota_stop_remaining_percent,
            "primary_endpoint": "official evaluator PASS over all 332 eligible tasks",
            "secondary_endpoints": (
                [
                    "coverage",
                    "passive tool audit completeness",
                    "latency",
                    "tokens",
                    "tool calls",
                    "model turns",
                    "workspace timeout rate",
                ]
                if arguments.runtime_protocol == RuntimeProtocol.MINIMAL_OPEN_V1.value
                else [
                    "coverage",
                    "preservation",
                    "policy violations",
                    "latency",
                    "tokens",
                    "tool calls",
                    "model turns",
                    "well-formed decision rate",
                    "workspace timeout rate",
                ]
            ),
            "infrastructure_policy": (
                "Model-caused invalid output or code failure remains FAIL. A deterministic "
                "Harness admission limit remains BLOCKED and counts against coverage. A zero-turn "
                "pre-request infrastructure failure may receive one recorded retry; a repeated "
                "failure remains ERROR, is excluded from conditional accuracy, and is counted as "
                "a failure in the all-eligible lower-bound score."
            ),
            "stop_rule": (
                "Stop before an attempt if weekly subscription remaining is below "
                f"{arguments.quota_stop_remaining_percent:g}%. No task replacement or runtime "
                "change is permitted after protocol freeze."
            ),
        },
        "official_reference": _official_reference(benchmark_root / "results/leaderboard.csv"),
        "code_freeze": {
            "runtime_scope": ["gameforge/**", "pyproject.toml", "uv.lock"],
            "runtime_file_count": runtime_count,
            "runtime_tree_sha256": runtime_digest,
            "pre_freeze_verification": arguments.pre_freeze_verification,
        },
        "tasks": tasks,
    }
    encoded = _canonical(payload)
    if output.exists() and output.read_bytes() != encoded:
        raise RuntimeError(f"refusing to replace frozen full manifest: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    print(output)
    print(hashlib.sha256(encoded).hexdigest())
    print(json.dumps(dict(strata), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
