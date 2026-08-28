from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from gameforge.adapters.unity import UnityBatchGateway, UnityToolError
from gameforge.harness.game_workspace import run_open_game_workspace
from gameforge.harness.model_profiles import ModelProfile, ModelProvider
from gameforge.harness.unity_open_workspace import _copy_project, run_open_unity_workspace

_PINNED_UNITY = Path("/Applications/Unity/Hub/Editor/6000.3.20f1/Unity.app/Contents/MacOS/Unity")


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_unified_auto_unity_workspace_runs_agent_then_independent_compile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release"
    project = tmp_path / "source-project"
    assets = project / "Assets"
    settings = project / "ProjectSettings"
    root.mkdir()
    assets.mkdir(parents=True)
    settings.mkdir()
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.3.20f1\n",
        encoding="utf-8",
    )
    codex_events = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "unity-open"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "implemented"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                }
            ),
        )
    )
    codex = _executable(
        tmp_path / "codex",
        """
workspace=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -C ]; then shift; workspace="$1"; fi
  shift
done
printf '%s\\n' 'public class GeneratedByOpenHarness {}' > "$workspace/Assets/Generated.cs"
"""
        + "printf '%s\\n' "
        + " ".join(shlex.quote(line) for line in codex_events.splitlines())
        + "\n",
    )
    unity = _executable(
        tmp_path / "Unity",
        """
log=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -logFile ]; then shift; log="$1"; fi
  shift
done
if [ -n "$log" ]; then
  mkdir -p "$(dirname "$log")"
  printf '%s\\n' 'Compilation completed successfully' > "$log"
fi
exit 0
""",
    )
    monkeypatch.setenv("UNITY_EDITOR_PATH", str(unity))
    profile = ModelProfile(
        provider=ModelProvider.CODEX_SUBSCRIPTION,
        model="test-model",
        executable=codex,
    )

    run = run_open_game_workspace(
        root=root,
        project=project,
        request="Create one valid C# script.",
        model_profile=profile,
        engine="auto",
        task_id="unity-native-smoke",
        timeout_seconds=60,
    )

    assert run.engine == "unity"
    assert run.harness_run.result.status.value == "PASS"
    assert (run.workspace / "Assets" / "Generated.cs").is_file()
    assert (run.directory / "logs" / "compile_project.log").is_file()
    runtime = json.loads((run.directory / "engine-runtime.json").read_text())
    assert runtime["execution_profile"] == "native-open"
    assert runtime["workflow_policy"] == "none"


def test_generic_compile_rejects_script_errors_even_with_zero_editor_exit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    editor = _executable(
        tmp_path / "Unity",
        """
log=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -logFile ]; then shift; log="$1"; fi
  shift
done
mkdir -p "$(dirname "$log")"
printf '%s\\n' 'Assets/Broken.cs(1,1): error CS1002: ; expected' > "$log"
exit 0
""",
    )

    with pytest.raises(UnityToolError, match="compiler errors"):
        UnityBatchGateway(editor, project, tmp_path / "run").compile_project()


def test_project_copy_does_not_recurse_when_runs_live_inside_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    destination = project / "runs" / "workspace" / "project"
    destination.parent.mkdir(parents=True)

    _copy_project(project, destination)

    assert (destination / "Assets").is_dir()
    assert not (destination / "runs").exists()


@pytest.mark.skipif(
    os.environ.get("GAMEFORGE_REAL_UNITY_TESTS") != "1" or not _PINNED_UNITY.is_file(),
    reason="explicit real Unity integration run is disabled",
)
def test_real_unity_project_compiles_after_minimal_open_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    source_root = Path(__file__).resolve().parents[1] / "source"
    project = source_root / "unity" / "ArenaTemplate"
    events = (
        {"type": "thread.started", "thread_id": "real-unity"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "workspace inspected"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    )
    codex = _executable(
        tmp_path / "codex",
        "printf '%s\\n' " + " ".join(shlex.quote(json.dumps(event)) for event in events) + "\n",
    )
    monkeypatch.setenv("UNITY_EDITOR_PATH", str(_PINNED_UNITY))
    profile = ModelProfile(
        provider=ModelProvider.CODEX_SUBSCRIPTION,
        model="test-model",
        executable=codex,
    )

    run = run_open_unity_workspace(
        root=root,
        project=project,
        request="Inspect the project and preserve its valid state.",
        model_profile=profile,
        task_id="unity-real-compile-smoke",
        timeout_seconds=600,
    )

    assert run.harness_run.result.status.value == "PASS"
