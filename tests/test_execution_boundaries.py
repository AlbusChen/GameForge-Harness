from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from gameforge.adapters.direct_api_workspace import _run_isolated_shell
from gameforge.adapters.llm import AgentCliLanguageModel
from gameforge.harness.engine_tools import unity_tool_session
from gameforge.harness.execution_profiles import ExecutionProfile
from gameforge.harness.isolation_runner import ExternalIsolationRunner
from gameforge.harness.model_profiles import ModelProfile, ModelProvider
from gameforge.harness.supervised_native_transport import run_client

_PINNED_UNITY = Path("/Applications/Unity/Hub/Editor/6000.3.20f1/Unity.app/Contents/MacOS/Unity")


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_isolation_runner(path: Path) -> Path:
    description = json.dumps(
        {
            "protocol": "gameforge-isolation-runner-v1",
            "isolation": "strong",
            "operations": ["shell", "workspace-agent"],
        },
        separators=(",", ":"),
    )
    return _executable(
        path,
        f"""
if [ "$1" = describe ]; then
  printf '%s\\n' {shlex.quote(description)}
  exit 0
fi
shift
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
""",
    )


def test_native_unity_command_survives_login_shell(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    editor = _executable(tmp_path / "Unity", "printf '%s\\n' fake-unity-1\n")

    with unity_tool_session(
        profile=ExecutionProfile.NATIVE_OPEN,
        workspace=workspace,
        editor=editor,
        timeout_seconds=30,
    ) as session:
        environment = os.environ.copy()
        environment.update(session.environment)
        completed = subprocess.run(
            ["/bin/zsh", "-lc", "command -v unity; unity -version"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            cwd=workspace,
        )

    assert completed.stdout.splitlines()[1] == "fake-unity-1"


def test_supervised_unity_transport_preserves_arguments_and_host_home(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    editor = _executable(
        tmp_path / "Unity",
        "printf 'home=%s\\n' \"$HOME\"\nprintf 'args='\nprintf '<%s>' \"$@\"\nprintf '\\n'\n",
    )

    with unity_tool_session(
        profile=ExecutionProfile.SUPERVISED_NATIVE,
        workspace=workspace,
        editor=editor,
        timeout_seconds=30,
    ) as session:
        environment = os.environ.copy()
        environment.update(session.environment)
        completed = subprocess.run(
            ["/bin/zsh", "-lc", "unity 'argument with spaces' --flag=value"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            cwd=workspace,
        )
        receipt = session.receipt()

    assert completed.returncode == 0, completed.stderr
    assert f"home={os.environ['HOME']}" in completed.stdout
    assert "args=<argument with spaces><--flag=value>" in completed.stdout
    assert receipt["argument_policy"] == "model-selected-unmodified"
    assert len(receipt["events"]) == 1
    assert receipt["events"][0]["attempt_count"] == 1


@pytest.mark.skipif(not _PINNED_UNITY.is_file(), reason="pinned Unity is not installed")
@pytest.mark.parametrize(
    "profile",
    (ExecutionProfile.NATIVE_OPEN, ExecutionProfile.SUPERVISED_NATIVE),
)
def test_real_unity_version_through_release_profiles(
    tmp_path: Path,
    profile: ExecutionProfile,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with unity_tool_session(
        profile=profile,
        workspace=workspace,
        editor=_PINNED_UNITY,
        timeout_seconds=60,
    ) as session:
        environment = os.environ.copy()
        environment.update(session.environment)
        completed = subprocess.run(
            ["/bin/zsh", "-lc", "unity -version"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            cwd=workspace,
            timeout=90,
        )

    assert completed.returncode == 0, completed.stderr
    assert "6000.3.20f1" in completed.stdout + completed.stderr


def test_supervised_transport_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    editor = _executable(tmp_path / "Unity", "printf should-not-run\n")

    with unity_tool_session(
        profile=ExecutionProfile.SUPERVISED_NATIVE,
        workspace=workspace,
        editor=editor,
        timeout_seconds=30,
    ) as session:
        assert session.transport is not None
        original = Path.cwd()
        os.chdir(outside)
        try:
            return_code = run_client(
                exchange_root=session.transport.exchange_root,
                token=session.transport._token,
                arguments=("-version",),
                timeout_seconds=30,
            )
        finally:
            os.chdir(original)

    assert return_code == 125
    assert session.transport.events[0]["rejected"] is True


def test_external_isolation_runner_requires_attested_protocol(tmp_path: Path) -> None:
    invalid = _executable(tmp_path / "runner", "printf '%s\\n' '{}'\n")

    with pytest.raises(ValueError, match="protocol"):
        ExternalIsolationRunner(invalid).validate("shell")


def test_strong_isolated_shell_uses_external_runner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _fake_isolation_runner(tmp_path / "runner")
    result = _run_isolated_shell(
        command="printf isolated > result.txt",
        workspace=workspace,
        shell_executable=Path("/bin/sh"),
        environment=os.environ.copy(),
        timeout_seconds=30,
        isolation_runner=runner,
    )

    assert result.returncode == 0
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "isolated"


def test_strong_subscription_wraps_complete_workspace_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex = _executable(tmp_path / "codex", "exit 0\n")
    runner = _fake_isolation_runner(tmp_path / "runner")
    profile = ModelProfile(
        provider=ModelProvider.CODEX_SUBSCRIPTION,
        model="test-model",
        executable=codex,
        execution_profile=ExecutionProfile.STRONG_ISOLATED,
        isolation_runner=runner,
    )
    assert profile.isolation_runner_is_configured(tmp_path)
    adapter = profile.build_adapter(tmp_path)
    assert isinstance(adapter, AgentCliLanguageModel)
    captured: dict[str, object] = {}

    def fake_process(
        command: list[str],
        *,
        timeout_seconds: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, environment
        captured["command"] = command
        output = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "isolated"}),
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
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("gameforge.adapters.llm._run_agent_cli_process", fake_process)
    adapter.run_workspace_agent(
        project=workspace,
        prompt="Build a game.",
        timeout_seconds=30,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == [str(runner), "workspace-agent"]
    assert "danger-full-access" in command
