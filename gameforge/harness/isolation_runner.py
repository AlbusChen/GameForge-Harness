from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROTOCOL_VERSION = "gameforge-isolation-runner-v1"


def _environment_without_credentials() -> dict[str, str]:
    blocked = {"GH_TOKEN", "GITHUB_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY"}
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in blocked and not name.upper().endswith(("_API_KEY", "_ACCESS_TOKEN"))
    }


@dataclass(frozen=True)
class ExternalIsolationRunner:
    """Validated interface to a user-supplied VM, container, or remote runner.

    The Harness deliberately does not equate a local process sandbox with strong
    isolation. A provider must identify itself through ``describe`` before it can
    receive a model workspace or a shell command.
    """

    executable: Path

    def __post_init__(self) -> None:
        executable = self.executable.resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"isolation runner is not executable: {executable}")
        object.__setattr__(self, "executable", executable)

    def validate(self, operation: str) -> dict[str, object]:
        try:
            completed = subprocess.run(
                [str(self.executable), "describe", "--json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                env=_environment_without_credentials(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"isolation runner preflight failed: {error}") from error
        if completed.returncode != 0:
            raise ValueError(
                f"isolation runner describe failed with exit code {completed.returncode}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ValueError("isolation runner describe returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("isolation runner description must be a JSON object")
        if payload.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("isolation runner protocol is not supported")
        if payload.get("isolation") != "strong":
            raise ValueError("isolation runner did not attest strong isolation")
        operations = payload.get("operations")
        if not isinstance(operations, list) or not all(
            isinstance(item, str) for item in operations
        ):
            raise ValueError("isolation runner operations are invalid")
        if operation not in operations:
            raise ValueError(f"isolation runner does not support {operation}")
        return payload

    def command(
        self,
        *,
        operation: str,
        workspace: Path,
        timeout_seconds: float,
        command: list[str],
    ) -> list[str]:
        if operation not in {"shell", "workspace-agent"}:
            raise ValueError(f"unsupported isolation operation: {operation}")
        resolved_workspace = workspace.resolve(strict=True)
        if not resolved_workspace.is_dir():
            raise ValueError("isolated workspace must be a directory")
        if timeout_seconds <= 0:
            raise ValueError("isolated operation timeout must be positive")
        if not command:
            raise ValueError("isolated command must not be empty")
        self.validate(operation)
        return [
            str(self.executable),
            operation,
            "--protocol",
            PROTOCOL_VERSION,
            "--workspace",
            str(resolved_workspace),
            "--timeout-seconds",
            str(timeout_seconds),
            "--",
            *command,
        ]
