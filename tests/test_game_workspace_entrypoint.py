from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from gameforge.cli import _parser
from gameforge.harness.contracts import EngineName
from gameforge.harness.engine_registry import (
    detect_workspace_engine,
    resolve_workspace_engine,
)
from gameforge.harness.game_workspace import run_open_game_workspace
from gameforge.harness.model_profiles import ModelProfile, ModelProvider


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _unity_project(root: Path) -> Path:
    (root / "Assets").mkdir(parents=True)
    settings = root / "ProjectSettings"
    settings.mkdir()
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.3.20f1\n", encoding="utf-8"
    )
    return root


def _godot_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "project.godot").write_text("[application]\n", encoding="utf-8")
    return root


def test_auto_detects_unity_and_godot_from_standard_markers(tmp_path: Path) -> None:
    unity = _unity_project(tmp_path / "unity")
    godot = _godot_project(tmp_path / "godot")

    assert detect_workspace_engine(unity) is EngineName.UNITY
    assert detect_workspace_engine(godot) is EngineName.GODOT
    assert resolve_workspace_engine("auto", unity).name is EngineName.UNITY
    assert resolve_workspace_engine("auto", godot).name is EngineName.GODOT


def test_auto_detection_fails_closed_for_unknown_or_ambiguous_projects(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    with pytest.raises(ValueError, match="could not detect"):
        detect_workspace_engine(unknown)

    ambiguous = _unity_project(tmp_path / "ambiguous")
    (ambiguous / "project.godot").write_text("[application]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="multiple engines"):
        detect_workspace_engine(ambiguous)


def test_unified_cli_defaults_to_auto_and_legacy_alias_remains() -> None:
    parser = _parser()
    unified = parser.parse_args(
        [
            "run-game-workspace",
            "--project",
            "/tmp/project",
            "--request",
            "Make a game",
            "--model-profile",
            "/tmp/model.yaml",
        ]
    )
    legacy = parser.parse_args(
        [
            "run-unity-workspace",
            "--project",
            "/tmp/project",
            "--request",
            "Make a game",
            "--model-profile",
            "/tmp/model.yaml",
        ]
    )

    assert unified.engine == "auto"
    assert legacy.command == "run-unity-workspace"


def test_unified_auto_godot_runs_common_solver_then_independent_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    project = _godot_project(tmp_path / "source-project")
    events = (
        {"type": "thread.started", "thread_id": "godot-open"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "implemented"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 2}},
    )
    codex = _executable(
        tmp_path / "codex",
        "printf '%s\\n' "
        + " ".join(shlex.quote(json.dumps(event)) for event in events)
        + "\n",
    )
    godot = _executable(tmp_path / "godot", "exit 0\n")
    monkeypatch.setenv("GODOT_EDITOR_PATH", str(godot))
    profile = ModelProfile(
        provider=ModelProvider.CODEX_SUBSCRIPTION,
        model="test-model",
        executable=codex,
    )

    run = run_open_game_workspace(
        root=root,
        project=project,
        request="Create a small Godot game.",
        model_profile=profile,
        engine="auto",
        task_id="godot-unified-smoke",
        timeout_seconds=60,
    )

    assert run.engine == "godot"
    assert run.harness_run.result.status.value == "PASS"
    metadata = json.loads((run.directory / "workspace-metadata.json").read_text())
    assert metadata["engine_selection"] == "detected"
