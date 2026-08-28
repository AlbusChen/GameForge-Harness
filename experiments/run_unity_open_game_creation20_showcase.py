#!/usr/bin/env python3
"""Run the frozen Unity20 single-condition game showcase with resumable receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERSION = ROOT
DEFAULT_MANIFEST = ROOT / "benchmarks/unity-open-game-creation20-v1.json"
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/unity-open-game-creation20-showcase-run3"
DEFAULT_AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
DEFAULT_UNITY = Path(
    "/Applications/Unity/Hub/Editor/6000.3.20f1/Unity.app/Contents/MacOS/Unity"
)

_EDITOR_PROBE = r'''
using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace GameForgeShowcaseEvaluator
{
    public static class BuildEntry
    {
        public static void Build()
        {
            string output = Environment.GetEnvironmentVariable("GAMEFORGE_SHOWCASE_BUILD_PATH");
            string resultPath = Environment.GetEnvironmentVariable(
                "GAMEFORGE_SHOWCASE_BUILD_RESULT");
            if (String.IsNullOrWhiteSpace(output) || String.IsNullOrWhiteSpace(resultPath))
                throw new InvalidOperationException("showcase evaluator paths are missing");

            string[] scenes = EditorBuildSettings.scenes
                .Where(scene => scene.enabled && File.Exists(scene.path))
                .Select(scene => scene.path)
                .ToArray();
            if (scenes.Length == 0)
            {
                scenes = AssetDatabase.FindAssets("t:Scene", new[] { "Assets" })
                    .Select(AssetDatabase.GUIDToAssetPath)
                    .Where(path => path.EndsWith(".unity", StringComparison.OrdinalIgnoreCase))
                    .Where(path => !path.Contains("/Tests/"))
                    .OrderBy(path => path, StringComparer.Ordinal)
                    .ToArray();
            }
            if (scenes.Length == 0)
                throw new InvalidOperationException("no public Unity scene is discoverable");

            Directory.CreateDirectory(Path.GetDirectoryName(output));
            BuildReport report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = scenes,
                locationPathName = output,
                target = BuildTarget.StandaloneOSX,
                options = BuildOptions.Development
            });
            string sceneJson = String.Join(",", scenes.Select(
                scene => "\"" + Escape(scene) + "\""));
            string resultName = report.summary.result.ToString();
            string payload = "{\n"
                + "  \"status\": \"" + Escape(resultName) + "\",\n"
                + "  \"scenes\": [" + sceneJson + "],\n"
                + "  \"output\": \"" + Escape(output) + "\",\n"
                + "  \"summary\": \"" + Escape(resultName) + "\",\n"
                + "  \"totalBytes\": " + report.summary.totalSize + "\n"
                + "}\n";
            File.WriteAllText(resultPath, payload);
            if (report.summary.result != BuildResult.Succeeded)
                throw new InvalidOperationException(
                    "player build failed: " + report.summary.result);
        }

        private static string Escape(string value)
        {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }
    }
}
'''

_RUNTIME_PROBE = r'''
using System;
using System.Collections;
using System.IO;
using System.Linq;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace GameForgeShowcaseEvaluator
{
    public sealed class RuntimeProbe : MonoBehaviour
    {
        private string initialPath;
        private string laterPath;
        private string metadataPath;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Install()
        {
            GameObject host = new GameObject("GameForgeShowcaseRuntimeProbe");
            DontDestroyOnLoad(host);
            host.AddComponent<RuntimeProbe>();
        }

        private void Awake()
        {
            Application.runInBackground = true;
            initialPath = ReadArgument("-gameforgeInitial");
            laterPath = ReadArgument("-gameforgeLater");
            metadataPath = ReadArgument("-gameforgeMetadata");
            StartCoroutine(Capture());
        }

        private IEnumerator Capture()
        {
            for (int index = 0; index < 30; index++) yield return null;
            CapturePath(initialPath);
            for (int index = 0; index < 150; index++) yield return null;
            CapturePath(laterPath);
            for (int index = 0; index < 30; index++) yield return null;

            Scene scene = SceneManager.GetActiveScene();
            int roots = scene.IsValid() ? scene.rootCount : 0;
            int cameras = FindObjectsByType<Camera>(FindObjectsSortMode.None).Length;
            int canvases = FindObjectsByType<Canvas>(FindObjectsSortMode.None).Length;
            int behaviours = FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None).Length;
            bool initialExists = File.Exists(initialPath);
            bool laterExists = File.Exists(laterPath);
            string payload = "{\n"
                + "  \"status\": \"PASS\",\n"
                + "  \"scene\": \"" + Escape(scene.name) + "\",\n"
                + "  \"rootObjects\": " + roots + ",\n"
                + "  \"cameras\": " + cameras + ",\n"
                + "  \"canvases\": " + canvases + ",\n"
                + "  \"behaviours\": " + behaviours + ",\n"
                + "  \"frame\": " + Time.frameCount + ",\n"
                + "  \"width\": " + Screen.width + ",\n"
                + "  \"height\": " + Screen.height + ",\n"
                + "  \"initialScreenshot\": " + initialExists.ToString().ToLowerInvariant()
                + ",\n"
                + "  \"laterScreenshot\": " + laterExists.ToString().ToLowerInvariant()
                + "\n}\n";
            EnsureParent(metadataPath);
            File.WriteAllText(metadataPath, payload);
            Application.Quit(0);
        }

        private static void CapturePath(string path)
        {
            if (String.IsNullOrWhiteSpace(path)) return;
            EnsureParent(path);
            ScreenCapture.CaptureScreenshot(path);
        }

        private static string ReadArgument(string name)
        {
            string[] arguments = Environment.GetCommandLineArgs();
            for (int index = 0; index + 1 < arguments.Length; index++)
                if (arguments[index] == name) return arguments[index + 1];
            return Path.Combine(Application.persistentDataPath, name.TrimStart('-') + ".json");
        }

        private static void EnsureParent(string path)
        {
            string parent = Path.GetDirectoryName(path);
            if (!String.IsNullOrWhiteSpace(parent)) Directory.CreateDirectory(parent);
        }

        private static string Escape(string value)
        {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }
    }
}
'''


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--version", type=Path, default=DEFAULT_VERSION)
    parser.add_argument("--experiment-directory", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--agent-executable", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--unity-executable", type=Path, default=DEFAULT_UNITY)
    parser.add_argument("--max-new-attempts", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--evaluator-smoke-project", type=Path)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_frozen_json(path: Path, payload: object) -> None:
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


def _copy_project(source: Path, destination: Path) -> None:
    ignored = {
        ".git",
        ".godot",
        "Build",
        "Builds",
        "Library",
        "Logs",
        "Temp",
        "UserSettings",
        "obj",
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        return {name for name in names if name in ignored or (base / name).is_symlink()}

    shutil.copytree(source, destination, ignore=ignore, copy_function=shutil.copy2)
    sibling_bridge = source.parent / "AgentBridge"
    if sibling_bridge.is_dir():
        shutil.copytree(
            sibling_bridge,
            destination.parent / "AgentBridge",
            ignore=ignore,
            copy_function=shutil.copy2,
        )


def _run_process(
    command: list[str],
    *,
    environment: Mapping[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        env=dict(environment) if environment is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    return {
        "return_code": process.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
    }


def _source_metrics(workspace: Path) -> dict[str, int]:
    files = [
        path
        for path in workspace.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not ({"Library", "Logs", "Temp", "obj"} & set(path.relative_to(workspace).parts))
    ]
    return {
        "public_file_count": len(files),
        "csharp_file_count": sum(path.suffix.lower() == ".cs" for path in files),
        "scene_file_count": sum(path.suffix.lower() == ".unity" for path in files),
        "prefab_file_count": sum(path.suffix.lower() == ".prefab" for path in files),
        "public_bytes": sum(path.stat().st_size for path in files),
    }


def _player_executable(application: Path) -> Path | None:
    directory = application / "Contents" / "MacOS"
    if not directory.is_dir():
        return None
    candidates = [
        path for path in directory.iterdir() if path.is_file() and os.access(path, os.X_OK)
    ]
    return sorted(candidates)[0] if candidates else None


def _evaluate_showcase(workspace: Path, artifact: Path, unity: Path) -> dict[str, Any]:
    artifact.mkdir(parents=True, exist_ok=True)
    evaluation_project = artifact / "evaluation-project"
    if evaluation_project.exists():
        raise RuntimeError(f"evaluation project already exists: {evaluation_project}")
    _copy_project(workspace, evaluation_project)
    editor_directory = evaluation_project / "Assets" / "GameForgeShowcaseEvaluator" / "Editor"
    runtime_directory = evaluation_project / "Assets" / "GameForgeShowcaseEvaluator" / "Runtime"
    editor_directory.mkdir(parents=True)
    runtime_directory.mkdir(parents=True)
    (editor_directory / "GameForgeShowcaseBuild.cs").write_text(_EDITOR_PROBE, encoding="utf-8")
    (runtime_directory / "GameForgeShowcaseRuntimeProbe.cs").write_text(
        _RUNTIME_PROBE, encoding="utf-8"
    )

    build = artifact / "build" / "Showcase.app"
    build_result_path = artifact / "build-result.json"
    build_log = artifact / "unity-build.log"
    environment = dict(os.environ)
    environment["GAMEFORGE_SHOWCASE_BUILD_PATH"] = str(build)
    environment["GAMEFORGE_SHOWCASE_BUILD_RESULT"] = str(build_result_path)
    build_process = _run_process(
        [
            str(unity),
            "-batchmode",
            "-quit",
            "-nographics",
            "-projectPath",
            str(evaluation_project),
            "-executeMethod",
            "GameForgeShowcaseEvaluator.BuildEntry.Build",
            "-logFile",
            str(build_log),
        ],
        environment=environment,
        timeout=600,
    )
    (artifact / "unity-build-process.log").write_text(
        build_process.pop("stdout") + build_process.pop("stderr"), encoding="utf-8"
    )
    build_result: object | None = None
    if build_result_path.is_file():
        try:
            build_result = json.loads(build_result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            build_result = None
    executable = _player_executable(build)
    runtime_process: dict[str, Any] | None = None
    initial = artifact / "initial.png"
    later = artifact / "later.png"
    metadata_path = artifact / "runtime-metadata.json"
    player_log = artifact / "player.log"
    if build_process["return_code"] == 0 and executable is not None:
        runtime_process = _run_process(
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
                "-gameforgeInitial",
                str(initial),
                "-gameforgeLater",
                str(later),
                "-gameforgeMetadata",
                str(metadata_path),
            ],
            timeout=120,
        )
        (artifact / "player-process.log").write_text(
            runtime_process.pop("stdout") + runtime_process.pop("stderr"), encoding="utf-8"
        )
    metadata: object | None = None
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = None
    scenes = sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*.unity")
        if "Library" not in path.relative_to(workspace).parts
    )
    screenshots_ok = all(path.is_file() and path.stat().st_size > 0 for path in (initial, later))
    build_ok = (
        build_process["return_code"] == 0
        and isinstance(build_result, dict)
        and build_result.get("status") == "Succeeded"
        and executable is not None
    )
    runtime_ok = (
        runtime_process is not None
        and runtime_process["return_code"] == 0
        and not runtime_process["timed_out"]
        and isinstance(metadata, dict)
        and metadata.get("status") == "PASS"
        and screenshots_ok
    )
    return {
        "status": "PASS" if scenes and build_ok and runtime_ok else "FAIL",
        "scene_paths": scenes,
        "source_metrics": _source_metrics(workspace),
        "build_process": build_process,
        "build_result": build_result,
        "build_application": str(build) if build.is_dir() else None,
        "player_executable": str(executable) if executable is not None else None,
        "runtime_process": runtime_process,
        "runtime_metadata": metadata,
        "initial_screenshot": str(initial) if initial.is_file() else None,
        "later_screenshot": str(later) if later.is_file() else None,
    }


def _load_runtime(version: Path) -> tuple[Any, Any, Any, Any]:
    source_candidate = version / "source"
    source = source_candidate if source_candidate.is_dir() else version
    sys.path.insert(0, str(source))
    from gameforge.harness.execution_profiles import ExecutionProfile
    from gameforge.harness.game_workspace import run_open_game_workspace

    from gameforge.harness.model_profiles import ModelProfile, ModelProvider

    return ModelProfile, ModelProvider, ExecutionProfile, run_open_game_workspace


def _validate_manifest(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if manifest["design"]["type"] != "single-condition showcase":
        raise ValueError("Unity20 must remain a single-condition showcase")
    if manifest["execution"]["execution_profile"] != "native-open":
        raise ValueError("Unity20 showcase is frozen to native-open")
    tasks = {str(task["task_id"]): task for task in manifest["tasks"]}
    schedule = manifest["schedule"]
    if len(tasks) != 20 or len(schedule) != 20:
        raise ValueError("Unity20 requires twenty unique scheduled tasks")
    if [item["ordinal"] for item in schedule] != list(range(1, 21)):
        raise ValueError("Unity20 ordinals must be contiguous")
    if {item["task_id"] for item in schedule} != set(tasks):
        raise ValueError("Unity20 schedule and task IDs differ")
    return schedule, tasks


def _progress(experiment: Path, total: int) -> None:
    receipts = sorted((experiment / "receipts").glob("*.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in receipts]
    hard_passes = sum(item.get("hard_gate") == "PASS" for item in payloads)
    infrastructure = sum(item.get("classification") == "INFRASTRUCTURE" for item in payloads)
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "completed": len(payloads),
            "total": total,
            "hard_gate_passes": hard_passes,
            "infrastructure_failures": infrastructure,
            "next_ordinal": len(payloads) + 1 if len(payloads) < total else None,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    manifest_path = arguments.manifest.resolve(strict=True)
    version = arguments.version.resolve(strict=True)
    experiment = arguments.experiment_directory.resolve()
    agent = arguments.agent_executable.resolve(strict=True)
    unity = arguments.unity_executable.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schedule, tasks = _validate_manifest(manifest)
    metadata_path = version / "VERSION.json"
    if not metadata_path.is_file():
        metadata_path = version / "RELEASE.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    source_candidate = version / "source"
    source = source_candidate if source_candidate.is_dir() else version
    template = (source / "unity/EmptyGameTemplate").resolve(strict=True)

    if arguments.evaluator_smoke_project is not None:
        result = _evaluate_showcase(
            arguments.evaluator_smoke_project.resolve(strict=True),
            experiment / "evaluator-smoke",
            unity,
        )
        _write_frozen_json(experiment / "evaluator-smoke-result.json", result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "PASS" else 1

    protocol = {
        "schema_version": 1,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "runner": str(Path(__file__).resolve()),
        "runner_sha256": _sha256(Path(__file__).resolve()),
        "version": metadata["version"],
        "runtime_tree_sha256": metadata["runtime_tree_sha256"],
        "runtime_file_count": metadata["runtime_file_count"],
        "template": str(template),
        "agent_executable": str(agent),
        "agent_sha256": _sha256(agent),
        "unity_executable": str(unity),
        "unity_sha256": _sha256(unity),
        "execution": manifest["execution"],
        "evaluation": manifest["evaluation"],
        "schedule": schedule,
    }
    experiment.mkdir(parents=True, exist_ok=True)
    _write_frozen_json(experiment / "protocol-freeze.json", protocol)
    _progress(experiment, len(schedule))
    if arguments.prepare_only:
        print(experiment / "protocol-freeze.json")
        return 0

    ModelProfile, ModelProvider, ExecutionProfile, run_open_game_workspace = _load_runtime(version)
    profile = ModelProfile(
        provider=ModelProvider.CODEX_SUBSCRIPTION,
        model=manifest["execution"]["model"],
        executable=agent,
        reasoning_effort=manifest["execution"]["reasoning_effort"],
        execution_profile=ExecutionProfile.NATIVE_OPEN,
        timeout_seconds=manifest["execution"]["solver_timeout_seconds"],
        max_output_tokens=1200,
    )
    completed = {
        int(json.loads(path.read_text(encoding="utf-8"))["ordinal"])
        for path in (experiment / "receipts").glob("*.json")
    }
    pending = [item for item in schedule if int(item["ordinal"]) not in completed]
    if arguments.max_new_attempts is not None:
        pending = pending[: arguments.max_new_attempts]

    for item in pending:
        ordinal = int(item["ordinal"])
        task_id = str(item["task_id"])
        task = tasks[task_id]
        attempt_started = datetime.now(UTC)
        started = time.monotonic()
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "ordinal": ordinal,
            "task_id": task_id,
            "genre": task["genre"],
            "condition": manifest["design"]["condition"],
            "started_at": attempt_started.isoformat(),
        }
        try:
            run = run_open_game_workspace(
                root=ROOT,
                project=template,
                request=task["brief"],
                model_profile=profile,
                engine="auto",
                task_id=f"unity20-{ordinal:02d}-{task_id}",
                timeout_seconds=manifest["execution"]["solver_timeout_seconds"],
            )
            result = run.harness_run.result
            agent_result_path = run.directory / "agent-result.json"
            agent_result = json.loads(agent_result_path.read_text(encoding="utf-8"))
            compile_pass = all(
                gate.status.value == "PASS"
                for gate in result.gates
                if gate.gate in {"specification", "compilation"}
            )
            evaluation: dict[str, Any] | None = None
            if compile_pass and agent_result.get("solver_completed"):
                evaluation = _evaluate_showcase(
                    run.workspace,
                    experiment / "artifacts" / f"{ordinal:02d}-{task_id}",
                    unity,
                )
            hard_gate = (
                "PASS"
                if compile_pass
                and agent_result.get("solver_completed")
                and evaluation is not None
                and evaluation["status"] == "PASS"
                else "FAIL"
            )
            receipt.update(
                {
                    "classification": "PASS" if hard_gate == "PASS" else "MODEL_OR_TASK",
                    "hard_gate": hard_gate,
                    "harness_run_directory": str(run.directory),
                    "workspace": str(run.workspace),
                    "engine": run.engine,
                    "solver_completed": agent_result.get("solver_completed"),
                    "solver_error_code": agent_result.get("solver_error_code"),
                    "compile_pass": compile_pass,
                    "run_status": result.status.value,
                    "usage": {
                        "input_tokens": result.input_tokens,
                        "cached_input_tokens": result.cached_input_tokens,
                        "output_tokens": result.output_tokens,
                        "reasoning_output_tokens": result.reasoning_output_tokens,
                        "tool_calls": result.tool_calls,
                    },
                    "evaluation": evaluation,
                }
            )
        except Exception as error:
            receipt.update(
                {
                    "classification": "INFRASTRUCTURE",
                    "hard_gate": "NOT_RUN",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
        receipt["ended_at"] = datetime.now(UTC).isoformat()
        receipt["duration_seconds"] = round(time.monotonic() - started, 3)
        _write_frozen_json(
            experiment / "receipts" / f"{ordinal:02d}-{task_id}.json",
            receipt,
        )
        _progress(experiment, len(schedule))
        print(
            f"[{ordinal:02d}/{len(schedule)}] {task_id}: "
            f"{receipt['classification']} / {receipt['hard_gate']}",
            flush=True,
        )

    _progress(experiment, len(schedule))
    print(experiment / "progress.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
