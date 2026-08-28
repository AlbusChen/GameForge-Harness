from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.adapters.direct_api_workspace import (
    DirectApiWorkspaceLanguageModel,
    _ShellResult,
)
from gameforge.adapters.llm import AgentCliLanguageModel
from gameforge.benchmarking import gamedevbench
from gameforge.harness.execution_profiles import ExecutionProfile
from gameforge.harness.model_profiles import ModelProfile, ModelProvider


def test_public_subscription_profile_builds_native_open_codex_adapter(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    profile = ModelProfile(
        provider=ModelProvider.CODEX_SUBSCRIPTION,
        model="test-model",
        executable=executable,
    )

    adapter = profile.build_adapter(tmp_path)

    assert isinstance(adapter, AgentCliLanguageModel)
    assert adapter.workspace_sandbox == "danger-full-access"
    assert profile.execution_profile is ExecutionProfile.NATIVE_OPEN


def test_public_responses_profile_builds_native_open_api_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    profile = ModelProfile(
        provider=ModelProvider.OPENAI_RESPONSES_API,
        model="test-model",
    )

    adapter = profile.build_adapter(tmp_path)

    assert isinstance(adapter, DirectApiWorkspaceLanguageModel)
    assert adapter.execution_profile is ExecutionProfile.NATIVE_OPEN
    assert adapter.provider == "openai-responses-api"


def test_strong_isolated_fails_closed_without_external_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="user-supplied isolation_runner"):
        ModelProfile(
            provider=ModelProvider.OPENAI_RESPONSES_API,
            model="test-model",
            execution_profile=ExecutionProfile.STRONG_ISOLATED,
        )


def test_native_open_responses_loop_uses_native_shell_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requests = 0
    responses = [
        {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "shell",
                    "arguments": json.dumps(
                        {"command": "printf done > result.txt", "timeout_seconds": 10}
                    ),
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ]

    def fake_post(
        self: DirectApiWorkspaceLanguageModel,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], str]:
        nonlocal requests
        del self, payload, timeout_seconds
        response = responses[requests]
        requests += 1
        return response, f"request-{requests}"

    def fake_native_shell(**arguments: object) -> _ShellResult:
        assert arguments["workspace"] == workspace
        (workspace / "result.txt").write_text("done", encoding="utf-8")
        return _ShellResult(0, "", "", 0.01)

    monkeypatch.setattr(DirectApiWorkspaceLanguageModel, "_post_response", fake_post)
    monkeypatch.setattr(
        "gameforge.adapters.direct_api_workspace._run_native_shell", fake_native_shell
    )
    model = DirectApiWorkspaceLanguageModel(
        base_url="https://api.openai.com/v1",
        model="test-model",
        api_key="test-key",
    )

    result = model.run_workspace_agent(
        project=workspace,
        prompt="Create result.txt.",
        timeout_seconds=30,
    )

    assert result.text == "done"
    assert result.tool_audit[0]["execution_profile"] == "native-open"
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "done"


def test_unified_minimal_dispatch_accepts_both_public_backends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: list[dict[str, object]] = []
    sentinel = object()

    def fake_run(**arguments: object) -> object:
        captured.append(arguments)
        return sentinel

    monkeypatch.setattr(gamedevbench, "_run_gamedevbench_minimal_harness", fake_run)
    profiles = (
        ModelProfile(
            provider=ModelProvider.CODEX_SUBSCRIPTION,
            model="test-model",
            executable=executable,
        ),
        ModelProfile(
            provider=ModelProvider.OPENAI_RESPONSES_API,
            model="test-model",
        ),
    )

    for profile in profiles:
        result = gamedevbench.run_gamedevbench_profile_minimal_harness(
            root=tmp_path,
            benchmark_root=tmp_path / "benchmark",
            task_id="task_0001",
            model_profile=profile,
        )
        assert result is sentinel

    assert [item["solver_provider"] for item in captured] == [
        "codex-subscription",
        "openai-responses-api",
    ]
    assert all(item["execution_profile"] is ExecutionProfile.NATIVE_OPEN for item in captured)
