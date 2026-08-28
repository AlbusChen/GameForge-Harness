#!/usr/bin/env python3
"""Run all 140 public GameCraft tasks with the frozen unified native-open Harness.

The completed family15 pilot is reused only after protocol compatibility checks.  The
remaining tasks can be deterministically sharded across workers; every task remains a
single independent model attempt with no Harness repair pass.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import run_gamecraft_bench_family15 as family


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERSION = ROOT
DEFAULT_BENCHMARK = ROOT / "third_party/gamecraft-bench"
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/gamecraft-bench-full140-unified-native-open-run1"
DEFAULT_PILOT = ROOT / "runs/experiments/gamecraft-bench-family15-unified-native-open-run1"
DEFAULT_AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
DEFAULT_GODOT = ROOT / "third_party/runtimes/godot-4.6.2/Godot.app/Contents/MacOS/Godot"
DEFAULT_LIVE_ROOT = Path("/private/tmp/gamecraft-bench-full140-live")
DEFAULT_IMAGE = "gamecraft-evaluator-arm:a433475"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=Path, default=DEFAULT_VERSION)
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--pilot-experiment", type=Path, default=DEFAULT_PILOT)
    parser.add_argument("--agent", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--godot", type=Path, default=DEFAULT_GODOT)
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE_ROOT)
    parser.add_argument("--evaluator-image", default=DEFAULT_IMAGE)
    parser.add_argument("--colima-profile", default="gamecraft-arm")
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--max-new-attempts", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--no-reuse-family15", action="store_true")
    parser.add_argument("--skip-official-evaluation", action="store_true")
    return parser.parse_args()


def _task_list(benchmark: Path) -> list[dict[str, Any]]:
    task_dirs = sorted(
        path
        for path in (benchmark / "tasks").iterdir()
        if path.is_dir() and path.name != "example"
    )
    if len(task_dirs) != 140:
        raise ValueError(f"expected 140 public tasks excluding example, found {len(task_dirs)}")
    tasks = []
    for ordinal, task_dir in enumerate(task_dirs, start=1):
        instruction = task_dir / "instruction.md"
        rubric = task_dir / "tests/rubric.json"
        if not instruction.is_file() or not rubric.is_file():
            raise ValueError(f"incomplete public task: {task_dir.name}")
        tasks.append(
            {
                "ordinal": ordinal,
                "task_id": task_dir.name,
                "family": task_dir.name.split("-", 1)[0],
                "instruction_sha256": family._sha256(instruction),
                "rubric_sha256": family._sha256(rubric),
            }
        )
    if len({task["family"] for task in tasks}) != 15:
        raise ValueError("full140 does not cover exactly 15 task families")
    return tasks


def _clone_directory(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    if not source.is_dir() or destination.exists():
        raise RuntimeError(f"invalid clone source/destination: {source} -> {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cp", "-cR", str(source), str(destination)],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"APFS experiment clone failed: {completed.stderr.strip()}")


def _write_progress(experiment: Path, total: int, worker: int | None = None) -> None:
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    payload = {
        "schema_version": 1,
        "completed": len(receipts),
        "total": total,
        "solver_passes": sum(item.get("solver", {}).get("status") == "PASS" for item in receipts),
        "official_build_passes": sum(
            item.get("official_evidence", {}).get("build_ok") is True for item in receipts
        ),
        "infrastructure_failures": sum("infrastructure_error" in item for item in receipts),
        "reused_attempts": sum("reused_from" in item for item in receipts),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    name = "progress.json" if worker is None else f"progress-worker-{worker:02d}.json"
    family._write_json(experiment / name, payload)


def _reuse_pilot(
    *, pilot: Path, experiment: Path, tasks: list[dict[str, Any]], protocol: dict[str, Any]
) -> None:
    pilot_protocol_path = pilot / "protocol-freeze.json"
    pilot_protocol = json.loads(pilot_protocol_path.read_text(encoding="utf-8"))
    compatibility_keys = (
        "harness_tree_sha256",
        "benchmark_commit",
        "agent_sha256",
        "godot_sha256",
        "evaluator_image_id",
        "solver",
        "evaluation",
    )
    for key in compatibility_keys:
        if pilot_protocol[key] != protocol[key]:
            raise RuntimeError(f"family15 reuse protocol mismatch: {key}")
    by_id = {task["task_id"]: task for task in tasks}
    for source_receipt_path in sorted((pilot / "receipts").glob("*.json")):
        source_receipt = json.loads(source_receipt_path.read_text(encoding="utf-8"))
        task = by_id[source_receipt["task_id"]]
        ordinal = int(task["ordinal"])
        attempt_id = f"{ordinal:03d}-{task['task_id']}"
        destination_receipt = experiment / "receipts" / f"{ordinal:03d}.json"
        if destination_receipt.exists():
            continue
        source_attempt = pilot / "attempts" / (
            f"{int(source_receipt['ordinal']):02d}-{source_receipt['task_id']}"
        )
        destination_attempt = experiment / "attempts" / attempt_id
        _clone_directory(source_attempt, destination_attempt)
        source_receipt["ordinal"] = ordinal
        source_receipt["attempt_id"] = attempt_id
        source_receipt["family"] = task["family"]
        source_receipt["reused_from"] = {
            "experiment": str(pilot),
            "attempt": str(source_attempt),
            "receipt_sha256": family._sha256(source_receipt_path),
            "protocol_sha256": family._sha256(pilot_protocol_path),
            "reason": "identical solver, harness, benchmark, engine, evaluator, and no-repair protocol",
        }
        family._write_json(destination_receipt, source_receipt)


def _fresh_live_path(live_root: Path, worker: int, attempt_id: str) -> Path:
    base = live_root / f"worker-{worker:02d}" / attempt_id
    if not base.exists():
        return base
    suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return base.with_name(f"{attempt_id}-resume-{suffix}")


def main() -> int:
    arguments = _arguments()
    if arguments.worker_count < 1 or not 0 <= arguments.worker_index < arguments.worker_count:
        raise ValueError("worker-index must be in [0, worker-count)")
    if arguments.max_new_attempts is not None and arguments.max_new_attempts < 0:
        raise ValueError("max-new-attempts must be nonnegative")

    version = arguments.version.resolve(strict=True)
    source_candidate = version / "source"
    source = (source_candidate if source_candidate.is_dir() else version).resolve(strict=True)
    benchmark = arguments.benchmark_root.resolve(strict=True)
    experiment = arguments.experiment.resolve()
    pilot = arguments.pilot_experiment.resolve(strict=True)
    agent = arguments.agent.resolve(strict=True)
    godot = arguments.godot.resolve(strict=True)
    live_root = arguments.live_root.resolve()
    tasks = _task_list(benchmark)
    pilot_manifest = json.loads(family.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest = {
        "benchmark": {
            **pilot_manifest["benchmark"],
            "selection_rule": "all lexicographically sorted public task directories except tasks/example",
        },
        "solver": pilot_manifest["solver"],
        "evaluation": pilot_manifest["evaluation"],
    }
    metadata_path = version / "VERSION.json"
    if not metadata_path.is_file():
        metadata_path = version / "RELEASE.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    repo_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=benchmark,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if repo_commit != manifest["benchmark"]["commit"]:
        raise ValueError(f"benchmark commit differs: {repo_commit}")
    evaluator_image = family._evaluator_image_metadata(
        arguments.colima_profile, arguments.evaluator_image
    )
    protocol = {
        "schema_version": 1,
        "runner": str(Path(__file__).resolve()),
        "runner_sha256": family._sha256(Path(__file__).resolve()),
        "harness_version": metadata["version"],
        "harness_tree_sha256": metadata["runtime_tree_sha256"],
        "benchmark_commit": repo_commit,
        "task_selection": manifest["benchmark"]["selection_rule"],
        "task_count": len(tasks),
        "agent": str(agent),
        "agent_sha256": family._sha256(agent),
        "godot": str(godot),
        "godot_sha256": family._sha256(godot),
        "godot_version": family._godot_version(godot),
        "evaluator_image": arguments.evaluator_image,
        "evaluator_image_id": evaluator_image["id"],
        "evaluator_image_architecture": evaluator_image["architecture"],
        "evaluator_image_os": evaluator_image["os"],
        "evaluator_godot_version": "4.6.2.stable.official.71f334935",
        "evaluator_godot_sha256": "34a1ce46e58920ad4db010c0dab5a564b1c096e5a6f8e2a61eb46d802518f501",
        "colima_profile": arguments.colima_profile,
        "solver": manifest["solver"],
        "evaluation": manifest["evaluation"],
        "assets": {
            "kenney": "complete pinned public library",
            "open_game_art": "empty, retained for protocol compatibility with family15",
        },
        "pilot_reuse": {
            "source": str(pilot),
            "task_count": 15,
            "compatibility": "exact protocol fields checked before APFS clone",
        },
        "tasks": tasks,
    }
    experiment.mkdir(parents=True, exist_ok=True)
    family._freeze_json(experiment / "protocol-freeze.json", protocol)
    if not arguments.no_reuse_family15:
        _reuse_pilot(pilot=pilot, experiment=experiment, tasks=tasks, protocol=protocol)
    _write_progress(experiment, len(tasks))
    if arguments.prepare_only:
        print(experiment / "protocol-freeze.json")
        return 0

    runtime = family._load_runtime(source)
    completed_ids = {
        json.loads(path.read_text(encoding="utf-8"))["task_id"]
        for path in (experiment / "receipts").glob("*.json")
    }
    pending = [
        task for task in tasks
        if task["task_id"] not in completed_ids
        and (int(task["ordinal"]) - 1) % arguments.worker_count == arguments.worker_index
    ]
    if arguments.max_new_attempts is not None:
        pending = pending[: arguments.max_new_attempts]
    kenney = (benchmark / "assets/library").resolve(strict=True)
    screenshot_wrapper = (
        ROOT / "experiments/gamecraft_bench/macos_screenshot.sh"
    ).resolve(strict=True)
    live_root.mkdir(parents=True, exist_ok=True)

    for task in pending:
        ordinal = int(task["ordinal"])
        task_id = str(task["task_id"])
        attempt_id = f"{ordinal:03d}-{task_id}"
        live = _fresh_live_path(live_root, arguments.worker_index, attempt_id)
        preserved = experiment / "attempts" / attempt_id
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "ordinal": ordinal,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "family": task["family"],
            "worker_index": arguments.worker_index,
            "started_at": datetime.now(UTC).isoformat(),
        }
        try:
            workspace, resources = family._prepare_live(
                live=live,
                benchmark=benchmark,
                kenney=kenney,
                screenshot_wrapper=screenshot_wrapper,
            )
            raw_instruction = (
                benchmark / "tasks" / task_id / "instruction.md"
            ).read_text(encoding="utf-8")
            instruction = family._adapt_instruction(raw_instruction, workspace, resources)
            (live / "adapted-instruction.md").write_text(instruction, encoding="utf-8")
            receipt["solver"] = family._run_solver(
                runtime=runtime,
                live=live,
                workspace=workspace,
                instruction=instruction,
                task=task,
                manifest=manifest,
                source=source,
                agent=agent,
                godot=godot,
            )
            family._preserve_live(live, preserved)
            shutil.copy2(live / "adapted-instruction.md", preserved / "adapted-instruction.md")
            if not arguments.skip_official_evaluation:
                receipt["official_evidence"] = family._run_official_evidence(
                    workspace=(preserved / "workspace").resolve(strict=True),
                    rubric=(benchmark / "tasks" / task_id / "tests/rubric.json").resolve(strict=True),
                    output=preserved / "official-evidence",
                    profile=arguments.colima_profile,
                    image=arguments.evaluator_image,
                )
        except Exception as error:
            receipt["infrastructure_error"] = f"{type(error).__name__}: {error}"
        receipt["ended_at"] = datetime.now(UTC).isoformat()
        family._write_json(experiment / "receipts" / f"{ordinal:03d}.json", receipt)
        _write_progress(experiment, len(tasks), arguments.worker_index)
        status = receipt.get("official_evidence", {}).get(
            "build_ok", receipt.get("solver", {}).get("status", "INFRASTRUCTURE_ERROR")
        )
        print(f"[{ordinal:03d}/140] {task_id}: {status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
