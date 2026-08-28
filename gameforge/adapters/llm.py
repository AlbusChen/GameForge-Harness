from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

from gameforge.harness.isolation_runner import ExternalIsolationRunner


class ModelError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    text: str
    usage: ModelUsage
    provider: str = "mock"
    model: str = "mock"
    request_id: str | None = None
    latency_seconds: float = 0.0
    transport_attempts: int = 1
    tool_audit: tuple[dict[str, object], ...] = ()

    def trace_record(self, *, system: str, prompt: str) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "latency_seconds": self.latency_seconds,
            "transport_attempts": self.transport_attempts,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "reasoning_output_tokens": self.usage.reasoning_output_tokens,
            "cost_usd": self.usage.cost_usd,
            "system_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "response_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
        }


class LanguageModel(Protocol):
    def complete(self, *, system: str, prompt: str) -> ModelResponse: ...


class MockLanguageModel:
    """Offline adapter used to prove orchestration before any paid request."""

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system, prompt
        return ModelResponse(
            text=json.dumps(
                {
                    "mode": "mock",
                    "decision": "use_deterministic_plan",
                    "rationale": "Paid execution is disabled; use the verified fixed plan.",
                    "evidence_sources": [],
                },
                sort_keys=True,
            ),
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_usd=0.0),
        )


@dataclass
class ScriptedControlLanguageModel:
    """Replay an explicit decision sequence for offline harness control experiments."""

    decisions: Sequence[dict[str, object]]
    name: str = "scripted-control"
    index: int = 0

    @classmethod
    def from_json(cls, path: Path) -> ScriptedControlLanguageModel:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read decision script: {path}") from error
        except json.JSONDecodeError as error:
            raise ValueError(f"decision script is invalid JSON: {error}") from error
        if (
            not isinstance(payload, list)
            or not payload
            or not all(isinstance(item, dict) for item in payload)
        ):
            raise ValueError("decision script must be a non-empty JSON array of objects")
        return cls(decisions=tuple(payload), name=path.stem)

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system, prompt
        if self.index >= len(self.decisions):
            raise ModelError("script_exhausted", "scripted control ran out of decisions")
        decision = self.decisions[self.index]
        self.index += 1
        text = json.dumps(decision, sort_keys=True)
        return ModelResponse(
            text=text,
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_usd=0.0),
            provider="scripted-control",
            model=self.name,
            request_id=f"decision-{self.index:03d}",
        )


