from __future__ import annotations

import io
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from gameforge.adapters.direct_api_workspace import (
    DirectApiWorkspaceLanguageModel,
    _BoundedStreamCapture,
    _invoke_image_tool,
    _invoke_shell_tool,
    _run_sandboxed_shell,
    _sandbox_profile,
    _ShellResult,
    _tool_environment,
)
from gameforge.benchmarking import gamedevbench
from gameforge.harness.execution_profiles import ExecutionProfile
from gameforge.harness.model_profiles import ModelProfile, ModelProvider


def _model() -> DirectApiWorkspaceLanguageModel:
    return DirectApiWorkspaceLanguageModel(
        base_url="https://api.openai.com/v1",
        model="test-model",
        api_key="secret-api-key",
        reasoning_effort="medium",
        execution_profile=ExecutionProfile.SUPERVISED_NATIVE,
    )


def test_responses_tool_loop_preserves_output_items_and_audits_without_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requests: list[dict[str, object]] = []
    tool_environments: list[dict[str, str]] = []
    responses = [
        {
            "status": "completed",
            "output": [
                {
                    "id": "reason-1",
                    "type": "reasoning",
                    "encrypted_content": "opaque-reasoning",
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "shell",
                    "arguments": json.dumps(
                        {"command": "printf done > result.txt", "timeout_seconds": 10}
                    ),
                },
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 60},
                "output_tokens_details": {"reasoning_tokens": 12},
            },
        },
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Implemented and checked."}],
                }
            ],
            "usage": {
                "input_tokens": 140,
                "output_tokens": 10,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens_details": {"reasoning_tokens": 4},
            },
        },
    ]

    def fake_post(
        self: DirectApiWorkspaceLanguageModel,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], str]:
        del self
        assert timeout_seconds > 0
        requests.append(payload)
        return responses[len(requests) - 1], f"request-{len(requests)}"

    def fake_shell(**arguments: object) -> _ShellResult:
        environment = arguments["environment"]
        assert isinstance(environment, dict)
        tool_environments.append(environment)
        (workspace / "result.txt").write_text("done", encoding="utf-8")
        return _ShellResult(0, "", "", 0.01)

    monkeypatch.setattr(DirectApiWorkspaceLanguageModel, "_post_response", fake_post)
    monkeypatch.setattr("gameforge.adapters.direct_api_workspace._run_sandboxed_shell", fake_shell)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-tools")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-tools")

    response = _model().run_workspace_agent(
        project=workspace,
        prompt="Create result.txt.",
        timeout_seconds=30,
        environment_overrides={"GAMEFORGE_TEST_MODE": "1"},
    )

    assert response.text == "Implemented and checked."
    assert response.request_id == "request-2"
    assert response.transport_attempts == 2
    assert response.usage.input_tokens == 240
    assert response.usage.cached_input_tokens == 160
    assert response.usage.output_tokens == 30
    assert response.usage.reasoning_output_tokens == 16
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "done"
    assert len(response.tool_audit) == 1
    audit = response.tool_audit[0]
    assert audit["tool"] == "shell"
    assert "command" not in audit
    assert audit["changed_paths"] == ["result.txt"]
    assert "OPENAI_API_KEY" not in tool_environments[0]
    assert "AWS_SECRET_ACCESS_KEY" not in tool_environments[0]
    assert tool_environments[0]["GAMEFORGE_TEST_MODE"] == "1"

    assert requests[0]["store"] is False
    assert requests[0]["parallel_tool_calls"] is False
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    second_input = requests[1]["input"]
    assert isinstance(second_input, list)
    assert any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content") == "opaque-reasoning"
        for item in second_input
    )
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "call-1"
        for item in second_input
    )


def test_image_tool_accepts_workspace_image_and_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "frame.png"
    image.write_bytes(b"small-png-placeholder")
    output, audit = _invoke_image_tool(
        arguments={"path": "frame.png", "detail": "high"},
        workspace=workspace,
    )
    assert isinstance(output, list)
    assert output[1]["type"] == "input_image"
    assert str(output[1]["image_url"]).startswith("data:image/png;base64,")
    assert audit["status"] == "success"
    assert audit["path"] == "frame.png"

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    (workspace / "escape.png").symlink_to(outside)
    rejected, rejected_audit = _invoke_image_tool(
        arguments={"path": "escape.png", "detail": "low"},
        workspace=workspace,
    )
    assert isinstance(rejected, str)
    assert json.loads(rejected)["status"] == "error"
    assert rejected_audit["status"] == "rejected"


