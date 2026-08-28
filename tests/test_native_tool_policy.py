from __future__ import annotations

from pathlib import Path

import pytest
from gameforge.harness.native_tool_policy import evaluate_hook, godot_command_violation


@pytest.fixture
def wrapper(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    path = tools / "godot"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    "command",
    (
        "godot --headless --version",
        "$GODOT --headless --path . --editor --quit",
    ),
)
def test_policy_allows_the_fixed_broker_entrypoints(
    wrapper: Path,
    command: str,
) -> None:
    assert godot_command_violation(command, wrapper) is None


def test_policy_allows_the_exact_absolute_broker_wrapper(wrapper: Path) -> None:
    command = f"{wrapper} --headless --version"

    assert godot_command_violation(command, wrapper) is None


@pytest.mark.parametrize(
    "command",
    (
        "/Applications/Godot.app/Contents/MacOS/Godot --headless --version",
        "/opt/homebrew/bin/godot --headless --version",
        "python -c 'import subprocess; subprocess.run(["
        '"/Applications/Godot.app/Contents/MacOS/Godot"])\'',
        "open -a Godot",
        "Godot --headless --version",
    ),
)
def test_policy_rejects_direct_or_gui_godot_entrypoints(
    wrapper: Path,
    command: str,
) -> None:
    assert godot_command_violation(command, wrapper) is not None


def test_pre_tool_hook_denies_direct_godot_and_leaves_other_tools_free(
    wrapper: Path,
) -> None:
    denied = evaluate_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "/Applications/Godot.app/Contents/MacOS/Godot --version"},
        },
        allowed_wrapper=wrapper,
    )

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"  # type: ignore[index]
    assert (
        evaluate_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "godot --version"},
            },
            allowed_wrapper=wrapper,
        )
        == {}
    )
    assert (
        evaluate_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"path": "project.godot"},
            },
            allowed_wrapper=wrapper,
        )
        == {}
    )
