from __future__ import annotations

import hashlib
import json
import os
import re
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gameforge.adapters.direct_api_workspace import DirectApiWorkspaceLanguageModel
from gameforge.adapters.llm import (
    AgentCliLanguageModel,
    LanguageModel,
    MockLanguageModel,
    OpenAICompatibleLanguageModel,
    load_project_environment,
)
from gameforge.harness.contracts import ModelRunSpec
from gameforge.harness.execution_profiles import ExecutionProfile, codex_sandbox_for


class ModelProvider(StrEnum):
    MOCK = "mock"
    AGENT_CLI = "agent-cli"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"
    OPENAI_RESPONSES_WORKSPACE = "openai-responses-workspace"
    CODEX_SUBSCRIPTION = "codex-subscription"
    OPENAI_RESPONSES_API = "openai-responses-api"


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider
    model: str = Field(min_length=1)
    base_url: str | None = None
    executable: Path | None = None
    isolation_runner: Path | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    reasoning_effort: str | None = None
    input_usd_per_million: float = Field(default=0.0, ge=0)
    cached_input_usd_per_million: float = Field(default=0.0, ge=0)
    output_usd_per_million: float = Field(default=0.0, ge=0)
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_output_tokens: int = Field(default=1200, ge=1, le=100_000)
    execution_profile: ExecutionProfile = ExecutionProfile.NATIVE_OPEN

    @model_validator(mode="after")
    def apply_provider_defaults(self) -> ModelProfile:
        if self.provider is ModelProvider.MOCK:
            if self.api_key_env or self.base_url or self.executable or self.isolation_runner:
                raise ValueError("mock profiles cannot configure credentials, URLs, or executables")
            return self

        if self.execution_profile is ExecutionProfile.STRONG_ISOLATED:
            if self.isolation_runner is None:
                raise ValueError("strong-isolated requires a user-supplied isolation_runner")
            if self.provider not in {
                ModelProvider.AGENT_CLI,
                ModelProvider.CODEX_SUBSCRIPTION,
                ModelProvider.OPENAI_RESPONSES_WORKSPACE,
                ModelProvider.OPENAI_RESPONSES_API,
            }:
                raise ValueError("strong-isolated is supported only by workspace solver backends")
        elif self.isolation_runner is not None:
            raise ValueError(
                "isolation_runner is only valid with execution_profile=strong-isolated"
            )

        if self.provider in {
            ModelProvider.AGENT_CLI,
            ModelProvider.CODEX_SUBSCRIPTION,
        }:
            if self.api_key_env or self.base_url or self.executable is None:
                raise ValueError(
                    "agent-cli profiles require executable and cannot configure API credentials"
                )
            return self

        if self.executable is not None:
            raise ValueError("API profiles cannot configure an agent executable")
        if self.provider in {
            ModelProvider.OPENAI,
            ModelProvider.OPENAI_RESPONSES_WORKSPACE,
            ModelProvider.OPENAI_RESPONSES_API,
        }:
            self.base_url = self.base_url or "https://api.openai.com/v1"
            self.api_key_env = self.api_key_env or "OPENAI_API_KEY"
        else:
            if not self.base_url:
                raise ValueError("openai-compatible profiles require base_url")
            self.api_key_env = self.api_key_env or "LLM_API_KEY"

        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("model base_url must be an absolute HTTPS URL")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> ModelProfile:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read model profile: {path}") from error
        except yaml.YAMLError as error:
            raise ValueError(f"invalid model profile YAML: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("model profile root must be a mapping")
        return cls.model_validate(payload)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_run_spec(self) -> ModelRunSpec:
        return ModelRunSpec(
            provider=self.provider.value,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            parameters={
                "temperature": 0,
                "response_format": "json_object",
                "max_output_tokens": self.max_output_tokens,
            },
            profile_sha256=self.fingerprint(),
        )

    def credential_is_configured(self, root: Path) -> bool:
        if self.provider is ModelProvider.MOCK:
            return True
        if self.provider in {
            ModelProvider.AGENT_CLI,
            ModelProvider.CODEX_SUBSCRIPTION,
        }:
            assert self.executable is not None
            executable = (
                self.executable.resolve()
                if self.executable.is_absolute()
                else (root / self.executable).resolve()
            )
            return executable.is_file()
        assert self.api_key_env is not None
        values = load_project_environment(root, {self.api_key_env})
        return bool(values.get(self.api_key_env))

    def isolation_runner_is_configured(self, root: Path) -> bool:
        if self.execution_profile is not ExecutionProfile.STRONG_ISOLATED:
            return True
        try:
            runner = self._resolved_isolation_runner(root).resolve(strict=True)
        except (OSError, ValueError):
            return False
        return runner.is_file() and os.access(runner, os.X_OK)

    def build_adapter(
        self,
        root: Path,
        *,
        trusted_read_roots: tuple[Path, ...] = (),
        host_runtime_roots: tuple[Path, ...] = (),
        host_runtime_files: tuple[Path, ...] = (),
    ) -> LanguageModel | DirectApiWorkspaceLanguageModel:
        if self.provider is ModelProvider.MOCK:
            return MockLanguageModel()
        if self.provider in {
            ModelProvider.AGENT_CLI,
            ModelProvider.CODEX_SUBSCRIPTION,
        }:
            assert self.executable is not None
            executable = (
                self.executable.resolve()
                if self.executable.is_absolute()
                else (root / self.executable).resolve()
            )
            return AgentCliLanguageModel(
                executable=executable,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
                input_usd_per_million=self.input_usd_per_million,
                cached_input_usd_per_million=self.cached_input_usd_per_million,
                output_usd_per_million=self.output_usd_per_million,
                reasoning_effort=self.reasoning_effort,
                workspace_sandbox=(
                    "danger-full-access"
                    if self.execution_profile is ExecutionProfile.STRONG_ISOLATED
                    else codex_sandbox_for(self.execution_profile)
                ),
                isolation_runner=(
                    self._resolved_isolation_runner(root)
                    if self.execution_profile is ExecutionProfile.STRONG_ISOLATED
                    else None
                ),
            )

        assert self.api_key_env is not None
        assert self.base_url is not None
        values = load_project_environment(root, {self.api_key_env})
        api_key = values.get(self.api_key_env, "")
        if not api_key:
            raise ValueError(f"missing model credential environment variable: {self.api_key_env}")
        if self.provider in {
            ModelProvider.OPENAI_RESPONSES_WORKSPACE,
            ModelProvider.OPENAI_RESPONSES_API,
        }:
            return DirectApiWorkspaceLanguageModel(
                base_url=self.base_url,
                model=self.model,
                api_key=api_key,
                provider=self.provider.value,
                request_timeout_seconds=self.timeout_seconds,
                max_output_tokens=self.max_output_tokens,
                reasoning_effort=self.reasoning_effort,
                input_usd_per_million=self.input_usd_per_million,
                cached_input_usd_per_million=self.cached_input_usd_per_million,
                output_usd_per_million=self.output_usd_per_million,
                trusted_read_roots=trusted_read_roots,
                host_runtime_roots=host_runtime_roots,
                host_runtime_files=host_runtime_files,
                execution_profile=self.execution_profile,
                isolation_runner=(
                    self._resolved_isolation_runner(root)
                    if self.execution_profile is ExecutionProfile.STRONG_ISOLATED
                    else None
                ),
            )
        return OpenAICompatibleLanguageModel(
            base_url=self.base_url,
            model=self.model,
            api_key=api_key,
            provider=self.provider.value,
            input_usd_per_million=self.input_usd_per_million,
            output_usd_per_million=self.output_usd_per_million,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
        )

    def _resolved_isolation_runner(self, root: Path) -> Path:
        if self.isolation_runner is None:
            raise ValueError("strong-isolated requires isolation_runner")
        return (
            self.isolation_runner.resolve()
            if self.isolation_runner.is_absolute()
            else (root / self.isolation_runner).resolve()
        )


def validate_environment_name(name: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise ValueError("credential environment name must be uppercase and shell-safe")
    return name


def environment_has_key(name: str) -> bool:
    return bool(os.environ.get(validate_environment_name(name)))
