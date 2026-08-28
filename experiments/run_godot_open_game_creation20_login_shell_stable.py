#!/usr/bin/env python3
"""Run the independent login-shell-stable candidate on the frozen open20 tasks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from experiments.run_godot_open_game_creation6 import (
    DEFAULT_AGENT,
    DEFAULT_GODOT,
    GODOT_LOCK,
    PROBE,
    _credential_scrubbed_environment,
    _evaluate_project,
    _isolated_godot_runtime,
    _parse_agent_jsonl,
    _progress,
    _sha256_file,
    _write_json,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/godot-open-game-creation20-v1.json"
DEFAULT_CANDIDATE = ROOT / "variants/login-shell-stable-engine-tools/source"
DEFAULT_EXPERIMENT = (
    ROOT / "runs/experiments/godot-open-game-creation20-login-shell-stable-run1"
)
CONDITION = "login-shell-stable-engine-tools"
PARENT_CONDITION = "baseline-minimal-open"
SHELL_ENVIRONMENT_MODULE = Path("gameforge/harness/shell_environment.py")
RUNTIME_PATHS = ("gameforge", "pyproject.toml", "uv.lock")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--experiment-directory", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--candidate-source", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--agent-executable", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--godot-executable", type=Path, default=DEFAULT_GODOT)
    parser.add_argument("--max-new-attempts", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _runtime_digest(root: Path) -> tuple[str, int]:
    files: list[Path] = []
    for relative in RUNTIME_PATHS:
        candidate = root / relative
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
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


def _load_shell_environment(candidate_source: Path) -> Callable[..., dict[str, str]]:
    module_path = (candidate_source / SHELL_ENVIRONMENT_MODULE).resolve(strict=True)
    specification = importlib.util.spec_from_file_location(
        "login_shell_stable_candidate_environment",
        module_path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load candidate shell environment module")
    module = importlib.util.module_from_spec(specification)
    assert isinstance(module, ModuleType)
    specification.loader.exec_module(module)
    helper = getattr(module, "login_shell_tool_environment", None)
    if not callable(helper):
        raise RuntimeError("candidate shell environment helper is unavailable")
    return helper


def _candidate_schedule(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    source = [
        item
        for item in manifest["schedule"]
        if str(item["condition"]) == PARENT_CONDITION
    ]
    if len(source) != int(manifest["design"]["task_count"]):
        raise ValueError("frozen manifest does not contain one parent attempt per task")
    return [
        {
            "ordinal": index,
            "source_ordinal": int(item["ordinal"]),
            "task_id": str(item["task_id"]),
            "condition": CONDITION,
        }
        for index, item in enumerate(source, start=1)
    ]


def _prompt(brief: str) -> str:
    return (
        "Implement the following request in the current disposable workspace. Work freely "
        "and use any available local tools as you see fit; an isolated Godot executable is "
        "available as `godot`.\n\nREQUEST:\n" + brief
    )


def _run_attempt(
    *,
    experiment: Path,
    attempt: dict[str, Any],
    task: dict[str, Any],
    model: str,
    effort: str,
    timeout_seconds: int,
    agent: Path,
    godot_source: Path,
    candidate_source: Path,
    login_shell_environment: Callable[..., dict[str, str]],
) -> dict[str, Any]:
    ordinal = int(attempt["ordinal"])
    task_id = str(attempt["task_id"])
    attempt_id = f"{ordinal:02d}-{task_id}-{CONDITION}"
    directory = experiment / "attempts" / attempt_id
    workspace = directory / "workspace"
    if directory.exists():
        raise RuntimeError(f"incomplete attempt directory already exists: {directory}")
    workspace.mkdir(parents=True)
    prompt = _prompt(str(task["brief"]))
    (directory / "solver-prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    environment = _credential_scrubbed_environment()
    command = [
        str(agent),
        "-a",
        "never",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "-m",
        model,
        "-s",
        "workspace-write",
        "-C",
        str(workspace),
        "-c",
        f'model_reasoning_effort="{effort}"',
        "--",
        prompt,
    ]
    started = time.monotonic()
    with _isolated_godot_runtime(godot_source) as (isolated_godot, runtime_root):
        tools = directory / "model-tools"
        private_home = tools / "home"
        private_home.mkdir(parents=True)
        wrapper = tools / "godot"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"export HOME={shlex.quote(str(private_home))}\n"
            f"export CFFIXED_USER_HOME={shlex.quote(str(private_home))}\n"
            f"exec {shlex.quote(sys.executable)} -m gameforge.harness.process_mutex "
            f"--lock-file {shlex.quote(str(GODOT_LOCK))} "
            f"--executable {shlex.quote(str(isolated_godot))} --crash-attempts 3 "
            f"--private-home-root {shlex.quote(str(private_home))} -- \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        environment["PYTHONPATH"] = str(candidate_source)
        environment.update(
            login_shell_environment(
                tools,
                exported_variables={
                    "GODOT": str(wrapper),
                    "GAMEFORGE_MINIMAL_OPEN": "1",
                },
                inherited_path=environment.get("PATH", ""),
            )
        )
        _write_json(
            directory / "model-runtime.json",
            {
                "schema_version": 1,
                "condition": CONDITION,
                "source_ordinal": int(attempt["source_ordinal"]),
                "command_resolution": "task-owned-login-shell-environment",
                "wrapper": str(wrapper),
                "isolated_godot": str(isolated_godot),
                "source_godot": str(godot_source),
                "source_godot_sha256": _sha256_file(godot_source),
                "shell_environment_module": str(
                    (candidate_source / SHELL_ENVIRONMENT_MODULE).resolve(strict=True)
                ),
                "shell_environment_module_sha256": _sha256_file(
                    (candidate_source / SHELL_ENVIRONMENT_MODULE).resolve(strict=True)
                ),
                "freedom_boundary": (
                    "resolution-only; model selects commands, argv, order, and workflow"
                ),
            },
        )
        try:
            completed = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            timed_out = False
        except subprocess.TimeoutExpired as error:
            return_code = 124
            stdout = error.stdout or ""
            stderr = error.stderr or ""
            timed_out = True
    duration = round(time.monotonic() - started, 3)
    (directory / "solver.jsonl").write_text(stdout, encoding="utf-8")
    (directory / "solver.stderr.log").write_text(stderr, encoding="utf-8")
    agent_record = _parse_agent_jsonl(stdout)
    if return_code != 0 and agent_record["thread_id"] is None:
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no stderr"
        raise RuntimeError(
            "agent CLI exited before starting a model turn "
            f"(return code {return_code}): {detail}"
        )
    evaluation = _evaluate_project(workspace, directory / "evaluation", godot_source)
    return {
        "schema_version": 1,
        "ordinal": ordinal,
        "source_ordinal": int(attempt["source_ordinal"]),
        "attempt_id": attempt_id,
        "task_id": task_id,
        "genre": task["genre"],
        "condition": CONDITION,
        "solver_return_code": return_code,
        "solver_timed_out": timed_out,
        "duration_seconds": duration,
        "agent": agent_record,
        "evaluation": evaluation,
        "workspace": str(workspace),
        "solver_log": str(directory / "solver.jsonl"),
        "solver_log_sha256": _sha256_file(directory / "solver.jsonl"),
        "model_runtime": str(directory / "model-runtime.json"),
        "model_runtime_sha256": _sha256_file(directory / "model-runtime.json"),
    }


def main() -> int:
    arguments = _arguments()
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    experiment = arguments.experiment_directory.resolve()
    candidate_source = arguments.candidate_source.resolve(strict=True)
    agent = arguments.agent_executable.resolve(strict=True)
    godot = arguments.godot_executable.resolve(strict=True)
    probe = PROBE.resolve(strict=True)
    schedule = _candidate_schedule(manifest)
    tasks = {str(task["task_id"]): task for task in manifest["tasks"]}
    if len(tasks) != 20 or {item["task_id"] for item in schedule} != set(tasks):
        raise ValueError("candidate schedule differs from the frozen twenty-task suite")
    candidate_digest, candidate_file_count = _runtime_digest(candidate_source)
    shell_module = (candidate_source / SHELL_ENVIRONMENT_MODULE).resolve(strict=True)
    login_shell_environment = _load_shell_environment(candidate_source)
    protocol = {
        "schema_version": 1,
        "design": {
            "type": "same-task historical comparator extension",
            "task_count": 20,
            "condition": CONDITION,
            "parent_condition": PARENT_CONDITION,
            "task_order": "original manifest parent-condition order",
            "repetitions_per_task": 1,
            "interpretation": (
                "Tests a single general executable-resolution change on the identical "
                "twenty briefs; historical comparisons remain single-run estimates."
            ),
        },
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": _sha256_file(manifest_path),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "probe": str(probe),
        "probe_sha256": _sha256_file(probe),
        "candidate_source": str(candidate_source),
        "candidate_runtime_tree_sha256": candidate_digest,
        "candidate_runtime_file_count": candidate_file_count,
        "shell_environment_module": str(shell_module),
        "shell_environment_module_sha256": _sha256_file(shell_module),
        "parent_runtime_tree_sha256": (
            "acc20d71db1ff4dbae5ada04052ae62e450140ef5a9f59d59699af19e1dbca84"
        ),
        "agent_executable": str(agent),
        "agent_executable_sha256": _sha256_file(agent),
        "godot_executable": str(godot),
        "godot_executable_sha256": _sha256_file(godot),
        "model": manifest["execution"]["model"],
        "reasoning_effort": manifest["execution"]["reasoning_effort"],
        "solver_timeout_seconds": manifest["execution"]["solver_timeout_seconds"],
        "parallelism": 1,
        "condition": {
            "name": CONDITION,
            "prompt": "unchanged minimal-open free-work prompt",
            "sandbox": "workspace-write",
            "ephemeral": True,
            "godot": (
                "per-attempt self-contained app clone through unchanged process mutex"
            ),
            "only_change": (
                "generic task-owned shell startup environment preserves shim resolution "
                "through child login shells"
            ),
            "not_added": [
                "task routing",
                "automatic tool invocation",
                "automatic repair",
                "argument policy",
                "fixed workflow",
                "host broker",
            ],
        },
        "schedule": schedule,
    }
    experiment.mkdir(parents=True, exist_ok=True)
    _write_json(experiment / "protocol-freeze.json", protocol)
    _progress(experiment, len(schedule))
    if arguments.prepare_only:
        print(experiment / "protocol-freeze.json")
        return 0

    completed_ordinals = {
        int(json.loads(path.read_text(encoding="utf-8"))["ordinal"])
        for path in (experiment / "receipts").glob("*.json")
    }
    pending = [item for item in schedule if int(item["ordinal"]) not in completed_ordinals]
    if arguments.max_new_attempts is not None:
        if arguments.max_new_attempts < 0:
            raise ValueError("max-new-attempts must be nonnegative")
        pending = pending[: arguments.max_new_attempts]
    for attempt in pending:
        ordinal = int(attempt["ordinal"])
        receipt_path = experiment / "receipts" / f"{ordinal:02d}.json"
        try:
            receipt = _run_attempt(
                experiment=experiment,
                attempt=attempt,
                task=tasks[str(attempt["task_id"])],
                model=str(manifest["execution"]["model"]),
                effort=str(manifest["execution"]["reasoning_effort"]),
                timeout_seconds=int(manifest["execution"]["solver_timeout_seconds"]),
                agent=agent,
                godot_source=godot,
                candidate_source=candidate_source,
                login_shell_environment=login_shell_environment,
            )
        except Exception as error:
            receipt = {
                "schema_version": 1,
                "ordinal": ordinal,
                "source_ordinal": int(attempt["source_ordinal"]),
                "attempt_id": f"{ordinal:02d}-{attempt['task_id']}-{CONDITION}",
                "task_id": attempt["task_id"],
                "condition": CONDITION,
                "infrastructure_error": f"{type(error).__name__}: {error}",
            }
        _write_json(receipt_path, receipt)
        _progress(experiment, len(schedule))
        status = receipt.get("evaluation", {}).get("hard_gate", "INFRASTRUCTURE_ERROR")
        crashes = receipt.get("agent", {}).get("command_crash_exit_count", "-")
        print(
            f"[{ordinal:02d}/{len(schedule):02d}] {attempt['task_id']} -> {status}; "
            f"model command crashes={crashes}",
            flush=True,
        )
    receipt_count = len(tuple((experiment / "receipts").glob("*.json")))
    if receipt_count == len(schedule):
        _write_json(
            experiment / "complete.json",
            {
                "schema_version": 1,
                "attempts_completed": receipt_count,
                "completed_at": datetime.now(UTC).isoformat(),
                "source_manifest_sha256": protocol["source_manifest_sha256"],
                "candidate_runtime_tree_sha256": candidate_digest,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