@dataclass(frozen=True)
class AgentCliLanguageModel:
    """Use an authenticated local agent executable as a JSON-only reasoning backend."""

    executable: Path
    model: str
    timeout_seconds: float = 600.0
    input_usd_per_million: float = 0.0
    cached_input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    reasoning_effort: str | None = None
    image_paths: tuple[Path, ...] = ()
    maximum_startup_retries: int = 1
    workspace_sandbox: str = "workspace-write"
    isolation_runner: Path | None = None

    def __post_init__(self) -> None:
        if not self.executable.is_file():
            raise ValueError(f"agent CLI executable does not exist: {self.executable}")
        if not self.model.strip():
            raise ValueError("agent CLI model must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("agent CLI timeout must be positive")
        if self.reasoning_effort not in {None, "none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("agent CLI reasoning effort is invalid")
        if not 0 <= self.maximum_startup_retries <= 3:
            raise ValueError("agent CLI startup retries must be between zero and three")
        if self.workspace_sandbox not in {"workspace-write", "danger-full-access"}:
            raise ValueError("workspace agent sandbox is invalid")
        if self.isolation_runner is not None:
            runner = self.isolation_runner.resolve(strict=True)
            if not runner.is_file() or not os.access(runner, os.X_OK):
                raise ValueError(f"isolation runner is not executable: {runner}")
            object.__setattr__(self, "isolation_runner", runner)
        _validate_agent_image_paths(self.image_paths)

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        return self._complete(system=system, prompt=prompt, image_paths=self.image_paths)

    def complete_with_images(
        self,
        *,
        system: str,
        prompt: str,
        image_paths: tuple[Path, ...],
    ) -> ModelResponse:
        """Attach bounded run-time evidence while retaining as much initial context as fits."""
        _validate_agent_image_paths(image_paths)
        dynamic = tuple(dict.fromkeys(image_paths))[-6:]
        retained_initial = tuple(path for path in self.image_paths if path not in dynamic)[
            : max(0, 6 - len(dynamic))
        ]
        return self._complete(
            system=system,
            prompt=prompt,
            image_paths=(*retained_initial, *dynamic),
        )

    def run_workspace_agent(
        self,
        *,
        project: Path,
        prompt: str,
        timeout_seconds: float,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> ModelResponse:
        """Run one open-ended native tool session inside a staged project workspace."""
        workspace = project.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace agent project must be a directory")
        if not prompt.strip():
            raise ValueError("workspace agent prompt must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("workspace agent timeout must be positive")
        _validate_agent_image_paths(self.image_paths)
        command = [
            str(self.executable),
            "-a",
            "never",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "-m",
            self.model,
            "-s",
            self.workspace_sandbox,
            "-C",
            str(workspace),
        ]
        if self.reasoning_effort is not None:
            command.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
        for image_path in self.image_paths:
            command.extend(["--image", str(image_path.resolve(strict=True))])
        command.extend(["--", prompt])

        if self.isolation_runner is not None:
            command = ExternalIsolationRunner(self.isolation_runner).command(
                operation="workspace-agent",
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                command=command,
            )

        environment = _environment_without_unrelated_credentials()
        environment.setdefault("GIT_CEILING_DIRECTORIES", str(workspace.parent))
        if environment_overrides is not None:
            environment.update(environment_overrides)
        started = time.monotonic()
        completed: subprocess.CompletedProcess[str] | None = None
        transport_attempts = 0
        for attempt in range(self.maximum_startup_retries + 1):
            transport_attempts += 1
            completed = _run_agent_cli_process(
                command,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
            if completed.returncode == 0:
                break
            if attempt >= self.maximum_startup_retries or not _zero_turn_cli_failure(completed):
                break
        assert completed is not None
        if completed.returncode != 0:
            raise ModelError(
                "command_failed",
                f"workspace agent CLI returned exit code {completed.returncode}",
            )
        text, usage, request_id = _parse_agent_cli_jsonl(completed.stdout)
        tool_audit = _parse_agent_cli_tool_audit(completed.stdout, workspace=workspace)
        uncached_input = max(0, usage.input_tokens - usage.cached_input_tokens)
        cost = (
            uncached_input * self.input_usd_per_million
            + usage.cached_input_tokens * self.cached_input_usd_per_million
            + usage.output_tokens * self.output_usd_per_million
        ) / 1_000_000
        return ModelResponse(
            text=text,
            usage=ModelUsage(
                usage.input_tokens,
                usage.output_tokens,
                round(cost, 8),
                usage.cached_input_tokens,
                usage.reasoning_output_tokens,
            ),
            provider="agent-cli-workspace",
            model=self.model,
            request_id=request_id,
            latency_seconds=round(time.monotonic() - started, 3),
            transport_attempts=transport_attempts,
            tool_audit=tool_audit,
        )

    def _complete(
        self,
        *,
        system: str,
        prompt: str,
        image_paths: tuple[Path, ...],
    ) -> ModelResponse:
        combined_prompt = (
            f"SYSTEM CONTRACT:\n{system}\n\n"
            "USER PAYLOAD:\n"
            f"{prompt}\n\n"
            "Return only the strict JSON decision object requested by the system contract. "
            "Do not act on files or invoke shell/native tools from the outer model runner. "
            "You are explicitly allowed to request any typed Harness tool listed in "
            "USER PAYLOAD by returning its JSON tool decision; the Harness executes that "
            "request inside its approved scope."
        )
        started = time.monotonic()
        completed: subprocess.CompletedProcess[str] | None = None
        transport_attempts = 0
        for attempt in range(self.maximum_startup_retries + 1):
            transport_attempts += 1
            with tempfile.TemporaryDirectory(prefix="gameforge-agent-cli-") as directory:
                command = [
                    str(self.executable),
                    "exec",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--json",
                    "-m",
                    self.model,
                    "-s",
                    "read-only",
                    "-C",
                    directory,
                ]
                if self.reasoning_effort is not None:
                    command.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
                for image_path in image_paths:
                    command.extend(["--image", str(image_path.resolve(strict=True))])
                command.extend(["--", combined_prompt])
                completed = _run_agent_cli_process(
                    command,
                    timeout_seconds=self.timeout_seconds,
                    environment=_environment_without_unrelated_credentials(),
                )
            if completed.returncode == 0:
                break
            if attempt >= self.maximum_startup_retries or not _zero_turn_cli_failure(completed):
                break
        assert completed is not None
        if completed.returncode != 0:
            raise ModelError(
                "command_failed",
                f"agent CLI returned exit code {completed.returncode}",
            )
        text, usage, request_id = _parse_agent_cli_jsonl(completed.stdout)
        uncached_input = max(0, usage.input_tokens - usage.cached_input_tokens)
        cost = (
            uncached_input * self.input_usd_per_million
            + usage.cached_input_tokens * self.cached_input_usd_per_million
            + usage.output_tokens * self.output_usd_per_million
        ) / 1_000_000
        return ModelResponse(
            text=text,
            usage=ModelUsage(
                usage.input_tokens,
                usage.output_tokens,
                round(cost, 8),
                usage.cached_input_tokens,
                usage.reasoning_output_tokens,
            ),
            provider="agent-cli",
            model=self.model,
            request_id=request_id,
            latency_seconds=round(time.monotonic() - started, 3),
            transport_attempts=transport_attempts,
        )


def _validate_agent_image_paths(image_paths: tuple[Path, ...]) -> None:
    if len(image_paths) > 6:
        raise ValueError("agent CLI accepts at most six bounded image attachments")
    for path in image_paths:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in {".jpeg", ".jpg", ".png", ".webp"}
        ):
            raise ValueError(f"agent CLI image attachment is invalid: {path}")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError(f"agent CLI image attachment exceeds 8 MiB: {path}")


def _run_agent_cli_process(
    command: list[str],
    *,
    timeout_seconds: float,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run one isolated CLI request and terminate its whole process group on timeout."""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        raise ModelError("command_failed", "agent CLI could not be started") from error
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _kill_process_group(process)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
        raise ModelError("timeout", "agent CLI request timed out") from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        process.kill()


def _zero_turn_cli_failure(completed: subprocess.CompletedProcess[str]) -> bool:
    """Retry only an exit-1 request that produced no model turn or final answer."""
    if completed.returncode != 1:
        return False
    for raw_line in completed.stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            return False
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            return False
    return True


def _parse_agent_cli_jsonl(payload: str) -> tuple[str, ModelUsage, str | None]:
    message: str | None = None
    request_id: str | None = None
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    reasoning_output_tokens = 0
    for raw_line in payload.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            request_id = event["thread_id"]
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            message = item["text"]
        usage = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(usage, dict):
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            cached_input_tokens = int(usage.get("cached_input_tokens", 0))
            reasoning_output_tokens = int(usage.get("reasoning_output_tokens", 0))
    if message is None:
        raise ModelError("invalid_response", "agent CLI produced no final message")
    return (
        message,
        ModelUsage(
            input_tokens,
            output_tokens,
            0.0,
            cached_input_tokens,
            reasoning_output_tokens,
        ),
        request_id,
    )


def _parse_agent_cli_tool_audit(
    payload: str,
    *,
    workspace: Path,
) -> tuple[dict[str, object], ...]:
    """Retain a bounded, content-free audit of native tool activity."""
    records: list[dict[str, object]] = []
    for raw_line in payload.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "command_execution":
            command = item.get("command")
            records.append(
                {
                    "kind": "command_execution",
                    "command_sha256": (
                        hashlib.sha256(command.encode("utf-8")).hexdigest()
                        if isinstance(command, str)
                        else None
                    ),
                    "references_project": (
                        str(workspace) in command if isinstance(command, str) else None
                    ),
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status"),
                }
            )
        elif item_type == "file_change":
            changes: list[dict[str, object]] = []
            raw_changes = item.get("changes")
            if isinstance(raw_changes, list):
                for change in raw_changes:
                    if not isinstance(change, dict):
                        continue
                    raw_path = change.get("path")
                    public_path = "$UNKNOWN"
                    within_project = False
                    if isinstance(raw_path, str):
                        candidate = Path(raw_path)
                        if not candidate.is_absolute():
                            candidate = workspace / candidate
                        try:
                            public_path = (
                                candidate.resolve(strict=False).relative_to(workspace).as_posix()
                            )
                            within_project = True
                        except ValueError:
                            public_path = "$OUTSIDE_PROJECT"
                    changes.append(
                        {
                            "path": public_path,
                            "within_project": within_project,
                            "change_kind": change.get("kind"),
                        }
                    )
            records.append(
                {
                    "kind": "file_change",
                    "changes": changes,
                    "status": item.get("status"),
                }
            )
        if len(records) >= 2_000:
            break
    return tuple(records)


def _environment_without_unrelated_credentials() -> dict[str, str]:
    def keep(name: str) -> bool:
        upper = name.upper()
        if upper in {"GH_TOKEN", "GITHUB_TOKEN"}:
            return False
        return not upper.endswith(("_API_KEY", "_ACCESS_TOKEN"))

    return {name: value for name, value in os.environ.items() if keep(name)}


@dataclass
class CostLedger:
    maximum_usd: float
    responses: list[ModelResponse] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(response.usage.cost_usd for response in self.responses)

    @property
    def input_tokens(self) -> int:
        return sum(response.usage.input_tokens for response in self.responses)

    @property
    def output_tokens(self) -> int:
        return sum(response.usage.output_tokens for response in self.responses)

    @property
    def cached_input_tokens(self) -> int:
        return sum(response.usage.cached_input_tokens for response in self.responses)

    @property
    def reasoning_output_tokens(self) -> int:
        return sum(response.usage.reasoning_output_tokens for response in self.responses)

    def record(self, response: ModelResponse) -> None:
        projected = self.total_usd + response.usage.cost_usd
        if projected > self.maximum_usd:
            raise ModelError(
                "cost_budget_exceeded",
                f"model cost budget would be exceeded: {projected:.6f} USD",
            )
        self.responses.append(response)

    def record_usage(
        self,
        usage: ModelUsage,
        *,
        provider: str = "auxiliary",
        model: str = "auxiliary",
    ) -> None:
        """Account for a bounded model call performed inside a typed tool."""
        self.record(
            ModelResponse(
                text="",
                usage=usage,
                provider=provider,
                model=model,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "currency": "USD",
            "maximum": self.maximum_usd,
            "total": round(self.total_usd, 8),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "requests": len(self.responses),
        }


@dataclass(frozen=True)
class OpenAICompatibleLanguageModel:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    provider: str = "openai-compatible"
    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    timeout_seconds: float = 60.0
    max_output_tokens: int = 1200
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("LLM_BASE_URL must be an absolute HTTPS URL")
        if not self.model.strip():
            raise ValueError("LLM_MODEL must not be empty")
        if not self.api_key:
            raise ValueError("LLM_API_KEY must not be empty")
        if self.input_usd_per_million < 0 or self.output_usd_per_million < 0:
            raise ValueError("model token prices cannot be negative")

    @classmethod
    def from_project(cls, root: Path) -> OpenAICompatibleLanguageModel:
        values = _model_environment(root)
        required = ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL")
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise ValueError(f"missing local model configuration: {', '.join(missing)}")
        return cls(
            api_key=values["LLM_API_KEY"],
            model=values["LLM_MODEL"],
            base_url=values["LLM_BASE_URL"],
            input_usd_per_million=float(values.get("LLM_INPUT_USD_PER_MILLION", "0")),
            output_usd_per_million=float(values.get("LLM_OUTPUT_USD_PER_MILLION", "0")),
        )

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        endpoint = urljoin(self.base_url.rstrip("/") + "/", "chat/completions")
        request_payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        if self.reasoning_effort:
            request_payload["reasoning_effort"] = self.reasoning_effort
        payload = json.dumps(request_payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "gameforge-harness/0.1",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
                request_id = response.headers.get("x-request-id")
        except urllib.error.HTTPError as error:
            raise ModelError("http_error", f"model endpoint returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ModelError("network_error", "model endpoint could not be reached") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelError("invalid_response", "model endpoint returned invalid JSON") from error

        try:
            text = response_payload["choices"][0]["message"]["content"]
            usage_payload = response_payload.get("usage", {})
            input_tokens = int(usage_payload.get("prompt_tokens", 0))
            output_tokens = int(usage_payload.get("completion_tokens", 0))
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ModelError(
                "invalid_response",
                "model response is missing required fields",
            ) from error
        if not isinstance(text, str):
            raise ModelError("invalid_response", "model response content must be text")
        returned_model = response_payload.get("model", self.model)
        if not isinstance(returned_model, str) or not returned_model:
            returned_model = self.model

        cost = (
            input_tokens * self.input_usd_per_million + output_tokens * self.output_usd_per_million
        ) / 1_000_000
        return ModelResponse(
            text=text,
            usage=ModelUsage(input_tokens, output_tokens, round(cost, 8)),
            provider=self.provider,
            model=returned_model,
            request_id=request_id,
            latency_seconds=round(time.monotonic() - started, 3),
        )


_MODEL_ENV_NAMES = {
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_INPUT_USD_PER_MILLION",
    "LLM_OUTPUT_USD_PER_MILLION",
}


def load_project_environment(root: Path, allowed_names: set[str]) -> dict[str, str]:
    invalid_names = [name for name in allowed_names if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)]
    if invalid_names:
        raise ValueError(f"invalid environment variable names: {sorted(invalid_names)}")
    values: dict[str, str] = {}
    local_env = root / ".env.local"
    if local_env.is_file():
        mode = stat.S_IMODE(local_env.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(".env.local must use permissions 0600")
        for line in local_env.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, separator, value = stripped.partition("=")
            name = name.strip()
            if separator and name in allowed_names:
                values[name] = value.strip()

    for name in allowed_names:
        if os.environ.get(name):
            values[name] = os.environ[name]
    return values


def _model_environment(root: Path) -> dict[str, str]:
    return load_project_environment(root, _MODEL_ENV_NAMES)


def sanitized_model_response(response: ModelResponse) -> dict[str, object]:
    payload = asdict(response)
    payload["usage"] = asdict(response.usage)
    return payload
