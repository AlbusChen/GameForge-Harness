from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from gameforge.adapters.llm import (
    AgentCliLanguageModel,
    CostLedger,
    MockLanguageModel,
    ModelError,
    ModelResponse,
    ModelUsage,
    OpenAICompatibleLanguageModel,
    _parse_agent_cli_jsonl,
    _parse_agent_cli_tool_audit,
    _temporary_workspace_pre_tool_hook,
)
from gameforge.schemas.model import PlanningDecision


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.headers = {"x-request-id": "request-123"}

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *arguments: object) -> None:
        del arguments

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_mock_model_returns_typed_zero_cost_decision() -> None:
    response = MockLanguageModel().complete(system="system", prompt="prompt")
    decision = PlanningDecision.from_json(response.text)

    assert decision.decision == "use_deterministic_plan"
    assert response.usage == ModelUsage(0, 0, 0.0)


def test_cost_ledger_enforces_total_budget() -> None:
    ledger = CostLedger(maximum_usd=0.01)
    ledger.record(ModelResponse("{}", ModelUsage(10, 5, 0.006)))

    with pytest.raises(ModelError, match="budget"):
        ledger.record(ModelResponse("{}", ModelUsage(10, 5, 0.005)))

    assert ledger.total_usd == 0.006


def test_agent_cli_jsonl_parser_retains_cache_and_reasoning_usage() -> None:
    payload = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "request-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"kind":"finish"}'},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 12,
                    },
                }
            ),
        )
    )

    message, usage, request_id = _parse_agent_cli_jsonl(payload)

    assert message == '{"kind":"finish"}'
    assert request_id == "request-1"
    assert usage == ModelUsage(100, 20, 0, cached_input_tokens=80, reasoning_output_tokens=12)


def test_agent_cli_timeout_terminates_descendants_holding_output_pipe(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "slow-agent"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    model = AgentCliLanguageModel(executable, "test-model", timeout_seconds=0.1)

    started = time.monotonic()
    with pytest.raises(ModelError, match="timed out"):
        model.complete(system="system", prompt="prompt")

    assert time.monotonic() - started < 2


def test_agent_cli_passes_bounded_image_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    image = tmp_path / "reference.png"
    image.write_bytes(b"bounded-image")
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        timeout_seconds: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        captured.update(
            {
                "command": command,
                "timeout_seconds": timeout_seconds,
                "environment": environment,
            }
        )
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": '{"verdict":"pass"}'},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    }
                ),
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("gameforge.adapters.llm._run_agent_cli_process", fake_run)

    response = AgentCliLanguageModel(
        executable,
        "test-model",
        timeout_seconds=12,
        reasoning_effort="medium",
        image_paths=(image,),
    ).complete(system="system", prompt="prompt")

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="medium"'
    assert command[command.index("--image") + 1] == str(image)
    assert command[-2] == "--"
    assert "explicitly allowed to request any typed Harness tool" in command[-1]
    assert "Do not use tools or inspect local files" not in command[-1]
    assert response.text == '{"verdict":"pass"}'


def test_agent_cli_workspace_session_uses_native_tools_with_write_sandbox(
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
        captured.update(
            command=command,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        stdout = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "workspace-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 60,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 4,
                        },
                    }
                ),
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("gameforge.adapters.llm._run_agent_cli_process", fake_run)

    response = AgentCliLanguageModel(
        executable,
        "test-model",
        timeout_seconds=12,
        reasoning_effort="medium",
    ).run_workspace_agent(
        project=project,
        prompt="Use native tools to solve the public task.",
        timeout_seconds=90,
        environment_overrides={"PATH": "/private/safe-bin:/usr/bin", "GODOT": "/safe/godot"},
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:5] == [str(executable), "-a", "never", "exec", "--ephemeral"]
    assert command[command.index("-s") + 1] == "workspace-write"
    assert command[command.index("-C") + 1] == str(project)
    assert "danger-full-access" not in command
    assert captured["timeout_seconds"] == 90
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["PATH"] == "/private/safe-bin:/usr/bin"
    assert environment["GODOT"] == "/safe/godot"
    assert response.provider == "agent-cli-workspace"
    assert response.request_id == "workspace-1"
    assert response.usage.reasoning_output_tokens == 4
    assert response.tool_audit == ()


