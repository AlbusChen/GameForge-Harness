from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gameforge.harness.shell_environment import login_shell_tool_environment
from gameforge.harness.workspace_program import _program_environment


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_zsh_login_shell_keeps_task_owned_tool_first(tmp_path: Path) -> None:
    tools = tmp_path / "task tools"
    tools.mkdir()
    wrapper = tools / "godot"
    _write_executable(wrapper, "printf '%s\\n' pinned-4.4.1\n")

    environment = os.environ.copy()
    environment.update(
        login_shell_tool_environment(
            tools,
            exported_variables={"GODOT": str(wrapper)},
            inherited_path="/usr/bin:/bin",
        )
    )
    completed = subprocess.run(
        [
            "/bin/zsh",
            "-lc",
            "command -v godot; godot --version; printf '%s\\n' \"$GODOT\"",
        ],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.stdout.splitlines() == [
        str(wrapper),
        "pinned-4.4.1",
        str(wrapper),
    ]


def test_nested_non_login_shell_preserves_exports_and_quotes(tmp_path: Path) -> None:
    tools = tmp_path / "tool's bin"
    tools.mkdir()
    marker = "a value with 'quotes' and spaces"
    environment = os.environ.copy()
    environment.update(
        login_shell_tool_environment(
            tools,
            exported_variables={"ENGINE_MARKER": marker},
            inherited_path="/usr/bin:/bin",
        )
    )

    completed = subprocess.run(
        ["/bin/zsh", "-c", "printf '%s\\n' \"$ENGINE_MARKER\"; print -r -- $path[1]"],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.stdout.splitlines() == [marker, str(tools)]


def test_rejects_non_portable_environment_names(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()

    with pytest.raises(ValueError, match="portable shell identifiers"):
        login_shell_tool_environment(
            tools,
            exported_variables={"NOT-SAFE": "value"},
        )


def test_rejects_non_string_environment_values(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()

    with pytest.raises(TypeError, match="values must be strings"):
        login_shell_tool_environment(
            tools,
            exported_variables={"COUNT": 1},  # type: ignore[dict-item]
        )


def test_workspace_program_login_shell_keeps_host_managed_shim(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    tools = scratch / "host-managed-bin"
    tools.mkdir()
    wrapper = tools / "godot"
    _write_executable(wrapper, "printf '%s\\n' supervised-host-shim\n")

    completed = subprocess.run(
        ["/bin/zsh", "-lc", "command -v godot; godot --version"],
        check=True,
        capture_output=True,
        env=_program_environment(
            scratch,
            shim_directory=tools,
            host_managed_executables=("godot",),
        ),
        text=True,
    )

    assert completed.stdout.splitlines() == [str(wrapper), "supervised-host-shim"]
