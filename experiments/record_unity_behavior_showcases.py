#!/usr/bin/env python3
"""Record Unity project-authored behavior-smoke showcases with an independent recorder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = ROOT / "runs/experiments/unity-open-game-creation20-showcase-run3"
DEFAULT_OUTPUT = ROOT / "runs/showcases/gameplay-showcase-2026-08-26/unity-scripted"
DEFAULT_UNITY = Path("/Applications/Unity/Hub/Editor/6000.3.20f1/Unity.app/Contents/MacOS/Unity")
ENCODER_SOURCE = ROOT / "experiments/tools/png_sequence_to_mp4.swift"
FPS = 30

SHOWCASES: dict[str, dict[str, Any]] = {
    "stealth_infiltration": {
        "ordinal": 9,
        "title": "Stealth Infiltration",
        "flag": "-blacksiteSmokeCapture",
        "marker": "RUNTIME SMOKE PASSED",
        "frame_stride": 2,
        "minimum_frames": 20,
        "interaction": "project-authored runtime world/guard validation",
    },
    "roguelike_dungeon": {
        "ordinal": 12,
        "title": "Roguelike Dungeon",
        "flag": "-moonvaultSmoke",
        "marker": "MOONVAULT_SMOKE_OK",
        "frame_stride": 1,
        "minimum_frames": 8,
        "interaction": "project-authored turn movement and enemy simulation",
    },
    "deckbuilding_duel": {
        "ordinal": 17,
        "title": "Deckbuilding Duel",
        "flag": "-aetherSmoke",
        "marker": "AETHER_SMOKE_OK",
        "frame_stride": 1,
        "minimum_frames": 20,
        "interaction": "project-authored card play, resolution, and turn progression",
    },
    "local_coop_arena": {
        "ordinal": 20,
        "title": "Local Co-op Arena",
        "flag": "-starholdValidate",
        "marker": "STARHOLD_PLAYTEST PASS",
        "frame_stride": 2,
        "minimum_frames": 30,
        "interaction": "project-authored enemy spawning, downing, and ally revive",
    },
}

EDITOR_SCRIPT = r'''
using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;

namespace GameForgeGameplayRecorder
{
    public static class BuildEntry
    {
        public static void Build()
        {
            string output = Environment.GetEnvironmentVariable("GAMEFORGE_RECORDING_BUILD");
            string resultPath = Environment.GetEnvironmentVariable(
                "GAMEFORGE_RECORDING_BUILD_RESULT");
            if (String.IsNullOrWhiteSpace(output) || String.IsNullOrWhiteSpace(resultPath))
                throw new InvalidOperationException("recording build paths are missing");
            string[] scenes = EditorBuildSettings.scenes
                .Where(scene => scene.enabled && File.Exists(scene.path))
                .Select(scene => scene.path).ToArray();
            if (scenes.Length == 0)
                scenes = AssetDatabase.FindAssets("t:Scene", new[] { "Assets" })
                    .Select(AssetDatabase.GUIDToAssetPath)
                    .Where(path => path.EndsWith(".unity", StringComparison.OrdinalIgnoreCase))
                    .OrderBy(path => path, StringComparer.Ordinal).ToArray();
            if (scenes.Length == 0) throw new InvalidOperationException("no Unity scene found");
            Directory.CreateDirectory(Path.GetDirectoryName(output));
            BuildReport report = BuildPipeline.BuildPlayer(new BuildPlayerOptions {
                scenes = scenes,
                locationPathName = output,
                target = BuildTarget.StandaloneOSX,
                options = BuildOptions.Development
            });
            File.WriteAllText(resultPath, "{\n"
                + "  \"status\": \"" + report.summary.result + "\",\n"
                + "  \"totalBytes\": " + report.summary.totalSize + ",\n"
                + "  \"sceneCount\": " + scenes.Length + "\n}\n");
            if (report.summary.result != BuildResult.Succeeded)
                throw new InvalidOperationException(
                    "recording build failed: " + report.summary.result);
        }
    }
}
'''

RUNTIME_SCRIPT = r'''
using System;
using System.Collections;
using System.IO;
using UnityEngine;

namespace GameForgeGameplayRecorder
{
    public sealed class FrameRecorder : MonoBehaviour
    {
        string directory;
        int stride = 1, maximum = 240, captured;
        Texture2D texture;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        static void Install()
        {
            string output = ReadArgument("-gameforgeFrameDir");
            if (String.IsNullOrWhiteSpace(output)) return;
            var host = new GameObject("GameForge Independent Frame Recorder");
            DontDestroyOnLoad(host);
            host.AddComponent<FrameRecorder>();
        }

        void Awake()
        {
            directory = ReadArgument("-gameforgeFrameDir");
            Int32.TryParse(ReadArgument("-gameforgeFrameStride"), out stride);
            Int32.TryParse(ReadArgument("-gameforgeMaximumFrames"), out maximum);
            stride = Mathf.Max(1, stride); maximum = Mathf.Max(1, maximum);
            Directory.CreateDirectory(directory);
            Debug.Log("GAMEFORGE_FRAME_RECORDER_START stride=" + stride + " maximum=" + maximum);
            StartCoroutine(Record());
        }

        IEnumerator Record()
        {
            int observed = 0;
            while (captured < maximum)
            {
                yield return new WaitForEndOfFrame();
                observed++;
                if (observed % stride != 0) continue;
                if (texture == null || texture.width != Screen.width
                    || texture.height != Screen.height)
                {
                    if (texture != null) Destroy(texture);
                    texture = new Texture2D(
                        Screen.width, Screen.height, TextureFormat.RGB24, false);
                }
                texture.ReadPixels(new Rect(0, 0, Screen.width, Screen.height), 0, 0, false);
                texture.Apply(false, false);
                File.WriteAllBytes(
                    Path.Combine(directory, "frame-" + captured.ToString("D5") + ".png"),
                    texture.EncodeToPNG());
                captured++;
            }
            Debug.Log("GAMEFORGE_FRAME_RECORDER_LIMIT count=" + captured);
            WriteMetadata();
            Application.Quit(0);
        }

        void OnApplicationQuit() { WriteMetadata(); }

        void WriteMetadata()
        {
            if (String.IsNullOrWhiteSpace(directory)) return;
            File.WriteAllText(Path.Combine(directory, "metadata.json"), "{\n"
                + "  \"capturedFrames\": " + captured + ",\n"
                + "  \"stride\": " + stride + ",\n"
                + "  \"width\": " + Screen.width + ",\n"
                + "  \"height\": " + Screen.height + "\n}\n");
        }

        static string ReadArgument(string name)
        {
            string[] arguments = Environment.GetCommandLineArgs();
            for (int index = 0; index + 1 < arguments.Length; index++)
                if (arguments[index] == name) return arguments[index + 1];
            return "";
        }
    }
}
'''


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, default=SOURCE_RUN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--unity", type=Path, default=DEFAULT_UNITY)
    parser.add_argument("--task", action="append", choices=sorted(SHOWCASES))
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _public_tree_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    excluded = {".git", "Build", "Builds", "Library", "Logs", "Temp", "UserSettings", "obj"}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if any(part in excluded for part in path.relative_to(root).parts):
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


def _copy_project(source: Path, destination: Path) -> None:
    cloned = subprocess.run(
        ["cp", "-cR", str(source), str(destination)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if cloned.returncode != 0:
        shutil.copytree(source, destination, copy_function=shutil.copy2)
    for name in ("Build", "Builds", "Logs", "Temp", "UserSettings"):
        target = destination / name
        if target.is_dir():
            shutil.rmtree(target)


def _inject_recorder(project: Path) -> tuple[Path, Path]:
    runtime = project / "Assets/GameForgeGameplayRecorder/Runtime/FrameRecorder.cs"
    editor = project / "Assets/GameForgeGameplayRecorder/Editor/BuildEntry.cs"
    runtime.parent.mkdir(parents=True)
    editor.parent.mkdir(parents=True)
    runtime.write_text(RUNTIME_SCRIPT, encoding="utf-8")
    editor.write_text(EDITOR_SCRIPT, encoding="utf-8")
    return runtime, editor


def _compile_encoder(output: Path, module_cache: Path) -> None:
    completed = subprocess.run(
        [
            "/usr/bin/xcrun", "swiftc", "-O", "-module-cache-path", str(module_cache),
            str(ENCODER_SOURCE), "-o", str(output),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)


def _workspace_from_receipt(source_run: Path, ordinal: int, task_id: str) -> tuple[Path, Path]:
    receipt = source_run / "receipts" / f"{ordinal:02d}-{task_id}.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    return Path(payload["workspace"]).resolve(strict=True), receipt.resolve(strict=True)


def _player_executable(application: Path) -> Path:
    with (application / "Contents/Info.plist").open("rb") as handle:
        executable_name = plistlib.load(handle)["CFBundleExecutable"]
    return (application / "Contents/MacOS" / executable_name).resolve(strict=True)


def _error_lines(payload: str) -> list[str]:
    indicators = ("Exception:", "NullReferenceException", "IndexOutOfRangeException", "_FAIL")
    return [line for line in payload.splitlines() if any(item in line for item in indicators)]


def _record_one(
    *,
    task_id: str,
    specification: dict[str, Any],
    source_run: Path,
    output_root: Path,
    unity: Path,
    encoder: Path,
    temporary_root: Path,
) -> dict[str, Any]:
    ordinal = int(specification["ordinal"])
    source, source_receipt = _workspace_from_receipt(source_run, ordinal, task_id)
    tree_hash, public_files = _public_tree_digest(source)
    destination = output_root / f"{ordinal:02d}-{task_id}"
    if destination.exists():
        raise RuntimeError(f"refusing to replace existing Unity recording: {destination}")
    destination.mkdir(parents=True)
    project = temporary_root / f"project-{ordinal:02d}-{task_id}"
    _copy_project(source, project)
    runtime_script, editor_script = _inject_recorder(project)
    build = temporary_root / f"build-{ordinal:02d}-{task_id}" / "Showcase.app"
    build_result = destination / "build-result.json"
    build_log = destination / "build.log"
    environment = dict(os.environ)
    environment["GAMEFORGE_RECORDING_BUILD"] = str(build)
    environment["GAMEFORGE_RECORDING_BUILD_RESULT"] = str(build_result)
    build_started = time.monotonic()
    built = subprocess.run(
        [
            str(unity), "-batchmode", "-nographics", "-quit", "-projectPath", str(project),
            "-executeMethod", "GameForgeGameplayRecorder.BuildEntry.Build",
            "-logFile", str(build_log),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    build_seconds = time.monotonic() - build_started
    if built.returncode != 0 or not build.is_dir():
        raise RuntimeError(f"Unity recording build failed for {task_id}; see {build_log}")

    frames = temporary_root / f"frames-{ordinal:02d}-{task_id}"
    frames.mkdir()
    player_log = destination / "player.log"
    executable = _player_executable(build)
    command = [
        str(executable),
        specification["flag"],
        "-screen-fullscreen", "0",
        "-screen-width", "1280",
        "-screen-height", "720",
        "-gameforgeFrameDir", str(frames),
        "-gameforgeFrameStride", str(specification["frame_stride"]),
        "-gameforgeMaximumFrames", "240",
        "-logFile", str(player_log),
    ]
    started_at = datetime.now(UTC)
    started = time.monotonic()
    played = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    runtime_seconds = time.monotonic() - started
    frame_paths = sorted(frames.glob("frame-*.png"))
    movie = destination / "gameplay.mp4"
    encoded = subprocess.run(
        [str(encoder), str(frames), str(movie), str(FPS)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    ) if frame_paths else subprocess.CompletedProcess([], 2, "", "no frames")
    (destination / "encoder.log").write_text(
        "STDOUT\n" + encoded.stdout + "\nSTDERR\n" + encoded.stderr,
        encoding="utf-8",
    )
    samples = destination / "sample-frames"
    samples.mkdir()
    sample_indexes = sorted(
        {0, len(frame_paths) // 3, (2 * len(frame_paths)) // 3, len(frame_paths) - 1}
    )
    for index in sample_indexes:
        if frame_paths:
            shutil.copy2(frame_paths[index], samples / f"frame-{index:05d}.png")
    player_payload = (
        player_log.read_text(encoding="utf-8", errors="replace")
        if player_log.is_file()
        else ""
    )
    errors = _error_lines(player_payload)
    marker_found = specification["marker"] in player_payload
    distinct_frames = len({_sha256(path) for path in frame_paths})
    status = (
        "PASS"
        if played.returncode == 0
        and encoded.returncode == 0
        and marker_found
        and not errors
        and len(frame_paths) >= int(specification["minimum_frames"])
        and distinct_frames > 1
        else "FAIL"
    )
    receipt = {
        "schema_version": 1,
        "status": status,
        "evidence_grade": "project-authored-behavior-smoke-replay",
        "claim_boundary": (
            "The model-generated project's own smoke path advances gameplay state while an "
            "independent evaluator records rendered frames. This is not raw keyboard/mouse replay."
        ),
        "task_id": task_id,
        "title": specification["title"],
        "interaction": specification["interaction"],
        "source": {
            "benchmark_run": str(source_run),
            "workspace": str(source),
            "public_tree_sha256": tree_hash,
            "public_file_count": public_files,
            "receipt": str(source_receipt),
            "receipt_sha256": _sha256(source_receipt),
        },
        "evaluator": {
            "runtime_recorder_sha256": hashlib.sha256(RUNTIME_SCRIPT.encode()).hexdigest(),
            "editor_builder_sha256": hashlib.sha256(EDITOR_SCRIPT.encode()).hexdigest(),
            "runtime_script_in_temporary_copy": str(runtime_script),
            "editor_script_in_temporary_copy": str(editor_script),
            "unity": str(unity),
            "unity_sha256": _sha256(unity),
            "build_seconds": round(build_seconds, 3),
        },
        "recording": {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "wall_seconds": round(runtime_seconds, 3),
            "project_smoke_flag": specification["flag"],
            "expected_marker": specification["marker"],
            "marker_found": marker_found,
            "player_return_code": played.returncode,
            "encoder_return_code": encoded.returncode,
            "frame_count": len(frame_paths),
            "distinct_frame_sha256_count": distinct_frames,
            "fps": FPS,
            "video_seconds": round(len(frame_paths) / FPS, 3),
            "runtime_error_count": len(errors),
            "runtime_error_lines": errors[:20],
            "movie": str(movie),
            "movie_sha256": _sha256(movie) if movie.is_file() else None,
            "movie_bytes": movie.stat().st_size if movie.is_file() else 0,
        },
    }
    _write_json(destination / "recording-receipt.json", receipt)
    return receipt


def main() -> int:
    arguments = _arguments()
    source_run = arguments.source_run.resolve(strict=True)
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    unity = arguments.unity.resolve(strict=True)
    selected = arguments.task or list(SHOWCASES)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="gameforge-unity-gameplay-") as directory:
        temporary_root = Path(directory)
        encoder = temporary_root / "png-sequence-to-mp4"
        module_cache = temporary_root / "swift-module-cache"
        module_cache.mkdir()
        _compile_encoder(encoder, module_cache)
        for task_id in selected:
            specification = SHOWCASES[task_id]
            existing_receipt = (
                output
                / f"{int(specification['ordinal']):02d}-{task_id}"
                / "recording-receipt.json"
            )
            if existing_receipt.is_file():
                result = json.loads(existing_receipt.read_text(encoding="utf-8"))
                results.append(result)
                print(f"reusing Unity {task_id}: {result['status']}", flush=True)
                continue
            print(f"recording Unity {task_id}...", flush=True)
            result = _record_one(
                task_id=task_id,
                specification=specification,
                source_run=source_run,
                output_root=output,
                unity=unity,
                encoder=encoder,
                temporary_root=temporary_root,
            )
            results.append(result)
            print(
                f"{task_id}: {result['status']} "
                f"({result['recording']['frame_count']} frames)",
                flush=True,
            )
    summary = {
        "schema_version": 1,
        "status": "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL",
        "evidence_grade": "project-authored-behavior-smoke-replay",
        "passed": sum(item["status"] == "PASS" for item in results),
        "total": len(results),
        "results": results,
    }
    _write_json(output / "summary.json", summary)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
