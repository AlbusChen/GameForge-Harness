#!/usr/bin/env python3
"""Run the paired six-genre open Godot creation proxy."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/godot-open-game-creation6-v1.json"
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/godot-open-game-creation6-v1-run1"
DEFAULT_BASELINE = ROOT
DEFAULT_AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
DEFAULT_GODOT = Path("/private/tmp/godot-4.4.1/Godot.app/Contents/MacOS/Godot")
PROBE = ROOT / "experiments/assets/open_game_probe.gd"
GODOT_LOCK = Path(tempfile.gettempdir()) / "gameforge-godot-process-v1.lock"
TEXT_SUFFIXES = {".gd", ".tscn", ".tres", ".godot", ".cfg", ".json", ".md", ".txt"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--experiment-directory", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--baseline-version", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--agent-executable", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--godot-executable", type=Path, default=DEFAULT_GODOT)
    parser.add_argument("--max-new-attempts", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise RuntimeError(f"refusing to replace different frozen JSON: {path}")
    if not path.exists():
        path.write_text(encoded, encoding="utf-8")


def _replace_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _credential_scrubbed_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in {"GH_TOKEN", "GITHUB_TOKEN"} or upper.endswith(
            ("_API_KEY", "_ACCESS_TOKEN")
        ):
            continue
        environment[name] = value
    return environment


def _prompt(brief: str, condition: str) -> str:
    if condition == "baseline-minimal-open":
        return (
            "Implement the following request in the current disposable workspace. Work freely "
            "and use any available local tools as you see fit; an isolated Godot executable is "
            "available as `godot`.\n\nREQUEST:\n" + brief
        )
    if condition == "official-local":
        return (
            brief
            + "\n You must complete the full task without any further assistance."
            + "\n Godot is installed and you can run godot using the `godot` command. It is "
            "recommended to run this with a timeout (e.g., `timeout 10 godot` for 10 second "
            "timeout) to prevent hanging."
            + "You are a visual agent and can use images and videos to help you understand the "
            "state of the game."
        )
    raise ValueError(f"unknown condition: {condition}")


@contextlib.contextmanager
def _isolated_godot_runtime(source: Path) -> Iterator[tuple[Path, Path]]:
    source = source.resolve(strict=True)
    if os.uname().sysname != "Darwin":
        yield source, source.parent
        return
    with tempfile.TemporaryDirectory(prefix="open-game-godot-runtime-") as directory:
        root = Path(directory)
        source_app = next((parent for parent in source.parents if parent.suffix == ".app"), None)
        if source_app is None:
            executable = root / source.name
            shutil.copy2(source, executable)
        else:
            destination_app = root / source_app.name
            cloned = subprocess.run(
                ["cp", "-cR", str(source_app), str(destination_app)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if cloned.returncode != 0:
                copied = subprocess.run(
                    ["cp", "-R", str(source_app), str(destination_app)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if copied.returncode != 0:
                    raise OSError((copied.stderr or cloned.stderr).strip())
            executable = destination_app / source.relative_to(source_app)
        (root / "._sc_").touch()
        (root / "editor_data").mkdir()
        (root / "home").mkdir()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise OSError(f"isolated Godot executable is unavailable: {executable}")
        yield executable, root


def _parse_agent_jsonl(payload: str) -> dict[str, Any]:
    final_message = ""
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    command_count = 0
    tool_calls = 0
    command_exit_codes: Counter[int] = Counter()
    thread_id: str | None = None
    for raw_line in payload.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "agent_message" and isinstance(item.get("text"), str):
                final_message = item["text"]
            if item_type in {"command_execution", "file_change", "mcp_tool_call"}:
                tool_calls += 1
            if item_type == "command_execution":
                command_count += 1
                code = item.get("exit_code")
                if isinstance(code, int):
                    command_exit_codes[code] += 1
        raw_usage = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(raw_usage, dict):
            for key in usage:
                value = raw_usage.get(key, 0)
                usage[key] = int(value) if isinstance(value, (int, float)) else 0
    return {
        "thread_id": thread_id,
        "final_message": final_message,
        "usage": usage,
        "tool_calls": tool_calls,
        "command_count": command_count,
        "command_exit_codes": {
            str(key): value for key, value in sorted(command_exit_codes.items())
        },
        "command_crash_exit_count": sum(
            value for key, value in command_exit_codes.items() if key in {134, 139, -6, -11}
        ),
    }


def _source_metrics(workspace: Path) -> dict[str, Any]:
    files = [
        path
        for path in workspace.rglob("*")
        if path.is_file() and ".godot" not in path.relative_to(workspace).parts
    ]
    text_files = [path for path in files if path.suffix.lower() in TEXT_SUFFIXES]
    lines = 0
    nodes = 0
    scripts = 0
    scenes = 0
    for path in text_files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines += len(content.splitlines())
        nodes += content.count("[node ") if path.suffix.lower() == ".tscn" else 0
        scripts += path.suffix.lower() == ".gd"
        scenes += path.suffix.lower() == ".tscn"
    return {
        "public_file_count": len(files),
        "text_file_count": len(text_files),
        "text_line_count": lines,
        "script_count": scripts,
        "scene_count": scenes,
        "serialized_node_count": nodes,
    }


def _run_engine(command: list[str], environment: dict[str, str], timeout: float) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for _attempt in range(3):
        try:
            completed = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            record = {
                "return_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as error:
            record = {
                "return_code": 124,
                "stdout": error.stdout or "",
                "stderr": error.stderr or "",
                "timed_out": True,
            }
        attempts.append(record)
        if record["return_code"] not in {134, 139, -6, -11}:
            break
    final = attempts[-1]
    return {
        "attempt_count": len(attempts),
        "attempt_return_codes": [item["return_code"] for item in attempts],
        "return_code": final["return_code"],
        "stdout": final["stdout"],
        "stderr": final["stderr"],
        "timed_out": final["timed_out"],
    }


def _image_difference(initial: Path, final: Path) -> dict[str, Any] | None:
    if not initial.is_file() or not final.is_file():
        return None
    with Image.open(initial).convert("RGB") as before, Image.open(final).convert("RGB") as after:
        if before.size != after.size:
            return {"same_size": False, "initial_size": before.size, "final_size": after.size}
        difference = ImageChops.difference(before, after)
        histogram = difference.convert("L").histogram()
        changed = sum(histogram[1:])
        pixels = before.width * before.height
        rms = ImageStat.Stat(difference).rms
        return {
            "same_size": True,
            "size": list(before.size),
            "changed_pixel_fraction": round(changed / pixels, 6) if pixels else 0.0,
            "channel_rms": [round(value, 4) for value in rms],
        }


def _evaluate_project(workspace: Path, artifact: Path, godot_source: Path) -> dict[str, Any]:
    artifact.mkdir(parents=True, exist_ok=True)
    project_file = workspace / "project.godot"
    metrics = _source_metrics(workspace)
    if not project_file.is_file():
        return {
            "hard_gate": "FAIL",
            "reason": "project.godot is missing",
            "source_metrics": metrics,
            "import": None,
            "probe": None,
            "image_difference": None,
        }
    initial = artifact / "initial.png"
    final = artifact / "post-input.png"
    metadata_path = artifact / "probe-metadata.json"
    with _isolated_godot_runtime(godot_source) as (godot, runtime_root):
        environment = _credential_scrubbed_environment()
        environment["HOME"] = str(runtime_root / "home")
        environment["CFFIXED_USER_HOME"] = str(runtime_root / "home")
        import_record = _run_engine(
            [
                str(godot),
                "--headless",
                "--import",
                "--quit",
                "--path",
                str(workspace),
                "--log-file",
                str(artifact / "import.log"),
            ],
            environment,
            180,
        )
        (artifact / "import-process.log").write_text(
            str(import_record["stdout"]) + str(import_record["stderr"]),
            encoding="utf-8",
        )
        probe_record: dict[str, Any] | None = None
        if import_record["return_code"] == 0:
            probe_record = _run_engine(
                [
                    str(godot),
                    "--path",
                    str(workspace),
                    "--script",
                    str(PROBE),
                    "--display-driver",
                    "macos",
                    "--rendering-method",
                    "gl_compatibility",
                    "--audio-driver",
                    "Dummy",
                    "--windowed",
                    "--resolution",
                    "1280x720",
                    "--position",
                    "0,0",
                    "--log-file",
                    str(artifact / "probe.log"),
                    "--",
                    str(initial),
                    str(final),
                    str(metadata_path),
                ],
                environment,
                180,
            )
            (artifact / "probe-process.log").write_text(
                str(probe_record["stdout"]) + str(probe_record["stderr"]),
                encoding="utf-8",
            )
    metadata: object | None = None
    if metadata_path.is_file() and metadata_path.stat().st_size <= 4 * 1024 * 1024:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            metadata = None
    probe_ok = (
        probe_record is not None
        and probe_record["return_code"] == 0
        and initial.is_file()
        and initial.stat().st_size > 0
        and final.is_file()
        and final.stat().st_size > 0
        and isinstance(metadata, dict)
    )
    hard_gate = import_record["return_code"] == 0 and probe_ok
    return {
        "hard_gate": "PASS" if hard_gate else "FAIL",
        "reason": None if hard_gate else "import or generic runtime probe failed",
        "source_metrics": metrics,
        "import": {
            key: value
            for key, value in import_record.items()
            if key not in {"stdout", "stderr"}
        },
        "probe": (
            {
                **{
                    key: value
                    for key, value in probe_record.items()
                    if key not in {"stdout", "stderr"}
                },
                "metadata": metadata,
            }
            if probe_record is not None
            else None
        ),
        "image_difference": _image_difference(initial, final),
        "initial_screenshot": str(initial) if initial.is_file() else None,
        "post_input_screenshot": str(final) if final.is_file() else None,
    }


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
    baseline_source: Path,
) -> dict[str, Any]:
    ordinal = int(attempt["ordinal"])
    task_id = str(attempt["task_id"])
    condition = str(attempt["condition"])
    attempt_id = f"{ordinal:02d}-{task_id}-{condition}"
    directory = experiment / "attempts" / attempt_id
    workspace = directory / "workspace"
    if directory.exists():
        raise RuntimeError(f"incomplete attempt directory already exists: {directory}")
    workspace.mkdir(parents=True)
    prompt = _prompt(str(task["brief"]), condition)
    (directory / "solver-prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    environment = _credential_scrubbed_environment()
    command = [str(agent), "-a", "never", "exec"]
    runtime_context: contextlib.AbstractContextManager[tuple[Path, Path] | None]
    if condition == "baseline-minimal-open":
        command.append("--ephemeral")
        runtime_context = _isolated_godot_runtime(godot_source)
    else:
        runtime_context = contextlib.nullcontext(None)
    command.extend(
        [
            "--skip-git-repo-check",
            "--json",
            "-m",
            model,
            "-s",
            "workspace-write" if condition == "baseline-minimal-open" else "danger-full-access",
            "-C",
            str(workspace),
            "-c",
            f'model_reasoning_effort="{effort}"',
            "--",
            prompt,
        ]
    )
    started = time.monotonic()
    with runtime_context as runtime:
        if condition == "baseline-minimal-open":
            assert runtime is not None
            isolated_godot, runtime_root = runtime
            tools = directory / "model-tools"
            private_home = tools / "home"
            private_home.mkdir(parents=True)
            wrapper = tools / "godot"
            wrapper.write_text(
                "#!/bin/sh\n"
                f"export HOME={private_home}\n"
                f"export CFFIXED_USER_HOME={private_home}\n"
                f"exec {os.sys.executable} -m gameforge.harness.process_mutex "
                f"--lock-file {GODOT_LOCK} --executable {isolated_godot} --crash-attempts 3 "
                f"--private-home-root {private_home} -- \"$@\"\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            environment["PATH"] = f"{tools}{os.pathsep}{environment.get('PATH', '')}"
            environment["PYTHONPATH"] = str(baseline_source)
            environment["GODOT"] = str(wrapper)
            environment["GAMEFORGE_MINIMAL_OPEN"] = "1"
            environment["HOME"] = environment.get("HOME", str(runtime_root / "home"))
        else:
            godot_bin = directory / "official-tools"
            godot_bin.mkdir()
            wrapper = godot_bin / "godot"
            wrapper.symlink_to(godot_source)
            environment["PATH"] = f"{godot_bin}{os.pathsep}{environment.get('PATH', '')}"
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
    evaluation = _evaluate_project(workspace, directory / "evaluation", godot_source)
    receipt = {
        "schema_version": 1,
        "ordinal": ordinal,
        "attempt_id": attempt_id,
        "task_id": task_id,
        "genre": task["genre"],
        "condition": condition,
        "solver_return_code": return_code,
        "solver_timed_out": timed_out,
        "duration_seconds": duration,
        "agent": agent_record,
        "evaluation": evaluation,
        "workspace": str(workspace),
        "solver_log": str(directory / "solver.jsonl"),
        "solver_log_sha256": _sha256_file(directory / "solver.jsonl"),
    }
    return receipt


def _progress(experiment: Path, attempts: int) -> None:
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "attempts_completed": len(rows),
            "attempts_total": attempts,
            "hard_gate_counts": dict(
                sorted(Counter(row["evaluation"]["hard_gate"] for row in rows).items())
            ),
            "solver_return_code_counts": dict(
                sorted(Counter(str(row["solver_return_code"]) for row in rows).items())
            ),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    experiment = arguments.experiment_directory.resolve()
    baseline_version = arguments.baseline_version.resolve(strict=True)
    source_candidate = baseline_version / "source"
    baseline_source = (
        source_candidate if source_candidate.is_dir() else baseline_version
    ).resolve(strict=True)
    agent = arguments.agent_executable.resolve(strict=True)
    godot = arguments.godot_executable.resolve(strict=True)
    probe = PROBE.resolve(strict=True)
    metadata_path = baseline_version / "VERSION.json"
    if not metadata_path.is_file():
        metadata_path = baseline_version / "RELEASE.json"
    version = json.loads(metadata_path.read_text(encoding="utf-8"))
    schedule = manifest["schedule"]
    tasks = {str(task["task_id"]): task for task in manifest["tasks"]}
    if len(schedule) != 12 or len(tasks) != 6:
        raise ValueError("open creation v1 requires six tasks and twelve paired attempts")
    if [int(item["ordinal"]) for item in schedule] != list(range(1, 13)):
        raise ValueError("schedule ordinals must be exact and contiguous")
    for task_id in tasks:
        conditions = [item["condition"] for item in schedule if item["task_id"] == task_id]
        if sorted(conditions) != ["baseline-minimal-open", "official-local"]:
            raise ValueError(f"task is not paired exactly once: {task_id}")
    protocol = {
        "schema_version": 1,
        "design": manifest["design"],
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "probe": str(probe),
        "probe_sha256": _sha256_file(probe),
        "baseline_version": baseline_version.name,
        "baseline_legacy_version": version["version"],
        "baseline_runtime_tree_sha256": version["runtime_tree_sha256"],
        "baseline_runtime_file_count": version["runtime_file_count"],
        "baseline_source": str(baseline_source),
        "agent_executable": str(agent),
        "agent_executable_sha256": _sha256_file(agent),
        "godot_executable": str(godot),
        "godot_executable_sha256": _sha256_file(godot),
        "model": manifest["execution"]["model"],
        "reasoning_effort": manifest["execution"]["reasoning_effort"],
        "solver_timeout_seconds": manifest["execution"]["solver_timeout_seconds"],
        "parallelism": manifest["execution"]["parallelism"],
        "conditions": {
            "baseline-minimal-open": {
                "prompt": "minimal-open free-work prompt",
                "sandbox": "workspace-write",
                "ephemeral": True,
                "godot": "per-attempt self-contained app clone through baseline process mutex",
            },
            "official-local": {
                "prompt": "public GameDevBench Codex prompt suffix",
                "sandbox": "danger-full-access",
                "ephemeral": False,
                "godot": "same pinned source executable directly on PATH",
            },
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
    parallelism = int(manifest["execution"]["parallelism"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {
            executor.submit(
                _run_attempt,
                experiment=experiment,
                attempt=attempt,
                task=tasks[str(attempt["task_id"])],
                model=str(manifest["execution"]["model"]),
                effort=str(manifest["execution"]["reasoning_effort"]),
                timeout_seconds=int(manifest["execution"]["solver_timeout_seconds"]),
                agent=agent,
                godot_source=godot,
                baseline_source=baseline_source,
            ): attempt
            for attempt in pending
        }
        for future in concurrent.futures.as_completed(futures):
            attempt = futures[future]
            ordinal = int(attempt["ordinal"])
            receipt_path = experiment / "receipts" / f"{ordinal:02d}.json"
            try:
                receipt = future.result()
            except Exception as error:
                receipt = {
                    "schema_version": 1,
                    "ordinal": ordinal,
                    "attempt_id": f"{ordinal:02d}-{attempt['task_id']}-{attempt['condition']}",
                    "task_id": attempt["task_id"],
                    "condition": attempt["condition"],
                    "infrastructure_error": f"{type(error).__name__}: {error}",
                }
            _write_json(receipt_path, receipt)
            _progress(experiment, len(schedule))
            status = receipt.get("evaluation", {}).get("hard_gate", "INFRASTRUCTURE_ERROR")
            print(
                f"[{ordinal:02d}/12] {attempt['task_id']} {attempt['condition']} -> {status}",
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
                "manifest_sha256": protocol["manifest_sha256"],
                "baseline_runtime_tree_sha256": protocol["baseline_runtime_tree_sha256"],
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
