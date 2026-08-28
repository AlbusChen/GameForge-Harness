from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gameforge.orchestrator.state_machine import RunState

TOOLS_BY_STATE: dict[RunState, frozenset[str]] = {
    RunState.INIT: frozenset({"health_check"}),
    RunState.VALIDATE_SPEC: frozenset(),
    RunState.CREATE_CHECKPOINT: frozenset({"create_checkpoint"}),
    RunState.INSPECT_PROJECT: frozenset(
        {"health_check", "inspect_project", "inspect_scene", "inspect_game_object"}
    ),
    RunState.PLAN: frozenset({"inspect_project", "inspect_scene", "inspect_game_object"}),
    RunState.IMPLEMENT: frozenset(
        {
            "create_game_object",
            "set_component_property",
            "create_script",
            "apply_code_patch",
            "wait_for_compilation",
            "read_console",
        }
    ),
    RunState.COMPILE: frozenset({"wait_for_compilation", "read_console"}),
    RunState.STRUCTURE_TEST: frozenset(
        {"inspect_scene", "inspect_game_object", "run_edit_mode_tests", "read_console"}
    ),
    RunState.PLAY_TEST: frozenset(
        {
            "enter_play_mode",
            "exit_play_mode",
            "send_test_command",
            "read_game_state",
            "capture_screenshot",
            "run_play_mode_tests",
            "read_console",
        }
    ),
    RunState.DIAGNOSE: frozenset(
        {"inspect_scene", "inspect_game_object", "read_console", "read_game_state"}
    ),
    RunState.REPAIR: frozenset(
        {"set_component_property", "apply_code_patch", "wait_for_compilation", "read_console"}
    ),
    RunState.RETEST: frozenset(
        {
            "wait_for_compilation",
            "read_console",
            "run_edit_mode_tests",
            "run_play_mode_tests",
            "read_game_state",
            "capture_screenshot",
        }
    ),
    RunState.BUILD: frozenset({"build_player", "read_console"}),
    RunState.BUILD_SMOKE_TEST: frozenset({"launch_build_smoke_test"}),
    RunState.REPORT: frozenset(),
    RunState.COMPLETE: frozenset(),
    RunState.FAILED: frozenset({"restore_checkpoint"}),
}


class PolicyViolation(PermissionError):
    pass


@dataclass(frozen=True)
class ToolPolicy:
    project_root: Path

    def require_tool_allowed(self, state: RunState, tool_name: str) -> None:
        if tool_name not in TOOLS_BY_STATE[state]:
            raise PolicyViolation(f"tool {tool_name!r} is not allowed in state {state}")

    def require_project_path(self, candidate: Path) -> Path:
        resolved_root = self.project_root.resolve()
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise PolicyViolation(f"path is outside the project root: {resolved}")
        return resolved
