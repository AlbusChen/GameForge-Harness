from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from gameforge.config import EXPECTED_UNITY_VERSION, configured_unity_editor, llm_key_is_configured


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    status: str
    detail: str
    required: bool = True


@dataclass(frozen=True)
class DoctorReport:
    overall: str
    checks: tuple[DoctorCheck, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


class UnityToolErrorCode(StrEnum):
    EDITOR_NOT_FOUND = "editor_not_found"
    PROJECT_NOT_FOUND = "project_not_found"
    TIMEOUT = "timeout"
    PROCESS_FAILED = "process_failed"
    RESULT_MISSING = "result_missing"
    BUILD_NOT_FOUND = "build_not_found"


class UnityToolError(RuntimeError):
    def __init__(self, code: UnityToolErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class UnityToolResult:
    tool: str
    return_code: int
    log_path: Path
    artifacts: tuple[Path, ...] = ()


@dataclass(frozen=True)
class UnityBatchGateway:
    """Fixed Unity batch operations; callers cannot provide arbitrary editor methods."""

    editor: Path
    project: Path
    run_directory: Path

    def compile_project(self, timeout_seconds: int = 900) -> UnityToolResult:
        """Import and compile a generic Unity project without requiring AgentBridge."""

        result = self._invoke(
            "compile_project",
            [],
            timeout_seconds=timeout_seconds,
        )
        try:
            log = result.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise UnityToolError(
                UnityToolErrorCode.RESULT_MISSING,
                f"Unity compile log is unavailable: {result.log_path}",
            ) from error
        compiler_markers = (
            "Scripts have compiler errors",
            "Compilation failed",
            "compilation had errors",
        )
        if any(marker.casefold() in log.casefold() for marker in compiler_markers) or re.search(
            r"\berror CS\d{4}\b", log
        ):
            raise UnityToolError(
                UnityToolErrorCode.PROCESS_FAILED,
                f"Unity reported compiler errors; see {result.log_path}",
            )
        return result

    def health_check(self, timeout_seconds: int = 300) -> UnityToolResult:
        output = self.run_directory / "bridge-health.json"
        return self._invoke(
            "health_check",
            [
                "-executeMethod",
                "VerifiedGameBuilder.AgentBridge.Editor.BridgeHealthCheck.WriteBatchHealthCheck",
                "-gameforgeOutput",
                str(output),
            ],
            artifacts=(output,),
            timeout_seconds=timeout_seconds,
        )

    def create_arena(self, timeout_seconds: int = 600) -> UnityToolResult:
        return self._invoke(
            "create_arena",
            ["-executeMethod", "VerifiedGameBuilder.Editor.ArenaSetup.CreateArena"],
            timeout_seconds=timeout_seconds,
        )

    def run_tests(self, platform: str, timeout_seconds: int = 900) -> UnityToolResult:
        allowed = {"EditMode": "edit-mode-results.xml", "PlayMode": "play-mode-results.xml"}
        if platform not in allowed:
            raise ValueError(f"unsupported Unity test platform: {platform}")
        output = self.run_directory / allowed[platform]
        return self._invoke(
            f"run_{platform.lower()}_tests",
            ["-runTests", "-testPlatform", platform, "-testResults", str(output)],
            artifacts=(output,),
            timeout_seconds=timeout_seconds,
            quit_editor=False,
        )

    def build_macos(self, timeout_seconds: int = 1200) -> UnityToolResult:
        output = self.run_directory / "build" / "ArenaDemo.app"
        return self._invoke(
            "build_macos",
            [
                "-executeMethod",
                "VerifiedGameBuilder.Editor.ArenaBuild.BuildMacOS",
                "-gameforgeOutput",
                str(output),
            ],
            artifacts=(output,),
            timeout_seconds=timeout_seconds,
        )

    def launch_build_smoke_test(self, timeout_seconds: int = 120) -> UnityToolResult:
        application = self.run_directory / "build" / "ArenaDemo.app"
        executable_directory = application / "Contents" / "MacOS"
        if not executable_directory.is_dir():
            raise UnityToolError(UnityToolErrorCode.BUILD_NOT_FOUND, str(application))
        executables = [path for path in executable_directory.iterdir() if path.is_file()]
        if len(executables) != 1:
            raise UnityToolError(
                UnityToolErrorCode.BUILD_NOT_FOUND,
                f"expected one player executable in {executable_directory}",
            )

        logs = self.run_directory / "logs"
        screenshots = self.run_directory / "screenshots"
        logs.mkdir(parents=True, exist_ok=True)
        screenshots.mkdir(parents=True, exist_ok=True)
        log_path = logs / "build-smoke.log"
        marker = self.run_directory / "build-smoke.json"
        checkpoint = self.run_directory / "build-smoke.checkpoint.json"
        screenshot = screenshots / "build-smoke.png"
        with tempfile.TemporaryDirectory(prefix="gameforge-smoke-") as staging_directory:
            staging = Path(staging_directory)
            staged_log = staging / "build-smoke.log"
            staged_marker = staging / "build-smoke.json"
            staged_checkpoint = staging / "build-smoke.checkpoint.json"
            staged_screenshot = staging / "build-smoke.png"
            command = [
                str(executables[0]),
                "-screen-fullscreen",
                "0",
                "-screen-width",
                "1280",
                "-screen-height",
                "720",
                "-logFile",
                str(staged_log),
                "-gameforgeSmokeOutput",
                str(staged_marker),
                "-gameforgeSmokeCheckpoint",
                str(staged_checkpoint),
                "-gameforgeScreenshot",
                str(staged_screenshot),
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=_environment_without_credentials(),
                )
            except subprocess.TimeoutExpired as error:
                if staged_log.is_file():
                    shutil.copy2(staged_log, log_path)
                if staged_checkpoint.is_file():
                    shutil.copy2(staged_checkpoint, checkpoint)
                checkpoint_detail = _smoke_checkpoint_detail(staged_checkpoint)
                raise UnityToolError(
                    UnityToolErrorCode.TIMEOUT,
                    f"build smoke test exceeded {timeout_seconds} seconds; "
                    f"{checkpoint_detail}; see {log_path}",
                ) from error
            if staged_log.is_file():
                shutil.copy2(staged_log, log_path)
            if staged_checkpoint.is_file():
                shutil.copy2(staged_checkpoint, checkpoint)
            if completed.returncode != 0:
                raise UnityToolError(
                    UnityToolErrorCode.PROCESS_FAILED,
                    "build smoke test failed with exit code "
                    f"{completed.returncode}; {_smoke_checkpoint_detail(staged_checkpoint)}; "
                    f"see {log_path}",
                )
            staged_artifacts = (staged_marker, staged_screenshot, staged_checkpoint)
            missing = [path for path in staged_artifacts if not path.exists()]
            if missing:
                raise UnityToolError(
                    UnityToolErrorCode.RESULT_MISSING,
                    f"build smoke test did not create: {missing}",
                )
            _validate_runtime_snapshot(staged_marker)
            shutil.copy2(staged_marker, marker)
            shutil.copy2(staged_screenshot, screenshot)

        artifacts = (marker, screenshot, checkpoint)
        return UnityToolResult("launch_build_smoke_test", completed.returncode, log_path, artifacts)

    def _invoke(
        self,
        tool: str,
        arguments: list[str],
        *,
        artifacts: tuple[Path, ...] = (),
        timeout_seconds: int,
        quit_editor: bool = True,
    ) -> UnityToolResult:
        self._validate()
        logs = self.run_directory / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts:
            artifact.parent.mkdir(parents=True, exist_ok=True)
        log_path = logs / f"{tool}.log"
        command = [
            str(self.editor),
            "-batchmode",
            "-nographics",
            "-projectPath",
            str(self.project),
            *arguments,
        ]
        if quit_editor:
            command.append("-quit")
        command.extend(["-logFile", str(log_path)])

        environment = _environment_without_credentials()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            raise UnityToolError(
                UnityToolErrorCode.TIMEOUT,
                f"Unity tool {tool} exceeded {timeout_seconds} seconds",
            ) from error
        if completed.returncode != 0:
            raise UnityToolError(
                UnityToolErrorCode.PROCESS_FAILED,
                f"Unity tool {tool} failed with exit code {completed.returncode}; see {log_path}",
            )
        missing = [path for path in artifacts if not path.exists()]
        if missing:
            raise UnityToolError(
                UnityToolErrorCode.RESULT_MISSING,
                f"Unity tool {tool} did not create: {missing}",
            )
        return UnityToolResult(tool, completed.returncode, log_path, artifacts)

    def _validate(self) -> None:
        if not self.editor.is_file():
            raise UnityToolError(UnityToolErrorCode.EDITOR_NOT_FOUND, str(self.editor))
        if not self.project.is_dir():
            raise UnityToolError(UnityToolErrorCode.PROJECT_NOT_FOUND, str(self.project))


def _environment_without_credentials() -> dict[str, str]:
    sensitive_names = {
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }

    def is_sensitive(name: str) -> bool:
        return name in sensitive_names or name.endswith(("_API_KEY", "_ACCESS_TOKEN"))

    return {name: value for name, value in os.environ.items() if not is_sensitive(name)}


def _validate_runtime_snapshot(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        player = payload["player"]
        enemies = payload["enemies"]
        ui = payload["ui"]
        errors = payload["errors"]
        valid = (
            payload["gameStatus"] == "playing"
            and int(payload["frame"]) > 0
            and int(payload["enemiesAlive"]) == len(enemies)
            and int(payload["currentWave"]) >= 1
            and int(player["health"]) > 0
            and int(player["maxHealth"]) >= int(player["health"])
            and len(player["position"]) == 3
            and isinstance(ui["endScreenVisible"], bool)
            and isinstance(errors, list)
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise UnityToolError(
            UnityToolErrorCode.RESULT_MISSING,
            f"build smoke state is invalid: {path}",
        ) from error
    if not valid:
        raise UnityToolError(
            UnityToolErrorCode.PROCESS_FAILED,
            f"build smoke state failed invariants: {path}",
        )


def _smoke_checkpoint_detail(path: Path) -> str:
    if not path.is_file():
        return "smoke checkpoint unavailable"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "smoke checkpoint unreadable"
    stage = payload.get("stage")
    return f"last smoke stage={stage}" if isinstance(stage, str) else "smoke checkpoint invalid"


def _command_version(command: str, *arguments: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0] if output else None


def _project_version(project_path: Path) -> str | None:
    version_file = project_path / "ProjectSettings" / "ProjectVersion.txt"
    if not version_file.is_file():
        return None
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("m_EditorVersion:"):
            return line.partition(":")[2].strip()
    return None


def run_doctor(root: Path, project_path: Path) -> DoctorReport:
    checks: list[DoctorCheck] = []

    python_version = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    checks.append(DoctorCheck("python", "PASS", python_version))

    uv_version = _command_version("uv", "--version")
    checks.append(DoctorCheck("uv", "PASS" if uv_version else "FAIL", uv_version or "uv not found"))

    git_version = _command_version("git", "--version")
    checks.append(
        DoctorCheck("git", "PASS" if git_version else "FAIL", git_version or "git not found")
    )

    disk = shutil.disk_usage(root)
    free_gib = disk.free / (1024**3)
    checks.append(
        DoctorCheck(
            "disk_space",
            "PASS" if free_gib >= 35 else "FAIL",
            f"{free_gib:.1f} GiB free; at least 35 GiB required",
        )
    )

    editor = configured_unity_editor()
    checks.append(
        DoctorCheck(
            "unity_editor",
            "PASS" if editor and editor.is_file() else "FAIL",
            str(editor) if editor else f"Unity {EXPECTED_UNITY_VERSION} is not installed",
        )
    )

    project_version = _project_version(project_path)
    checks.append(
        DoctorCheck(
            "project_version",
            "PASS" if project_version == EXPECTED_UNITY_VERSION else "FAIL",
            project_version or "ProjectVersion.txt not found",
        )
    )

    package_manifest = project_path / "Packages" / "manifest.json"
    checks.append(
        DoctorCheck(
            "unity_manifest",
            "PASS" if package_manifest.is_file() else "FAIL",
            str(package_manifest),
        )
    )

    bridge_manifest = root / "unity" / "AgentBridge" / "package.json"
    checks.append(
        DoctorCheck(
            "agent_bridge_package",
            "PASS" if bridge_manifest.is_file() else "FAIL",
            str(bridge_manifest),
        )
    )

    arena_scene = project_path / "Assets" / "Scenes" / "Arena.unity"
    checks.append(
        DoctorCheck(
            "arena_scene",
            "PASS" if arena_scene.is_file() else "FAIL",
            str(arena_scene) if arena_scene.is_file() else "run scripts/unity-baseline.command",
        )
    )

    key_configured = llm_key_is_configured(root)
    checks.append(
        DoctorCheck(
            "llm_api_key",
            "PASS" if key_configured else "WARN",
            "configured" if key_configured else "not configured; not required for mock mode",
            required=False,
        )
    )

    local_env = root / ".env.local"
    if local_env.exists():
        permissions = oct(os.stat(local_env).st_mode & 0o777)
        safe = permissions in {"0o600", "0o400"}
        checks.append(
            DoctorCheck(
                "local_env_permissions",
                "PASS" if safe else "WARN",
                f"permissions are {permissions}; recommended 0o600",
                required=False,
            )
        )

    failed = [check for check in checks if check.required and check.status != "PASS"]
    return DoctorReport(overall="PASS" if not failed else "FAIL", checks=tuple(checks))