@pytest.mark.skipif(
    not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec is required",
)
def test_real_shell_sandbox_allows_workspace_write_and_denies_sibling_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    outside = tmp_path / "outside.txt"
    environment = _tool_environment(scratch=scratch, environment_overrides=None)
    profile = _sandbox_profile(
        workspace=workspace,
        scratch=scratch,
        shell_executable=Path("/bin/sh").resolve(strict=True),
        trusted_read_roots=(),
        environment=environment,
    )
    result = _run_sandboxed_shell(
        command=(f"printf inside > inside.txt; printf escape > {shlex.quote(str(outside))}"),
        workspace=workspace,
        scratch=scratch,
        sandbox_executable=Path("/usr/bin/sandbox-exec"),
        shell_executable=Path("/bin/sh"),
        profile=profile,
        environment=environment,
        timeout_seconds=10,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("the parent test process is already sandboxed")
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "inside"
    assert not outside.exists()
    assert result.returncode != 0


def test_model_profile_builds_direct_workspace_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    profile = ModelProfile(
        provider=ModelProvider.OPENAI_RESPONSES_WORKSPACE,
        model="test-model",
        reasoning_effort="medium",
        max_output_tokens=16_000,
    )
    adapter = profile.build_adapter(tmp_path)
    assert isinstance(adapter, DirectApiWorkspaceLanguageModel)
    assert adapter.max_output_tokens == 16_000
    assert adapter.reasoning_effort == "medium"


def test_direct_gamedevbench_entry_uses_distinct_provider_and_same_minimal_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    tools = tmp_path / "tools"
    runtime_root.mkdir()
    tools.mkdir()
    process_lock = tmp_path / "godot-process.lock"
    process_lock.touch()
    monkeypatch.setattr(gamedevbench, "_GODOT_PROCESS_LOCK", process_lock)
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_run(**arguments: object) -> object:
        captured.update(arguments)
        factory = arguments["model_factory"]
        assert callable(factory)
        runtime = gamedevbench.GodotRuntimeSandbox(
            executable=Path("/opt/homebrew/bin/godot"),
            source_executable=Path("/opt/homebrew/bin/godot"),
            root=runtime_root,
            mode="test",
        )
        built = factory(runtime, tools)
        assert isinstance(built, DirectApiWorkspaceLanguageModel)
        assert runtime_root in built.host_runtime_roots
        assert tools in built.host_runtime_roots
        assert process_lock.resolve() in built.host_runtime_files
        assert Path(gamedevbench.__file__).resolve().parents[2] in built.trusted_read_roots
        return sentinel

    monkeypatch.setattr(gamedevbench, "_run_gamedevbench_minimal_harness", fake_run)
    profile = ModelProfile(
        provider=ModelProvider.OPENAI_RESPONSES_WORKSPACE,
        model="test-model",
        reasoning_effort="high",
        max_output_tokens=32_768,
    )
    result = gamedevbench.run_gamedevbench_direct_api_minimal_harness(
        root=tmp_path,
        benchmark_root=tmp_path / "benchmark",
        task_id="task_0001",
        model_profile=profile,
    )

    assert result is sentinel
    assert captured["solver_provider"] == "openai-responses-workspace"
    assert captured["solver_profile"] == "direct-api-minimal-open"
    fingerprint = captured["fingerprint_payload"]
    assert isinstance(fingerprint, dict)
    assert fingerprint["harness_protocol"] == "minimal-open-v1"
    assert fingerprint["intermediate_validation"] is False
    assert fingerprint["generic_tools"] == ["shell", "view_image"]


def test_stream_capture_bounds_retained_memory_but_hashes_full_output() -> None:
    payload = b"a" * (256 * 1024) + b"tail"
    capture = _BoundedStreamCapture()
    capture.consume(io.BytesIO(payload))

    retained = capture.text().encode("utf-8")
    assert capture.byte_count == len(payload)
    assert capture.truncated is True
    assert len(retained) <= 64 * 1024
    assert b"output truncated" in retained
    assert retained.endswith(b"tail")
    import hashlib

    assert capture.hexdigest() == hashlib.sha256(payload).hexdigest()


def test_shell_start_failure_is_a_structured_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()

    def fail_start(**arguments: object) -> _ShellResult:
        del arguments
        raise OSError("synthetic start failure")

    monkeypatch.setattr("gameforge.adapters.direct_api_workspace._run_sandboxed_shell", fail_start)
    output, audit = _invoke_shell_tool(
        arguments={"command": "true", "timeout_seconds": 1},
        workspace=workspace,
        scratch=scratch,
        sandbox_executable=Path("/usr/bin/sandbox-exec"),
        shell_executable=Path("/bin/sh"),
        profile="(version 1)\n(deny default)\n",
        environment={},
        remaining_seconds=10,
    )

    assert json.loads(output) == {
        "error": "shell tool could not start",
        "status": "error",
    }
    assert audit["status"] == "infrastructure_error"
    assert audit["error_type"] == "OSError"


@pytest.mark.skipif(
    not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec is required",
)
def test_real_shell_sandbox_bounds_output_times_out_and_cleans_background_child(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    environment = _tool_environment(scratch=scratch, environment_overrides=None)
    profile = _sandbox_profile(
        workspace=workspace,
        scratch=scratch,
        shell_executable=Path("/bin/sh").resolve(strict=True),
        trusted_read_roots=(),
        environment=environment,
    )
    common = {
        "workspace": workspace,
        "scratch": scratch,
        "sandbox_executable": Path("/usr/bin/sandbox-exec"),
        "shell_executable": Path("/bin/sh"),
        "profile": profile,
        "environment": environment,
    }

    flood = _run_sandboxed_shell(
        command="/usr/bin/python3 -c \"import sys; sys.stdout.write('x' * 1048576)\"",
        timeout_seconds=10,
        **common,
    )
    if flood.returncode == 71 and "sandbox_apply: Operation not permitted" in flood.stderr:
        pytest.skip("the parent test process is already sandboxed")
    assert flood.returncode == 0
    assert flood.stdout_bytes == 1_048_576
    assert flood.stdout_truncated is True
    assert len(flood.stdout.encode("utf-8")) <= 64 * 1024

    started = time.monotonic()
    timeout = _run_sandboxed_shell(
        command="sleep 30",
        timeout_seconds=0.2,
        **common,
    )
    assert timeout.timed_out is True
    assert time.monotonic() - started < 5

    background = _run_sandboxed_shell(
        command="sleep 30 >/dev/null 2>&1 & echo $! > child.pid",
        timeout_seconds=5,
        **common,
    )
    assert background.returncode == 0
    child_pid = int((workspace / "child.pid").read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"background child survived process-group cleanup: {child_pid}")


@pytest.mark.skipif(
    not Path("/usr/bin/sandbox-exec").is_file() or not Path("/opt/homebrew/bin/godot").is_file(),
    reason="macOS sandbox-exec and Godot are required",
)
def test_real_direct_shell_can_invoke_host_managed_godot_transport(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    host_runtime = tmp_path / "host-runtime"
    private_home = host_runtime / "home"
    workspace.mkdir()
    scratch.mkdir()
    private_home.mkdir(parents=True)
    process_lock = tmp_path / "godot-process.lock"
    process_lock.touch()
    process_mutex_script = (
        Path(gamedevbench.__file__).resolve().parents[1] / "harness" / "process_mutex.py"
    )
    wrapper = host_runtime / "godot"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(str(Path(sys.executable).resolve(strict=True)))} "
        f"{shlex.quote(str(process_mutex_script))} "
        f"--lock-file {shlex.quote(str(process_lock))} "
        "--executable /opt/homebrew/bin/godot --crash-attempts 1 "
        f'--private-home-root {shlex.quote(str(private_home))} -- "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = _tool_environment(
        scratch=scratch,
        environment_overrides={
            "PATH": f"{host_runtime}{os.pathsep}/opt/homebrew/bin:/usr/bin:/bin",
            "GODOT": str(wrapper),
        },
    )
    profile = _sandbox_profile(
        workspace=workspace,
        scratch=scratch,
        shell_executable=Path("/bin/sh").resolve(strict=True),
        trusted_read_roots=(
            Path(gamedevbench.__file__).resolve().parents[2],
            Path(sys.executable).resolve(strict=True).parents[1],
        ),
        host_runtime_roots=(host_runtime,),
        host_runtime_files=(process_lock,),
        environment=environment,
    )
    result = _run_sandboxed_shell(
        command="godot --version",
        workspace=workspace,
        scratch=scratch,
        sandbox_executable=Path("/usr/bin/sandbox-exec"),
        shell_executable=Path("/bin/sh"),
        profile=profile,
        environment=environment,
        timeout_seconds=30,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("the parent test process is already sandboxed")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("4.")


@pytest.mark.skipif(
    not Path("/Applications/Unity/Hub/Editor/6000.3.20f1/Unity.app/Contents/MacOS/Unity").is_file(),
    reason="the pinned Unity editor is required",
)
def test_real_direct_shell_can_invoke_pinned_unity_editor_version(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    environment = _tool_environment(scratch=scratch, environment_overrides=None)
    profile = _sandbox_profile(
        workspace=workspace,
        scratch=scratch,
        shell_executable=Path("/bin/sh").resolve(strict=True),
        trusted_read_roots=(),
        environment=environment,
    )
    unity = "/Applications/Unity/Hub/Editor/6000.3.20f1/Unity.app/Contents/MacOS/Unity"
    result = _run_sandboxed_shell(
        command=f"{shlex.quote(unity)} -version",
        workspace=workspace,
        scratch=scratch,
        sandbox_executable=Path("/usr/bin/sandbox-exec"),
        shell_executable=Path("/bin/sh"),
        profile=profile,
        environment=environment,
        timeout_seconds=30,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("the parent test process is already sandboxed")
    assert result.returncode == 0, result.stderr
    assert "6000.3.20f1" in (result.stdout + result.stderr)
