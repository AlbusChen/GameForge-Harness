from pathlib import Path

import pytest

from gameforge.orchestrator.policy import PolicyViolation, ToolPolicy
from gameforge.orchestrator.state_machine import RunState


def test_tool_is_restricted_by_state(tmp_path: Path) -> None:
    policy = ToolPolicy(tmp_path)

    with pytest.raises(PolicyViolation):
        policy.require_tool_allowed(RunState.PLAN, "apply_code_patch")


def test_path_cannot_escape_project(tmp_path: Path) -> None:
    policy = ToolPolicy(tmp_path / "project")

    with pytest.raises(PolicyViolation):
        policy.require_project_path(tmp_path / "outside.txt")
