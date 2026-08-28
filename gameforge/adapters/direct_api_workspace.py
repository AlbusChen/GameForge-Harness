from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from gameforge.adapters.llm import ModelError, ModelResponse, ModelUsage
from gameforge.harness.execution_profiles import ExecutionProfile
from gameforge.harness.isolation_runner import ExternalIsolationRunner
from gameforge.harness.preservation import snapshot_project

_MAXIMUM_COMMAND_BYTES = 256 * 1024
_MAXIMUM_TOOL_OUTPUT_BYTES = 64 * 1024
_MAXIMUM_IMAGE_BYTES = 8 * 1024 * 1024
_MAXIMUM_SHELL_SECONDS = 180
_TERMINATION_GRACE_SECONDS = 2
_IMAGE_SUFFIXES = {".jpeg", ".jpg", ".png", ".webp"}
_TRUNCATION_MARKER = b"\n... output truncated ...\n"


@dataclass(frozen=True)
class _ShellResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass
class _BoundedStreamCapture:
    """Continuously drain a pipe while retaining only bounded head/tail bytes."""

    byte_count: int = 0
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    digest: object = field(default_factory=hashlib.sha256)

    def consume(self, stream: object) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    break
                assert isinstance(chunk, bytes)
                self.byte_count += len(chunk)
                self.digest.update(chunk)  # type: ignore[attr-defined]
                remaining_head = _MAXIMUM_TOOL_OUTPUT_BYTES // 2 - len(self.head)
                if remaining_head > 0:
                    self.head.extend(chunk[:remaining_head])
                    chunk = chunk[remaining_head:]
                if chunk:
                    self.tail.extend(chunk)
                    maximum_tail = _MAXIMUM_TOOL_OUTPUT_BYTES // 2
                    if len(self.tail) > maximum_tail:
                        del self.tail[:-maximum_tail]
        except OSError:
            return
        finally:
            with suppress(OSError):
                stream.close()  # type: ignore[attr-defined]

    @property
    def truncated(self) -> bool:
        return self.byte_count > _MAXIMUM_TOOL_OUTPUT_BYTES

    def text(self) -> str:
        if not self.truncated:
            data = bytes(self.head + self.tail)
        else:
            retained = _MAXIMUM_TOOL_OUTPUT_BYTES - len(_TRUNCATION_MARKER)
            head_bytes = retained // 2
            tail_bytes = retained - head_bytes
            data = (
                bytes(self.head[:head_bytes]) + _TRUNCATION_MARKER + bytes(self.tail[-tail_bytes:])
            )
        return data.decode("utf-8", errors="replace")

    def hexdigest(self) -> str:
        return self.digest.hexdigest()  # type: ignore[attr-defined,no-any-return]


