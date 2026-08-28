from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gameforge.adapters.llm import AgentCliLanguageModel


def test_native_open_workspace_session_selects_full_access_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        timeout_seconds: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        captured["command"] = command
        captured["environment"] = environment
        stdout = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "native-open-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ),
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("gameforge.adapters.llm._run_agent_cli_process", fake_run)

    AgentCliLanguageModel(
        executable,
        "test-model",
        workspace_sandbox="danger-full-access",
    ).run_workspace_agent(
        project=project,
        prompt="Use the native engine freely.",
        timeout_seconds=10,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("-s") + 1] == "danger-full-access"
    assert command[command.index("-C") + 1] == str(project)
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["GIT_CEILING_DIRECTORIES"] == str(project.parent)


def test_workspace_session_keeps_workspace_write_as_safe_default(tmp_path: Path) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    model = AgentCliLanguageModel(executable, "test-model")

    assert model.workspace_sandbox == "workspace-write"


def test_workspace_session_rejects_unknown_sandbox(tmp_path: Path) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sandbox is invalid"):
        AgentCliLanguageModel(
            executable,
            "test-model",
            workspace_sandbox="unknown",
        )