def test_agent_cli_workspace_session_installs_pre_tool_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    captured: list[str] = []
    captured_hook: dict[str, object] = {}

    def fake_run(
        command: list[str],
        *,
        timeout_seconds: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, environment
        captured.extend(command)
        captured_hook.update(
            json.loads((project / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        )
        stdout = "\n".join(
            (
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
        pre_tool_use_hook_command=(
            "/usr/bin/python3",
            "-m",
            "gameforge.harness.native_tool_policy",
            "godot",
        ),
    ).run_workspace_agent(
        project=project,
        prompt="test the fixed Godot boundary",
        timeout_seconds=10,
    )

    assert "--dangerously-bypass-hook-trust" in captured
    group = captured_hook["hooks"]["PreToolUse"][0]  # type: ignore[index]
    assert group["matcher"] == "^Bash$"
    assert (
        group["hooks"][0]["command"]
        == "/usr/bin/python3 -m gameforge.harness.native_tool_policy godot"
    )
    assert captured[captured.index("-s") + 1] == "workspace-write"
    assert not (project / ".codex").exists()


def test_temporary_workspace_hook_restores_existing_config_exactly(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = project / ".codex" / "hooks.json"
    config.parent.mkdir(parents=True)
    original = b'{"description":"user hook","hooks":{"PreToolUse":[]}}\n'
    config.write_bytes(original)

    with _temporary_workspace_pre_tool_hook(
        project,
        command=("/usr/bin/true",),
    ):
        active = json.loads(config.read_text(encoding="utf-8"))
        assert len(active["hooks"]["PreToolUse"]) == 1

    assert config.read_bytes() == original


def test_agent_cli_tool_audit_omits_command_content_and_redacts_outside_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    payload = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "secret-tool --token should-not-survive",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "file_change",
                        "changes": [
                            {"path": str(workspace / "scene.tscn"), "kind": "update"},
                            {"path": str(tmp_path / "outside.txt"), "kind": "add"},
                        ],
                        "status": "completed",
                    },
                }
            ),
        )
    )

    audit = _parse_agent_cli_tool_audit(payload, workspace=workspace)

    assert len(audit) == 2
    assert "should-not-survive" not in json.dumps(audit)
    assert audit[1]["changes"] == [
        {"path": "scene.tscn", "within_project": True, "change_kind": "update"},
        {"path": "$OUTSIDE_PROJECT", "within_project": False, "change_kind": "add"},
    ]


def test_agent_cli_can_attach_new_run_evidence_on_a_later_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    initial = tuple(tmp_path / f"initial-{index}.png" for index in range(6))
    for path in initial:
        path.write_bytes(b"png")
    dynamic = tmp_path / "current-revision.png"
    dynamic.write_bytes(b"png")
    captured: list[str] = []

    def fake_run(
        command: list[str],
        *,
        timeout_seconds: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, environment
        captured.extend(
            command[index + 1] for index, value in enumerate(command) if value == "--image"
        )
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": '{"kind":"finish"}'},
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
    model = AgentCliLanguageModel(executable, "test-model", image_paths=initial)

    model.complete_with_images(
        system="system",
        prompt="prompt",
        image_paths=(dynamic,),
    )

    assert captured == [*(str(path) for path in initial[:5]), str(dynamic)]


def test_agent_cli_retries_one_zero_turn_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = 0

    def fake_run(
        command: list[str],
        *,
        timeout_seconds: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, environment
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 1, "", "temporary startup failure")
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": '{"kind":"finish"}'},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    }
                ),
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("gameforge.adapters.llm._run_agent_cli_process", fake_run)

    response = AgentCliLanguageModel(executable, "test-model").complete(
        system="system",
        prompt="prompt",
    )

    assert calls == 2
    assert response.transport_attempts == 2
    assert response.trace_record(system="system", prompt="prompt")["transport_attempts"] == 2


def test_http_adapter_sends_key_only_as_header_and_records_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float) -> _FakeHttpResponse:
        captured["authorization"] = request.get_header("Authorization")  # type: ignore[attr-defined]
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["timeout"] = timeout
        return _FakeHttpResponse(
            {
                "model": "example-model-2026-08-01",
                "choices": [{"message": {"content": '{"decision":"ok"}'}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    model = OpenAICompatibleLanguageModel(
        base_url="https://model.example/v1",
        model="example-model",
        api_key="local-secret",
        input_usd_per_million=1.0,
        output_usd_per_million=2.0,
        timeout_seconds=12.0,
    )

    response = model.complete(system="system", prompt="prompt")

    assert captured == {
        "authorization": "Bearer local-secret",
        "url": "https://model.example/v1/chat/completions",
        "timeout": 12.0,
    }
    assert response.usage.cost_usd == 0.002
    assert response.model == "example-model-2026-08-01"
    assert response.request_id == "request-123"
    assert "local-secret" not in repr(model)
    assert "local-secret" not in json.dumps(response.trace_record(system="system", prompt="prompt"))


def test_local_model_file_requires_private_permissions(tmp_path: Path) -> None:
    local_env = tmp_path / ".env.local"
    local_env.write_text(
        "LLM_API_KEY=local-secret\n"
        "LLM_MODEL=example-model\n"
        "LLM_BASE_URL=https://model.example/v1\n",
        encoding="utf-8",
    )
    local_env.chmod(0o644)

    with pytest.raises(PermissionError, match="0600"):
        OpenAICompatibleLanguageModel.from_project(tmp_path)