@dataclass(frozen=True)
class DirectApiWorkspaceLanguageModel:
    """A thin Responses API loop with only generic workspace shell and image tools."""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    provider: str = "openai-responses-workspace"
    request_timeout_seconds: float = 300.0
    max_output_tokens: int = 32_768
    maximum_tool_calls: int = 128
    reasoning_effort: str | None = None
    input_usd_per_million: float = 0.0
    cached_input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    sandbox_executable: Path = Path("/usr/bin/sandbox-exec")
    shell_executable: Path = Path("/bin/sh")
    trusted_read_roots: tuple[Path, ...] = ()
    host_runtime_roots: tuple[Path, ...] = ()
    host_runtime_files: tuple[Path, ...] = ()
    execution_profile: ExecutionProfile = ExecutionProfile.NATIVE_OPEN
    isolation_runner: Path | None = None

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Responses API base_url must be an absolute HTTPS URL")
        if not self.model.strip():
            raise ValueError("Responses API model must not be empty")
        if not self.api_key:
            raise ValueError("Responses API key must not be empty")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Responses API request timeout must be positive")
        if not 1 <= self.max_output_tokens <= 128_000:
            raise ValueError("Responses API max_output_tokens must be between 1 and 128000")
        if not 1 <= self.maximum_tool_calls <= 1000:
            raise ValueError("Responses API maximum_tool_calls must be between 1 and 1000")
        if self.reasoning_effort not in {
            None,
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Responses API reasoning effort is invalid")
        if any(
            price < 0
            for price in (
                self.input_usd_per_million,
                self.cached_input_usd_per_million,
                self.output_usd_per_million,
            )
        ):
            raise ValueError("Responses API token prices cannot be negative")
        sandbox = self.sandbox_executable.resolve(
            strict=self.execution_profile is ExecutionProfile.SUPERVISED_NATIVE
        )
        if self.execution_profile is ExecutionProfile.SUPERVISED_NATIVE and not sandbox.is_file():
            raise ValueError("supervised-native requires macOS sandbox-exec")
        shell = self.shell_executable.resolve(strict=True)
        trusted = tuple(path.resolve(strict=True) for path in self.trusted_read_roots)
        host_runtime = tuple(path.resolve(strict=True) for path in self.host_runtime_roots)
        host_files = tuple(path.resolve(strict=True) for path in self.host_runtime_files)
        if not all(path.is_dir() for path in trusted):
            raise ValueError("trusted Responses API read roots must be directories")
        if not all(path.is_dir() for path in host_runtime):
            raise ValueError("Responses API host runtime roots must be directories")
        if not all(path.is_file() and not path.is_symlink() for path in host_files):
            raise ValueError("Responses API host runtime files must be regular files")
        object.__setattr__(self, "sandbox_executable", sandbox)
        object.__setattr__(self, "shell_executable", shell)
        object.__setattr__(self, "trusted_read_roots", trusted)
        object.__setattr__(self, "host_runtime_roots", host_runtime)
        object.__setattr__(self, "host_runtime_files", host_files)
        if self.execution_profile is ExecutionProfile.STRONG_ISOLATED:
            if self.isolation_runner is None:
                raise ValueError("strong-isolated requires an external isolation runner")
            runner = self.isolation_runner.resolve(strict=True)
            ExternalIsolationRunner(runner).validate("shell")
            object.__setattr__(self, "isolation_runner", runner)
        elif self.isolation_runner is not None:
            raise ValueError("isolation_runner is only valid with strong-isolated")

    def run_workspace_agent(
        self,
        *,
        project: Path,
        prompt: str,
        timeout_seconds: float,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> ModelResponse:
        workspace = project.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("Direct API workspace project must be a directory")
        if not prompt.strip():
            raise ValueError("Direct API workspace prompt must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("Direct API workspace timeout must be positive")
        started = time.monotonic()
        deadline = started + timeout_seconds
        input_items: list[dict[str, object]] = [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ]
        audit: list[dict[str, object]] = []
        final_text = ""
        request_id: str | None = None
        input_tokens = 0
        cached_input_tokens = 0
        output_tokens = 0
        reasoning_output_tokens = 0
        tool_calls = 0
        transport_attempts = 0

        with tempfile.TemporaryDirectory(prefix="gameforge-direct-api-") as scratch_raw:
            scratch = Path(scratch_raw).resolve(strict=True)
            environment = _tool_environment(
                scratch=scratch,
                environment_overrides=environment_overrides,
                execution_profile=self.execution_profile,
            )
            environment.setdefault("GIT_CEILING_DIRECTORIES", str(workspace.parent))
            profile = (
                _sandbox_profile(
                    workspace=workspace,
                    scratch=scratch,
                    shell_executable=self.shell_executable,
                    trusted_read_roots=self.trusted_read_roots,
                    host_runtime_roots=self.host_runtime_roots,
                    host_runtime_files=self.host_runtime_files,
                    environment=environment,
                )
                if self.execution_profile is ExecutionProfile.SUPERVISED_NATIVE
                else ""
            )
            tools = _workspace_tools()

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelError("timeout", "Direct API workspace session timed out")
                payload: dict[str, object] = {
                    "model": self.model,
                    "instructions": _workspace_instructions(self.execution_profile),
                    "input": input_items,
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                    "max_output_tokens": self.max_output_tokens,
                    "include": ["reasoning.encrypted_content"],
                    "store": False,
                }
                if self.reasoning_effort is not None:
                    payload["reasoning"] = {"effort": self.reasoning_effort}
                transport_attempts += 1
                response, current_request_id = self._post_response(
                    payload,
                    timeout_seconds=min(self.request_timeout_seconds, remaining),
                )
                if current_request_id is not None:
                    request_id = current_request_id
                _validate_response(response)
                usage = _response_usage(response)
                input_tokens += usage.input_tokens
                cached_input_tokens += usage.cached_input_tokens
                output_tokens += usage.output_tokens
                reasoning_output_tokens += usage.reasoning_output_tokens

                output = response.get("output")
                assert isinstance(output, list)
                input_items.extend(item for item in output if isinstance(item, dict))
                calls = [
                    item
                    for item in output
                    if isinstance(item, dict) and item.get("type") == "function_call"
                ]
                if not calls:
                    final_text = _response_text(response)
                    if not final_text:
                        raise ModelError(
                            "invalid_response",
                            "Responses API returned neither a function call nor final text",
                        )
                    break

                if tool_calls + len(calls) > self.maximum_tool_calls:
                    raise ModelError(
                        "tool_budget_exceeded",
                        "Direct API workspace tool-call budget was exceeded",
                    )
                for call in calls:
                    tool_calls += 1
                    call_id = call.get("call_id")
                    name = call.get("name")
                    if not isinstance(call_id, str) or not isinstance(name, str):
                        raise ModelError(
                            "invalid_response",
                            "Responses API function call is missing name or call_id",
                        )
                    raw_arguments = call.get("arguments")
                    try:
                        arguments = (
                            json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
                        )
                    except json.JSONDecodeError:
                        arguments = None
                    if not isinstance(arguments, dict):
                        result: str | list[dict[str, object]] = json.dumps(
                            {"status": "error", "error": "invalid JSON function arguments"},
                            sort_keys=True,
                        )
                        audit.append(
                            {
                                "kind": "tool_call",
                                "tool": name,
                                "status": "invalid_arguments",
                            }
                        )
                    elif name == "shell":
                        result, record = _invoke_shell_tool(
                            arguments=arguments,
                            workspace=workspace,
                            scratch=scratch,
                            sandbox_executable=self.sandbox_executable,
                            shell_executable=self.shell_executable,
                            profile=profile,
                            environment=environment,
                            remaining_seconds=max(0.001, deadline - time.monotonic()),
                            execution_profile=self.execution_profile,
                            isolation_runner=self.isolation_runner,
                        )
                        audit.append(record)
                    elif name == "view_image":
                        result, record = _invoke_image_tool(
                            arguments=arguments,
                            workspace=workspace,
                        )
                        audit.append(record)
                    else:
                        result = json.dumps(
                            {"status": "error", "error": f"unknown tool: {name}"},
                            sort_keys=True,
                        )
                        audit.append({"kind": "tool_call", "tool": name, "status": "unknown_tool"})
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result,
                        }
                    )

        uncached_input = max(0, input_tokens - cached_input_tokens)
        cost = (
            uncached_input * self.input_usd_per_million
            + cached_input_tokens * self.cached_input_usd_per_million
            + output_tokens * self.output_usd_per_million
        ) / 1_000_000
        return ModelResponse(
            text=final_text,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=round(cost, 8),
                cached_input_tokens=cached_input_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
            ),
            provider=self.provider,
            model=self.model,
            request_id=request_id,
            latency_seconds=round(time.monotonic() - started, 3),
            transport_attempts=transport_attempts,
            tool_audit=tuple(audit),
        )

    def _post_response(
        self,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], str | None]:
        endpoint = urljoin(self.base_url.rstrip("/") + "/", "responses")
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "gameforge-direct-api/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
                request_id = response.headers.get("x-request-id")
        except urllib.error.HTTPError as error:
            raise ModelError("http_error", f"Responses API returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ModelError("network_error", "Responses API could not be reached") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelError("invalid_response", "Responses API returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise ModelError("invalid_response", "Responses API response must be an object")
        return decoded, request_id


def _workspace_tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "name": "shell",
            "description": (
                "Run one POSIX shell command in the complete current game-project workspace. "
                "The command may inspect and edit any workspace file and invoke installed local "
                "tools, but has no network and cannot read or write outside approved roots."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAXIMUM_SHELL_SECONDS,
                    },
                },
                "required": ["command", "timeout_seconds"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "view_image",
            "description": (
                "Inspect one PNG, JPEG, or WebP image inside the workspace with native vision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "detail": {"type": "string", "enum": ["low", "high", "auto"]},
                },
                "required": ["path", "detail"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


def _validate_response(response: dict[str, object]) -> None:
    status = response.get("status")
    if status not in {None, "completed"}:
        detail = response.get("error") or response.get("incomplete_details") or status
        raise ModelError("incomplete_response", f"Responses API did not complete: {detail}")
    if not isinstance(response.get("output"), list):
        raise ModelError("invalid_response", "Responses API response is missing output items")


def _response_text(response: dict[str, object]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    texts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                texts.append(part["text"])
    return "\n".join(texts).strip()


def _response_usage(response: dict[str, object]) -> ModelUsage:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return ModelUsage(0, 0, 0.0)
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    cached = int(input_details.get("cached_tokens", 0)) if isinstance(input_details, dict) else 0
    reasoning = (
        int(output_details.get("reasoning_tokens", 0)) if isinstance(output_details, dict) else 0
    )
    try:
        return ModelUsage(
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            0.0,
            cached_input_tokens=cached,
            reasoning_output_tokens=reasoning,
        )
    except (TypeError, ValueError) as error:
        raise ModelError("invalid_response", "Responses API usage is invalid") from error


def _invoke_shell_tool(
    *,
    arguments: dict[str, object],
    workspace: Path,
    scratch: Path,
    sandbox_executable: Path,
    shell_executable: Path,
    profile: str,
    environment: dict[str, str],
    remaining_seconds: float,
    execution_profile: ExecutionProfile = ExecutionProfile.SUPERVISED_NATIVE,
    isolation_runner: Path | None = None,
) -> tuple[str, dict[str, object]]:
    command = arguments.get("command")
    timeout = arguments.get("timeout_seconds")
    if not isinstance(command, str) or not command.strip():
        return _tool_error("shell command must be non-empty"), {
            "kind": "tool_call",
            "tool": "shell",
            "status": "invalid_arguments",
        }
    command_bytes = command.encode("utf-8")
    if len(command_bytes) > _MAXIMUM_COMMAND_BYTES:
        return _tool_error("shell command exceeds 256 KiB"), {
            "kind": "tool_call",
            "tool": "shell",
            "status": "invalid_arguments",
            "command_sha256": hashlib.sha256(command_bytes).hexdigest(),
        }
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= _MAXIMUM_SHELL_SECONDS
    ):
        return _tool_error("timeout_seconds must be between 1 and 180"), {
            "kind": "tool_call",
            "tool": "shell",
            "status": "invalid_arguments",
            "command_sha256": hashlib.sha256(command_bytes).hexdigest(),
        }
    effective_timeout = max(0.001, min(float(timeout), remaining_seconds))
    before = snapshot_project(workspace)
    try:
        if execution_profile is ExecutionProfile.NATIVE_OPEN:
            result = _run_native_shell(
                command=command,
                workspace=workspace,
                shell_executable=shell_executable,
                environment=environment,
                timeout_seconds=effective_timeout,
            )
        elif execution_profile is ExecutionProfile.SUPERVISED_NATIVE:
            result = _run_sandboxed_shell(
                command=command,
                workspace=workspace,
                scratch=scratch,
                sandbox_executable=sandbox_executable,
                shell_executable=shell_executable,
                profile=profile,
                environment=environment,
                timeout_seconds=effective_timeout,
            )
        else:
            if isolation_runner is None:
                raise OSError("strong-isolated runner is not configured")
            result = _run_isolated_shell(
                command=command,
                workspace=workspace,
                shell_executable=shell_executable,
                environment=environment,
                timeout_seconds=effective_timeout,
                isolation_runner=isolation_runner,
            )
    except (OSError, ValueError) as error:
        return _tool_error("shell tool could not start"), {
            "kind": "tool_call",
            "tool": "shell",
            "status": "infrastructure_error",
            "error_type": type(error).__name__,
            "command_sha256": hashlib.sha256(command_bytes).hexdigest(),
            "command_bytes": len(command_bytes),
        }
    after = snapshot_project(workspace)
    changed = sorted(
        path
        for path in set(before.files) | set(after.files)
        if before.files.get(path) != after.files.get(path)
    )
    stdout, bounded_stdout_truncated = _bounded_text(result.stdout)
    stderr, bounded_stderr_truncated = _bounded_text(result.stderr)
    stdout_truncated = result.stdout_truncated or bounded_stdout_truncated
    stderr_truncated = result.stderr_truncated or bounded_stderr_truncated
    stdout_bytes = result.stdout_bytes
    if stdout_bytes is None:
        stdout_bytes = len(result.stdout.encode("utf-8"))
    stderr_bytes = result.stderr_bytes
    if stderr_bytes is None:
        stderr_bytes = len(result.stderr.encode("utf-8"))
    stdout_sha256 = (
        result.stdout_sha256 or hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    )
    stderr_sha256 = (
        result.stderr_sha256 or hashlib.sha256(result.stderr.encode("utf-8")).hexdigest()
    )
    payload = {
        "status": "timeout" if result.timed_out else "completed",
        "return_code": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "changed_paths": changed,
        "execution_profile": execution_profile.value,
        "elapsed_seconds": result.elapsed_seconds,
    }
    audit = {
        "kind": "tool_call",
        "tool": "shell",
        "status": payload["status"],
        "return_code": result.returncode,
        "timed_out": result.timed_out,
        "elapsed_seconds": result.elapsed_seconds,
        "command_sha256": hashlib.sha256(command_bytes).hexdigest(),
        "command_bytes": len(command_bytes),
        "stdout_sha256": stdout_sha256,
        "stdout_bytes": stdout_bytes,
        "stdout_truncated": stdout_truncated,
        "stderr_sha256": stderr_sha256,
        "stderr_bytes": stderr_bytes,
        "stderr_truncated": stderr_truncated,
        "changed_paths": changed,
        "execution_profile": execution_profile.value,
    }
    return json.dumps(payload, sort_keys=True), audit


def _invoke_image_tool(
    *,
    arguments: dict[str, object],
    workspace: Path,
) -> tuple[str | list[dict[str, object]], dict[str, object]]:
    raw_path = arguments.get("path")
    detail = arguments.get("detail")
    if not isinstance(raw_path, str) or not raw_path:
        return _tool_error("image path must be non-empty"), {
            "kind": "tool_call",
            "tool": "view_image",
            "status": "invalid_arguments",
        }
    if detail not in {"low", "high", "auto"}:
        return _tool_error("image detail must be low, high, or auto"), {
            "kind": "tool_call",
            "tool": "view_image",
            "status": "invalid_arguments",
        }
    candidate = workspace / raw_path
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(workspace).as_posix()
    except (OSError, ValueError):
        return _tool_error("image must be an existing workspace-relative file"), {
            "kind": "tool_call",
            "tool": "view_image",
            "status": "rejected",
            "path": "$OUTSIDE_OR_MISSING",
        }
    if (
        candidate.is_symlink()
        or not resolved.is_file()
        or resolved.suffix.lower() not in _IMAGE_SUFFIXES
    ):
        return _tool_error("image must be a regular PNG, JPEG, or WebP file"), {
            "kind": "tool_call",
            "tool": "view_image",
            "status": "rejected",
            "path": relative,
        }
    size = resolved.stat().st_size
    if size > _MAXIMUM_IMAGE_BYTES:
        return _tool_error("image exceeds 8 MiB"), {
            "kind": "tool_call",
            "tool": "view_image",
            "status": "rejected",
            "path": relative,
            "bytes": size,
        }
    data = resolved.read_bytes()
    mime_type = mimetypes.guess_type(resolved.name)[0] or "image/png"
    digest = hashlib.sha256(data).hexdigest()
    output: list[dict[str, object]] = [
        {
            "type": "input_text",
            "text": json.dumps(
                {"status": "success", "path": relative, "bytes": size, "sha256": digest},
                sort_keys=True,
            ),
        },
        {
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}",
            "detail": detail,
        },
    ]
    return output, {
        "kind": "tool_call",
        "tool": "view_image",
        "status": "success",
        "path": relative,
        "bytes": size,
        "sha256": digest,
        "detail": detail,
    }


def _tool_error(message: str) -> str:
    return json.dumps({"status": "error", "error": message}, sort_keys=True)


def _workspace_instructions(profile: ExecutionProfile) -> str:
    boundary = (
        "The shell runs natively on the Host so installed game engines and tools work "
        "without an inner command sandbox. Keep task changes in the current disposable "
        "workspace unless the public request explicitly requires another location."
        if profile is ExecutionProfile.NATIVE_OPEN
        else "The shell is restricted to the disposable workspace and private scratch; "
        "network access is denied."
    )
    return (
        "Work directly in the complete current game-project workspace. Choose your own "
        "investigation, editing, engine, and validation strategy. Use the generic shell "
        f"and image tools as needed. Finish only when the public request is complete. {boundary}"
    )


def _run_native_shell(
    *,
    command: str,
    workspace: Path,
    shell_executable: Path,
    environment: dict[str, str],
    timeout_seconds: float,
) -> _ShellResult:
    return _run_shell_process(
        arguments=[str(shell_executable), "-c", command],
        workspace=workspace,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )


def _run_sandboxed_shell(
    *,
    command: str,
    workspace: Path,
    scratch: Path,
    sandbox_executable: Path,
    shell_executable: Path,
    profile: str,
    environment: dict[str, str],
    timeout_seconds: float,
) -> _ShellResult:
    return _run_shell_process(
        arguments=[
            str(sandbox_executable),
            "-p",
            profile,
            str(shell_executable),
            "-c",
            command,
        ],
        workspace=workspace,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )


def _run_isolated_shell(
    *,
    command: str,
    workspace: Path,
    shell_executable: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    isolation_runner: Path,
) -> _ShellResult:
    arguments = ExternalIsolationRunner(isolation_runner).command(
        operation="shell",
        workspace=workspace,
        timeout_seconds=timeout_seconds,
        command=[str(shell_executable), "-c", command],
    )
    return _run_shell_process(
        arguments=arguments,
        workspace=workspace,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )


def _run_shell_process(
    *,
    arguments: list[str],
    workspace: Path,
    environment: dict[str, str],
    timeout_seconds: float,
) -> _ShellResult:
    started = time.monotonic()
    process = subprocess.Popen(
        arguments,
        cwd=workspace,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=os.name == "posix",
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture = _BoundedStreamCapture()
    stderr_capture = _BoundedStreamCapture()
    stdout_thread = threading.Thread(
        target=stdout_capture.consume,
        args=(process.stdout,),
        name="gameforge-stdout-capture",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=stderr_capture.consume,
        args=(process.stderr,),
        name="gameforge-stderr-capture",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, signal.SIGKILL)
            process.wait()
    finally:
        # The shell can exit after spawning background descendants. They retain the
        # process group and pipe descriptors, so always clean the full group.
        _terminate_process_group(process, signal.SIGTERM)
    stdout_thread.join(timeout=_TERMINATION_GRACE_SECONDS)
    stderr_thread.join(timeout=_TERMINATION_GRACE_SECONDS)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _terminate_process_group(process, signal.SIGKILL)
        stdout_thread.join(timeout=_TERMINATION_GRACE_SECONDS)
        stderr_thread.join(timeout=_TERMINATION_GRACE_SECONDS)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        process.stdout.close()
        process.stderr.close()
        stdout_thread.join(timeout=_TERMINATION_GRACE_SECONDS)
        stderr_thread.join(timeout=_TERMINATION_GRACE_SECONDS)
    return _ShellResult(
        process.returncode if process.returncode is not None else 124,
        stdout_capture.text(),
        stderr_capture.text(),
        round(time.monotonic() - started, 3),
        timed_out=timed_out,
        stdout_bytes=stdout_capture.byte_count,
        stderr_bytes=stderr_capture.byte_count,
        stdout_sha256=stdout_capture.hexdigest(),
        stderr_sha256=stderr_capture.hexdigest(),
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )


def _terminate_process_group(
    process: subprocess.Popen[str],
    signal_number: signal.Signals,
) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal_number)
        elif process.poll() is None and signal_number is signal.SIGTERM:
            process.terminate()
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        return


def _tool_environment(
    *,
    scratch: Path,
    environment_overrides: Mapping[str, str] | None,
    execution_profile: ExecutionProfile = ExecutionProfile.SUPERVISED_NATIVE,
) -> dict[str, str]:
    if execution_profile is ExecutionProfile.NATIVE_OPEN:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name.upper() not in {"GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY"}
            and not name.upper().endswith(("_API_KEY", "_ACCESS_TOKEN"))
        }
        environment.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        environment.setdefault("LANG", "C.UTF-8")
        environment.setdefault("LC_ALL", "C.UTF-8")
    else:
        environment = {
            "HOME": str(scratch),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "TMPDIR": str(scratch),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    if environment_overrides:
        for name, value in environment_overrides.items():
            upper = name.upper()
            if upper in {
                "PATH",
                "GODOT",
                "UNITY",
                "UNITY_EDITOR",
                "DISPLAY",
                "ZDOTDIR",
                "BASH_ENV",
                "ENV",
                "GIT_CEILING_DIRECTORIES",
            } or upper.startswith("GAMEFORGE_"):
                environment[name] = value
    return environment


def _sandbox_profile(
    *,
    workspace: Path,
    scratch: Path,
    shell_executable: Path,
    trusted_read_roots: tuple[Path, ...],
    environment: Mapping[str, str],
    host_runtime_roots: tuple[Path, ...] = (),
    host_runtime_files: tuple[Path, ...] = (),
) -> str:
    readable_roots = {
        workspace,
        scratch,
        shell_executable.parent,
        Path("/Applications"),
        Path("/Library"),
        Path("/System"),
        Path("/bin"),
        Path("/opt/homebrew"),
        Path("/private/var/db/timezone"),
        Path("/private/var/select"),
        Path("/sbin"),
        Path("/usr"),
        *trusted_read_roots,
        *host_runtime_roots,
    }
    path_value = environment.get("PATH", "")
    readable_roots.update(
        path.resolve(strict=True)
        for raw in path_value.split(os.pathsep)
        if raw and (path := Path(raw)).is_dir()
    )
    read_subpaths = "\n".join(
        f'    (subpath "{_scheme_path(path)}")' for path in sorted(readable_roots)
    )
    ancestors = {
        ancestor
        for path in (*readable_roots, *host_runtime_files)
        for ancestor in path.resolve(strict=False).parents
    }
    read_ancestors = "\n".join(
        f'    (literal "{_scheme_path(path)}")' for path in sorted(ancestors)
    )
    runtime_write_subpaths = "\n".join(
        f'    (subpath "{_scheme_path(path)}")' for path in sorted(host_runtime_roots)
    )
    runtime_read_files = "\n".join(
        f'    (literal "{_scheme_path(path)}")' for path in sorted(host_runtime_files)
    )
    runtime_write_files = "\n".join(
        f'    (literal "{_scheme_path(path)}")' for path in sorted(host_runtime_files)
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process*)\n"
        "(allow signal (target self))\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        "(allow ipc-posix*)\n"
        "(allow file-read*\n"
        f"{read_subpaths}\n"
        f"{read_ancestors}\n"
        f"{runtime_read_files}\n"
        '    (literal "/dev/null")\n'
        '    (literal "/dev/urandom")\n'
        '    (literal "/var")\n'
        '    (subpath "/var/select"))\n'
        "(allow file-write*\n"
        f'    (subpath "{_scheme_path(workspace)}")\n'
        f'    (subpath "{_scheme_path(scratch)}")\n'
        f"{runtime_write_subpaths}\n"
        f"{runtime_write_files}\n"
        '    (literal "/dev/null"))\n'
    )


def _scheme_path(path: Path) -> str:
    return str(path.resolve(strict=False)).replace("\\", "\\\\").replace('"', '\\"')


def _bounded_text(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAXIMUM_TOOL_OUTPUT_BYTES:
        return value, False
    half = _MAXIMUM_TOOL_OUTPUT_BYTES // 2
    head = encoded[:half].decode("utf-8", errors="replace")
    tail = encoded[-half:].decode("utf-8", errors="replace")
    return f"{head}\n... output truncated ...\n{tail}", True
