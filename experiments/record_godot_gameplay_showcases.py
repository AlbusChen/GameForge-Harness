#!/usr/bin/env python3
"""Record reproducible gameplay clips from preserved Godot benchmark workspaces.

This is evaluation/showcase tooling, not part of the model-facing harness. It copies
each generated project, replays a frozen engine-level input trace, and records rendered
frames with Godot MovieWriter before encoding an MP4 with macOS AVFoundation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = ROOT / "runs/experiments/godot-open-game-creation20-native-open-minimal-run2"
DEFAULT_OUTPUT = ROOT / "runs/showcases/gameplay-showcase-2026-08-26/godot"
DEFAULT_GODOT = Path("/Applications/Godot.app/Contents/MacOS/Godot")
PROBE = ROOT / "experiments/assets/showcase_record_probe.gd"
ENCODER_SOURCE = ROOT / "experiments/tools/png_sequence_to_mp4.swift"
FPS = 30
WIDTH = 1280
HEIGHT = 720


def _wait(frames: int) -> dict[str, Any]:
    return {"type": "wait", "frames": frames}


def _key(key: str | list[str], frames: int = 2) -> dict[str, Any]:
    return {"type": "key", "keys": [key] if isinstance(key, str) else key, "frames": frames}


def _click(x: int, y: int) -> dict[str, Any]:
    return {"type": "mouse_click", "x": x, "y": y, "frames": 2}


SHOWCASES: dict[str, dict[str, Any]] = {
    "arena_survivor": {
        "source_ordinal": 2,
        "title": "Arena Survivor",
        "interaction": "movement, continuous fire, aiming, and dash",
        "actions": [
            {"type": "mouse_move", "x": 760, "y": 330},
            _key(["D", "SPACE"], 75),
            _key(["W", "SPACE"], 60),
            _key(["D", "SHIFT"], 20),
            _key(["A", "SPACE"], 75),
            _key(["S", "SPACE"], 45),
        ],
    },
    "tower_defense": {
        "source_ordinal": 4,
        "title": "Tower Defense",
        "interaction": "tower selection/placement, wave start, and speed control",
        "actions": [
            _click(1100, 233),
            _click(455, 578),
            _click(1100, 469),
            _click(678, 600),
            _click(1189, 629),
            _wait(90),
            _click(1067, 629),
            _wait(240),
        ],
    },
    "rhythm_game": {
        "source_ordinal": 7,
        "title": "Rhythm Game",
        "interaction": "start flow and four independent note lanes",
        "actions": [
            _key("ENTER"),
            _wait(50),
            *sum(([_key(key), _wait(8)] for key in ["D", "F", "J", "K"] * 10), []),
        ],
    },
    "local_coop_arena": {
        "source_ordinal": 12,
        "title": "Local Co-op Arena",
        "interaction": "mode selection, movement, combat pulse, and live enemies",
        "actions": [
            _key("1"),
            _wait(30),
            _key(["W", "D"], 55),
            _key("Q", 3),
            _wait(30),
            _key(["S", "A"], 65),
            _key("Q", 3),
            _wait(75),
        ],
    },
    "stealth_infiltration": {
        "source_ordinal": 13,
        "title": "Stealth Infiltration",
        "interaction": "movement, crouching, guard simulation, and interaction",
        "actions": [
            _key("D", 75),
            _key(["D", "SHIFT"], 65),
            _key("S", 45),
            _key(["A", "SHIFT"], 55),
            _key(["W", "E"], 45),
            _wait(60),
        ],
    },
    "match_three": {
        "source_ordinal": 14,
        "title": "Match Three",
        "interaction": "mouse selection and multiple adjacent swaps",
        "actions": [
            _click(128, 175), _click(200, 175), _wait(30),
            _click(272, 247), _click(272, 319), _wait(30),
            _click(416, 391), _click(488, 391), _wait(30),
            _click(200, 535), _click(200, 607), _wait(45),
        ],
    },
    "fishing_challenge": {
        "source_ordinal": 15,
        "title": "Fishing Challenge",
        "interaction": "start, cast, hook timing, reel tension, and steering",
        "actions": [
            _key("SPACE"),
            _wait(20),
            _key("SPACE"),
            *sum(([_wait(12), _key("SPACE")] for _ in range(16)), []),
            _key(["SPACE", "D"], 90),
            _key("A", 35),
            _key(["SPACE", "A"], 90),
        ],
    },
    "brick_breaker": {
        "source_ordinal": 18,
        "title": "Brick Breaker",
        "interaction": "launch plus keyboard and mouse paddle control",
        "actions": [
            _key("SPACE"),
            _wait(30),
            _key("D", 90),
            {"type": "mouse_move", "x": 220, "y": 650, "frames": 30},
            _key("A", 90),
            {"type": "mouse_move", "x": 690, "y": 650, "frames": 30},
            _wait(90),
        ],
    },
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, default=SOURCE_RUN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--godot", type=Path, default=DEFAULT_GODOT)
    parser.add_argument(
        "--clone-runtime",
        action="store_true",
        help="Clone the app into the temporary directory instead of using a private HOME only.",
    )
    parser.add_argument(
        "--adhoc-sign-runtime",
        action="store_true",
        help="Ad-hoc sign a temporary app clone (useful for archived macOS Godot apps).",
    )
    parser.add_argument("--task", action="append", choices=sorted(SHOWCASES))
    parser.add_argument("--keep-frame-sequences", action="store_true")
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--list", action="store_true")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if ".godot" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(f"{relative}\0{_sha256(path)}\n".encode())
        count += 1
    return digest.hexdigest(), count


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _runtime_error_lines(payload: str) -> list[str]:
    return [
        line
        for line in payload.splitlines()
        if "SCRIPT ERROR:" in line or "ERROR:" in line
    ]


def _copy_project(source: Path, destination: Path) -> None:
    ignored = {".git", ".godot", "build", "builds", "Build", "Builds", "logs", "Logs"}

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in ignored}

    shutil.copytree(source, destination, ignore=ignore, copy_function=shutil.copy2)


def _prepare_runtime(
    source: Path,
    root: Path,
    *,
    clone_runtime: bool,
    adhoc_sign_runtime: bool,
) -> tuple[Path, dict[str, str]]:
    source = source.resolve(strict=True)
    if adhoc_sign_runtime and not clone_runtime:
        raise ValueError("--adhoc-sign-runtime requires --clone-runtime")
    source_app = next((parent for parent in source.parents if parent.suffix == ".app"), None)
    if not clone_runtime:
        executable = source
        mode = "installed-executable-private-home"
    elif source_app is None:
        executable = root / source.name
        shutil.copy2(source, executable)
        mode = "binary-copy"
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
            if destination_app.exists():
                shutil.rmtree(destination_app)
            copied = subprocess.run(
                ["cp", "-R", str(source_app), str(destination_app)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if copied.returncode != 0:
                raise RuntimeError((copied.stderr or cloned.stderr).strip())
        executable = destination_app / source.relative_to(source_app)
        mode = "macos-self-contained-app-clone"
        if adhoc_sign_runtime:
            signed = subprocess.run(
                [
                    "/usr/bin/codesign",
                    "--force",
                    "--deep",
                    "--sign",
                    "-",
                    "--timestamp=none",
                    str(destination_app),
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if signed.returncode != 0:
                raise RuntimeError(f"cannot ad-hoc sign Godot clone: {signed.stderr.strip()}")
            mode += "-adhoc-signed"
    (root / "._sc_").touch()
    (root / "editor_data").mkdir()
    private_home = root / "home"
    private_home.mkdir()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"isolated Godot executable is unavailable: {executable}")
    environment = dict(os.environ)
    environment["HOME"] = str(private_home)
    environment["CFFIXED_USER_HOME"] = str(private_home)
    environment["GODOT_SILENCE_ROOT_WARNING"] = "1"
    return executable, {
        "mode": mode,
        "environment_home": str(private_home),
        "adhoc_signed": str(adhoc_sign_runtime).lower(),
        **environment,
    }


def _compile_encoder(output: Path, module_cache: Path) -> None:
    completed = subprocess.run(
        [
            "/usr/bin/xcrun",
            "swiftc",
            "-O",
            "-module-cache-path",
            str(module_cache),
            str(ENCODER_SOURCE),
            "-o",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot compile video encoder:\n{completed.stderr}")


def _source_workspace(source_run: Path, ordinal: int, task_id: str) -> Path:
    candidates = sorted((source_run / "attempts").glob(f"{ordinal:02d}-{task_id}-*/workspace"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one workspace for {ordinal:02d}-{task_id}, found {candidates}"
        )
    return candidates[0]


def _source_receipt(source_run: Path, ordinal: int) -> Path:
    path = source_run / "receipts" / f"{ordinal:02d}.json"
    return path.resolve(strict=True)


def _record_one(
    *,
    task_id: str,
    specification: dict[str, Any],
    source_run: Path,
    output_root: Path,
    runtime: Path,
    runtime_environment: dict[str, str],
    source_godot: Path,
    encoder: Path,
    temporary_root: Path,
    keep_frame_sequences: bool,
) -> dict[str, Any]:
    ordinal = int(specification["source_ordinal"])
    source_workspace = _source_workspace(source_run, ordinal, task_id)
    source_receipt = _source_receipt(source_run, ordinal)
    project_digest, project_file_count = _tree_digest(source_workspace)
    destination = output_root / f"{ordinal:02d}-{task_id}"
    destination.mkdir(parents=True, exist_ok=True)
    temporary_project = temporary_root / f"project-{ordinal:02d}-{task_id}"
    frames = temporary_root / f"frames-{ordinal:02d}-{task_id}"
    frames.mkdir()
    _copy_project(source_workspace, temporary_project)

    trace = {
        "schema_version": 1,
        "showcase_id": f"godot-{task_id}",
        "task_id": task_id,
        "title": specification["title"],
        "interaction": specification["interaction"],
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "settle_frames": 30,
        "tail_frames": 60,
        "actions": specification["actions"],
    }
    trace_path = destination / "input-trace.json"
    _write_json(trace_path, trace)
    evidence_path = destination / "engine-evidence.json"
    initial_path = destination / "initial.png"
    final_path = destination / "final.png"
    movie_path = destination / "gameplay.mp4"
    log_path = destination / "engine.log"
    frame_pattern = frames / "frame.png"
    total_action_frames = sum(
        int(action.get("frames", 2)) + int(action.get("release_frames", 1))
        for action in trace["actions"]
    )
    quit_after = 30 + total_action_frames + 60 + 300
    command = [
        str(runtime),
        "--path",
        str(temporary_project),
        "--audio-driver",
        "Dummy",
        "--windowed",
        "--resolution",
        f"{WIDTH}x{HEIGHT}",
        "--write-movie",
        str(frame_pattern),
        "--fixed-fps",
        str(FPS),
        "--quit-after",
        str(quit_after),
        "--script",
        str(PROBE),
        "--",
        str(trace_path),
        str(evidence_path),
        str(initial_path),
        str(final_path),
    ]
    started_at = datetime.now(UTC)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=temporary_project,
        env=runtime_environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    duration = time.monotonic() - started
    log_path.write_text(
        "$ " + " ".join(command) + "\n\nSTDOUT\n" + completed.stdout
        + "\nSTDERR\n" + completed.stderr,
        encoding="utf-8",
    )
    frame_paths = sorted(frames.glob("*.png"))
    if completed.returncode == 0 and frame_paths:
        encoded = subprocess.run(
            [str(encoder), str(frames), str(movie_path), str(FPS)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        (destination / "encoder.log").write_text(
            "STDOUT\n" + encoded.stdout + "\nSTDERR\n" + encoded.stderr,
            encoding="utf-8",
        )
    else:
        encoded = subprocess.CompletedProcess([], 2, "", "engine recording failed")

    if frame_paths:
        samples = destination / "sample-frames"
        if samples.exists():
            shutil.rmtree(samples)
        samples.mkdir()
        sample_indexes = sorted(
            {0, len(frame_paths) // 3, (2 * len(frame_paths)) // 3, len(frame_paths) - 1}
        )
        for index in sample_indexes:
            shutil.copy2(frame_paths[index], samples / f"frame-{index:05d}.png")
    if keep_frame_sequences and frame_paths:
        shutil.copytree(frames, destination / "all-frames", dirs_exist_ok=True)

    engine_evidence: dict[str, Any] = {}
    if evidence_path.is_file():
        engine_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    distinct_frame_hashes = len({_sha256(path) for path in frame_paths})
    action_results = engine_evidence.get("actions", [])
    action_passes = sum(item.get("status") == "PASS" for item in action_results)
    expected_actions = len(trace["actions"])
    runtime_error_lines = _runtime_error_lines(completed.stderr)
    status = (
        "PASS"
        if completed.returncode == 0
        and encoded.returncode == 0
        and movie_path.is_file()
        and len(frame_paths) >= 90
        and distinct_frame_hashes > 1
        and action_passes == expected_actions
        and not runtime_error_lines
        else "FAIL"
    )
    receipt = {
        "schema_version": 1,
        "status": status,
        "evidence_grade": "engine-input-replay",
        "claim_boundary": (
            "Proves the preserved project launches, renders, receives the listed normal "
            "Godot InputEvent trace, and changes over time; it does not claim level completion."
        ),
        "task_id": task_id,
        "title": specification["title"],
        "interaction": specification["interaction"],
        "source": {
            "benchmark_run": str(source_run),
            "workspace": str(source_workspace),
            "workspace_tree_sha256": project_digest,
            "workspace_file_count": project_file_count,
            "receipt": str(source_receipt),
            "receipt_sha256": _sha256(source_receipt),
        },
        "runtime": {
            "source_godot": str(source_godot),
            "source_godot_sha256": _sha256(source_godot),
            "isolated_executable": str(runtime),
            "prepared_godot_sha256": _sha256(runtime),
            "isolation_mode": runtime_environment["mode"],
            "adhoc_signed": runtime_environment["adhoc_signed"] == "true",
            "probe": str(PROBE),
            "probe_sha256": _sha256(PROBE),
        },
        "recording": {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "wall_seconds": round(duration, 3),
            "engine_return_code": completed.returncode,
            "encoder_return_code": encoded.returncode,
            "fps": FPS,
            "frame_count": len(frame_paths),
            "distinct_frame_sha256_count": distinct_frame_hashes,
            "duration_seconds": round(len(frame_paths) / FPS, 3),
            "expected_action_count": expected_actions,
            "passed_action_count": action_passes,
            "runtime_error_count": len(runtime_error_lines),
            "runtime_error_lines": runtime_error_lines[:20],
            "trace": str(trace_path),
            "trace_sha256": _sha256(trace_path),
            "movie": str(movie_path),
            "movie_sha256": _sha256(movie_path) if movie_path.is_file() else None,
            "movie_bytes": movie_path.stat().st_size if movie_path.is_file() else 0,
            "initial_sha256": _sha256(initial_path) if initial_path.is_file() else None,
            "final_sha256": _sha256(final_path) if final_path.is_file() else None,
        },
    }
    _write_json(destination / "recording-receipt.json", receipt)
    return receipt


def main() -> int:
    arguments = _arguments()
    if arguments.list:
        for task_id, specification in SHOWCASES.items():
            print(f"{task_id}\t{specification['interaction']}")
        return 0
    source_run = arguments.source_run.resolve(strict=True)
    source_godot = arguments.godot.resolve(strict=True)
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    selected = arguments.task or list(SHOWCASES)
    results: list[dict[str, Any]] = []
    if not arguments.summarize_only:
        with tempfile.TemporaryDirectory(prefix="gameforge-gameplay-showcase-") as directory:
            temporary_root = Path(directory)
            runtime_root = temporary_root / "runtime"
            runtime_root.mkdir()
            runtime, runtime_environment = _prepare_runtime(
                source_godot,
                runtime_root,
                clone_runtime=arguments.clone_runtime,
                adhoc_sign_runtime=arguments.adhoc_sign_runtime,
            )
            encoder = temporary_root / "png-sequence-to-mp4"
            module_cache = temporary_root / "swift-module-cache"
            module_cache.mkdir()
            _compile_encoder(encoder, module_cache)
            for task_id in selected:
                print(f"recording {task_id}...", flush=True)
                result = _record_one(
                    task_id=task_id,
                    specification=SHOWCASES[task_id],
                    source_run=source_run,
                    output_root=output,
                    runtime=runtime,
                    runtime_environment=runtime_environment,
                    source_godot=source_godot,
                    encoder=encoder,
                    temporary_root=temporary_root,
                    keep_frame_sequences=arguments.keep_frame_sequences,
                )
                results.append(result)
                print(
                    f"{task_id}: {result['status']} "
                    f"({result['recording']['frame_count']} frames, "
                    f"{result['recording']['duration_seconds']}s)",
                    flush=True,
                )
    recorded_results: list[dict[str, Any]] = []
    for task_id, specification in SHOWCASES.items():
        ordinal = int(specification["source_ordinal"])
        receipt_path = output / f"{ordinal:02d}-{task_id}" / "recording-receipt.json"
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            log_path = receipt_path.parent / "engine.log"
            audited_errors = (
                _runtime_error_lines(log_path.read_text(encoding="utf-8", errors="replace"))
                if log_path.is_file()
                else ["ERROR: engine.log is missing"]
            )
            receipt["recording"]["audited_runtime_error_count"] = len(audited_errors)
            receipt["recording"]["audited_runtime_error_lines"] = audited_errors[:20]
            if audited_errors:
                receipt["status"] = "FAIL"
            recorded_results.append(receipt)
    summary = {
        "schema_version": 1,
        "status": (
            "PASS"
            if recorded_results
            and all(result["status"] == "PASS" for result in recorded_results)
            else "FAIL"
        ),
        "evidence_grade": "engine-input-replay",
        "source_run": str(source_run),
        "current_invocation_tasks": [] if arguments.summarize_only else selected,
        "recorded_tasks": [result["task_id"] for result in recorded_results],
        "passed": sum(result["status"] == "PASS" for result in recorded_results),
        "total": len(recorded_results),
        "results": recorded_results,
    }
    _write_json(output / "summary.json", summary)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
