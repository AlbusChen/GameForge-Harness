#!/usr/bin/env python3
"""Record one Unity Player with macOS-native input and external screen capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = (
    ROOT
    / "runs/game-open-workspaces/20260825T095021052806Z-unity20-17-deckbuilding_duel"
    / "project/Builds/AetherDuel.app"
)
DEFAULT_OUTPUT = (
    ROOT
    / "runs/showcases/gameplay-showcase-2026-08-26/unity-native-input"
    / "17-deckbuilding_duel-run1"
)
ENCODER_SOURCE = ROOT / "experiments/tools/png_sequence_to_mp4.swift"
INPUT_SOURCE = ROOT / "experiments/tools/macos_input_event.swift"
PROCESS_NAME = "Aether Duel"
CAPTURE_FPS = 10
CAPTURE_SECONDS = 12.0

# Coordinates are normalized to the complete macOS window rectangle. The y values account for
# the title bar; they are resolved only after System Events reports the actual window geometry.
TRACE = (
    {"at": 2.0, "kind": "click", "x": 0.664, "y": 0.735, "label": "play Pulse"},
    {"at": 4.0, "kind": "click", "x": 0.180, "y": 0.735, "label": "play Ward"},
    {"at": 6.0, "kind": "key", "key_code": 49, "label": "end turn with Space"},
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--process-name", default=PROCESS_NAME)
    parser.add_argument("--encoder", type=Path)
    parser.add_argument("--input-helper", type=Path)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _applescript(lines: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["/usr/bin/osascript"]
    for line in lines:
        command.extend(("-e", line))
    return subprocess.run(command, capture_output=True, text=True, check=check, timeout=15)


def _window_geometry(process_id: int) -> tuple[int, int, int, int]:
    completed = _applescript(
        [
            'tell application "System Events"',
            f"tell (first process whose unix id is {process_id})",
            "set frontmost to true",
            "set p to position of window 1",
            "set s to size of window 1",
            'return (item 1 of p as text) & "," & (item 2 of p as text) & "," & '
            '(item 1 of s as text) & "," & (item 2 of s as text)',
            "end tell",
            "end tell",
        ]
    )
    values = tuple(int(item) for item in completed.stdout.strip().split(","))
    if len(values) != 4 or values[2] < 320 or values[3] < 240:
        raise RuntimeError(f"unexpected Unity window geometry: {values}")
    return values


def _wait_for_window(process_id: int, timeout: float = 20.0) -> tuple[int, int, int, int]:
    deadline = time.monotonic() + timeout
    last_error = "window did not appear"
    while time.monotonic() < deadline:
        try:
            return _window_geometry(process_id)
        except (RuntimeError, subprocess.SubprocessError, ValueError) as error:
            last_error = str(error)
            time.sleep(0.25)
    raise RuntimeError(last_error)


def _player_executable(application: Path) -> Path:
    with (application / "Contents/Info.plist").open("rb") as handle:
        executable = plistlib.load(handle)["CFBundleExecutable"]
    return (application / "Contents/MacOS" / executable).resolve(strict=True)


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
        raise RuntimeError(completed.stderr)


def _compile_input_helper(output: Path, module_cache: Path) -> None:
    completed = subprocess.run(
        [
            "/usr/bin/xcrun",
            "swiftc",
            "-O",
            "-module-cache-path",
            str(module_cache),
            str(INPUT_SOURCE),
            "-o",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)


def _capture(rectangle: tuple[int, int, int, int], destination: Path) -> None:
    region = ",".join(str(item) for item in rectangle)
    completed = subprocess.run(
        ["/usr/sbin/screencapture", "-x", f"-R{region}", str(destination)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        raise RuntimeError(f"screen capture failed: {completed.stderr.strip()}")


def _perform_action(
    action: dict[str, Any],
    rectangle: tuple[int, int, int, int],
    process_id: int,
    input_helper: Path,
) -> dict[str, Any]:
    started = datetime.now(UTC).isoformat()
    if action["kind"] == "click":
        x = round(rectangle[0] + rectangle[2] * float(action["x"]))
        y = round(rectangle[1] + rectangle[3] * float(action["y"]))
        _applescript(
            [
                'tell application "System Events"',
                f"set frontmost of (first process whose unix id is {process_id}) to true",
                "end tell",
            ]
        )
        time.sleep(0.08)
        completed = subprocess.run(
            [str(input_helper), "click", str(x), str(y)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        resolved = {"x": x, "y": y}
    else:
        _applescript(
            [
                'tell application "System Events"',
                f"set frontmost of (first process whose unix id is {process_id}) to true",
                "end tell",
            ]
        )
        time.sleep(0.08)
        completed = subprocess.run(
            [str(input_helper), "key", str(int(action["key_code"]))],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        resolved = {"key_code": int(action["key_code"])}
    return {
        **action,
        "resolved": resolved,
        "executed_at": started,
        "return_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _process_ids(process_name: str) -> list[int]:
    escaped = process_name.replace('"', '\\"')
    completed = _applescript(
        [
            'tell application "System Events"',
            f'return unix id of every process whose name is "{escaped}"',
            "end tell",
        ],
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    return [int(item.strip()) for item in completed.stdout.split(",") if item.strip()]


def _terminate_existing(process_name: str) -> list[int]:
    process_ids = _process_ids(process_name)
    for process_id in process_ids:
        os.kill(process_id, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while process_ids and time.monotonic() < deadline:
        time.sleep(0.1)
        process_ids = _process_ids(process_name)
    if process_ids:
        raise RuntimeError(f"existing Unity processes did not terminate: {process_ids}")
    return process_ids


def _sample_frames(frames: list[Path], destination: Path) -> list[str]:
    destination.mkdir()
    indexes = sorted({0, len(frames) // 4, len(frames) // 2, 3 * len(frames) // 4, len(frames) - 1})
    outputs: list[str] = []
    for index in indexes:
        target = destination / frames[index].name
        shutil.copy2(frames[index], target)
        outputs.append(str(target))
    return outputs


def main() -> int:
    arguments = _arguments()
    application = arguments.app.resolve(strict=True)
    output = arguments.output.resolve()
    if output.exists():
        raise RuntimeError(f"refusing to replace existing recording: {output}")
    output.mkdir(parents=True)
    frames_directory = output / "raw-frames"
    frames_directory.mkdir()
    executable = _player_executable(application)
    player_log = output / "player.log"
    started_at = datetime.now(UTC)
    _terminate_existing(arguments.process_name)
    helper_temporary: tempfile.TemporaryDirectory[str] | None = None
    if arguments.input_helper:
        input_helper = arguments.input_helper.resolve(strict=True)
    else:
        helper_temporary = tempfile.TemporaryDirectory(prefix="gameforge-native-input-helper-")
        helper_root = Path(helper_temporary.name)
        input_helper = helper_root / "macos-input-event"
        helper_cache = helper_root / "module-cache"
        helper_cache.mkdir()
        _compile_input_helper(input_helper, helper_cache)
    process = subprocess.Popen(
        [
            str(executable),
            "-screen-fullscreen",
            "0",
            "-screen-width",
            "1280",
            "-screen-height",
            "720",
            "-logFile",
            str(player_log),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    executed_actions: list[dict[str, Any]] = []
    frames: list[Path] = []
    try:
        rectangle = _wait_for_window(process.pid)
        client_height = min(720, rectangle[3])
        capture_rectangle = (
            rectangle[0],
            rectangle[1] + rectangle[3] - client_height,
            rectangle[2],
            client_height,
        )
        print(
            f"window={rectangle} capture={capture_rectangle}",
            flush=True,
        )
        start = time.monotonic()
        next_frame_at = 0.0
        pending = list(TRACE)
        while True:
            elapsed = time.monotonic() - start
            while pending and elapsed >= float(pending[0]["at"]):
                executed_actions.append(
                    _perform_action(
                        pending.pop(0), rectangle, process.pid, input_helper
                    )
                )
            if elapsed >= next_frame_at:
                frame = frames_directory / f"frame-{len(frames):05d}.png"
                _capture(capture_rectangle, frame)
                frames.append(frame)
                next_frame_at += 1.0 / CAPTURE_FPS
            if elapsed >= CAPTURE_SECONDS:
                break
            time.sleep(0.01)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Unity Player {process.pid} did not terminate") from error

    if not frames:
        raise RuntimeError("no external screen frames were captured")
    if helper_temporary:
        helper_temporary.cleanup()
    print(f"captured={len(frames)} actions={len(executed_actions)}", flush=True)
    with tempfile.TemporaryDirectory(prefix="gameforge-native-input-encoder-") as directory:
        temporary = Path(directory)
        if arguments.encoder:
            encoder = arguments.encoder.resolve(strict=True)
        else:
            encoder = temporary / "png-sequence-to-mp4"
            module_cache = temporary / "module-cache"
            module_cache.mkdir()
            _compile_encoder(encoder, module_cache)
        movie = output / "gameplay.mp4"
        encoded = subprocess.run(
            [str(encoder), str(frames_directory), str(movie), str(CAPTURE_FPS)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    (output / "encoder.log").write_text(
        "STDOUT\n" + encoded.stdout + "\nSTDERR\n" + encoded.stderr
    )
    movie = output / "gameplay.mp4"
    distinct_frames = len({_sha256(frame) for frame in frames})
    samples = _sample_frames(frames, output / "sample-frames")
    player_payload = player_log.read_text(errors="replace") if player_log.is_file() else ""
    error_lines = [
        line
        for line in player_payload.splitlines()
        if "Exception:" in line or "Error" in line or "_FAIL" in line
    ]
    status = (
        "PASS"
        if encoded.returncode == 0
        and movie.is_file()
        and len(frames) >= 30
        and distinct_frames > 3
        and len(executed_actions) == len(TRACE)
        and all(action["return_code"] == 0 for action in executed_actions)
        and not error_lines
        else "FAIL"
    )
    receipt = {
        "schema_version": 1,
        "status": status,
        "evidence_grade": "macos-native-window-input-and-external-screen-capture",
        "claim_boundary": (
            "An unmodified generated Unity Player received operating-system mouse/keyboard "
            "events while an external evaluator captured its window. Semantic gameplay "
            "progress is recorded separately by manual visual review."
        ),
        "application": str(application),
        "application_executable": str(executable),
        "application_executable_sha256": _sha256(executable),
        "application_process_id": process.pid,
        "project_modified": False,
        "input_transport": "CoreGraphics CGEvent at cghidEventTap",
        "input_helper_source": str(INPUT_SOURCE),
        "input_helper_source_sha256": _sha256(INPUT_SOURCE),
        "capture_transport": "macOS screencapture window rectangle",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "window_rectangle": list(rectangle),
        "capture_rectangle": list(capture_rectangle),
        "actions": executed_actions,
        "recording": {
            "fps": CAPTURE_FPS,
            "duration_seconds": round(len(frames) / CAPTURE_FPS, 3),
            "frame_count": len(frames),
            "distinct_frame_sha256_count": distinct_frames,
            "encoder_return_code": encoded.returncode,
            "runtime_error_count": len(error_lines),
            "runtime_error_lines": error_lines[:20],
            "movie": str(movie),
            "movie_sha256": _sha256(movie) if movie.is_file() else None,
            "movie_bytes": movie.stat().st_size if movie.is_file() else 0,
            "sample_frames": samples,
        },
    }
    _write_json(output / "recording-receipt.json", receipt)
    print(json.dumps({"status": status, "output": str(output), "frames": len(frames)}))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
