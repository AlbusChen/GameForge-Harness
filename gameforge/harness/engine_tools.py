from __future__ import annotations

import json
import shlex
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from gameforge.harness.execution_profiles import ExecutionProfile
from gameforge.harness.shell_environment import login_shell_tool_environment
from gameforge.harness.supervised_native_transport import SupervisedNativeTransport


@dataclass
class EngineToolSession:
    engine: str
    profile: ExecutionProfile
    root: Path | None
    environment: dict[str, str]
    executable: Path | None
    transport: SupervisedNativeTransport | None = None

    def receipt(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "engine": self.engine,
            "execution_profile": self.profile.value,
            "command": self.engine,
            "pinned_host_executable": (
                str(self.executable) if self.executable is not None else None
            ),
            "transport": (
                "direct-host-exec"
                if self.profile is ExecutionProfile.NATIVE_OPEN
                else "transparent-supervised-host-broker"
                if self.profile is ExecutionProfile.SUPERVISED_NATIVE
                else "external-strong-isolation-runner"
            ),
            "argument_policy": "model-selected-unmodified",
            "workflow_policy": "none",
            "automatic_retry": False,
            "events": list(self.transport.events) if self.transport is not None else [],
        }


def _write_wrapper(path: Path, invocation: str) -> None:
    path.write_text(f'#!/bin/sh\nexec {invocation} "$@"\n', encoding="utf-8")
    path.chmod(0o755)


def _aliases(tools: Path, target: Path, names: tuple[str, ...]) -> None:
    for name in names:
        alias = tools / name
        if alias == target or alias.exists():
            continue
        alias.symlink_to(target.name)


@contextmanager
def unity_tool_session(
    *,
    profile: ExecutionProfile,
    workspace: Path,
    editor: Path | None,
    timeout_seconds: float,
) -> Iterator[EngineToolSession]:
    """Expose Unity without prescribing how the model uses it."""

    resolved_workspace = workspace.resolve(strict=True)
    if not resolved_workspace.is_dir():
        raise ValueError("Unity workspace must be a directory")
    if profile is ExecutionProfile.STRONG_ISOLATED:
        yield EngineToolSession(
            engine="unity",
            profile=profile,
            root=None,
            executable=None,
            environment={
                "UNITY": "unity",
                "UNITY_EDITOR": "unity",
                "GAMEFORGE_ENGINE_EXECUTION_PROFILE": profile.value,
                "GIT_CEILING_DIRECTORIES": str(resolved_workspace.parent),
            },
        )
        return

    if editor is None:
        raise ValueError("the pinned Unity editor is not installed")
    resolved_editor = editor.resolve(strict=True)
    if not resolved_editor.is_file():
        raise ValueError(f"Unity editor is unavailable: {resolved_editor}")

    with tempfile.TemporaryDirectory(prefix="gameforge-unity-tools-", dir="/tmp") as raw:
        root = Path(raw).resolve(strict=True)
        tools = root / "bin"
        tools.mkdir(mode=0o700)
        wrapper = tools / "unity"
        transport: SupervisedNativeTransport | None = None
        if profile is ExecutionProfile.NATIVE_OPEN:
            _write_wrapper(wrapper, shlex.quote(str(resolved_editor)))
        else:
            transport = SupervisedNativeTransport(
                executable=resolved_editor,
                workspace_root=resolved_workspace,
                scratch_root=root / "scratch",
                exchange_root=root / "exchange",
                timeout_seconds=timeout_seconds,
            )
            transport.__enter__()
            invocation = " ".join(
                [
                    shlex.quote(str(Path(sys.executable).resolve(strict=True))),
                    *(shlex.quote(item) for item in transport.client_arguments),
                ]
            )
            _write_wrapper(wrapper, invocation)
        _aliases(tools, wrapper, ("Unity", "UnityEditor"))
        environment = login_shell_tool_environment(
            tools,
            exported_variables={
                "UNITY": str(wrapper),
                "UNITY_EDITOR": str(wrapper),
                "UNITY_EDITOR_PATH": str(wrapper),
                "GAMEFORGE_ENGINE_EXECUTION_PROFILE": profile.value,
                "GIT_CEILING_DIRECTORIES": str(resolved_workspace.parent),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        session = EngineToolSession(
            engine="unity",
            profile=profile,
            root=root,
            environment=environment,
            executable=resolved_editor,
            transport=transport,
        )
        try:
            yield session
        finally:
            if transport is not None:
                transport.__exit__(None, None, None)


@contextmanager
def godot_tool_session(
    *,
    profile: ExecutionProfile,
    workspace: Path,
    executable: Path | None,
    timeout_seconds: float,
) -> Iterator[EngineToolSession]:
    """Expose pinned Godot with the same model-selected command contract as Unity."""

    resolved_workspace = workspace.resolve(strict=True)
    if profile is ExecutionProfile.STRONG_ISOLATED:
        yield EngineToolSession(
            engine="godot",
            profile=profile,
            root=None,
            executable=None,
            environment={
                "GODOT": "godot",
                "GAMEFORGE_ENGINE_EXECUTION_PROFILE": profile.value,
                "GIT_CEILING_DIRECTORIES": str(resolved_workspace.parent),
            },
        )
        return
    if executable is None:
        raise ValueError("the pinned Godot editor is not installed")
    resolved_executable = executable.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="gameforge-godot-tools-", dir="/tmp") as raw:
        root = Path(raw).resolve(strict=True)
        tools = root / "bin"
        tools.mkdir(mode=0o700)
        wrapper = tools / "godot"
        transport: SupervisedNativeTransport | None = None
        if profile is ExecutionProfile.NATIVE_OPEN:
            _write_wrapper(wrapper, shlex.quote(str(resolved_executable)))
        else:
            transport = SupervisedNativeTransport(
                executable=resolved_executable,
                workspace_root=resolved_workspace,
                scratch_root=root / "scratch",
                exchange_root=root / "exchange",
                timeout_seconds=timeout_seconds,
            )
            transport.__enter__()
            invocation = " ".join(
                [
                    shlex.quote(str(Path(sys.executable).resolve(strict=True))),
                    *(shlex.quote(item) for item in transport.client_arguments),
                ]
            )
            _write_wrapper(wrapper, invocation)
        environment = login_shell_tool_environment(
            tools,
            exported_variables={
                "GODOT": str(wrapper),
                "GODOT_EDITOR_PATH": str(wrapper),
                "GAMEFORGE_ENGINE_EXECUTION_PROFILE": profile.value,
                "GIT_CEILING_DIRECTORIES": str(resolved_workspace.parent),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        session = EngineToolSession(
            engine="godot",
            profile=profile,
            root=root,
            environment=environment,
            executable=resolved_executable,
            transport=transport,
        )
        try:
            yield session
        finally:
            if transport is not None:
                transport.__exit__(None, None, None)


def write_engine_receipt(path: Path, session: EngineToolSession) -> None:
    path.write_text(
        json.dumps(session.receipt(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
