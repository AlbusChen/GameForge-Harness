from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.adapters.llm import (
    AgentCliLanguageModel,
    MockLanguageModel,
    OpenAICompatibleLanguageModel,
)
from gameforge.harness.model_profiles import ModelProfile


def test_mock_profile_builds_without_credentials(tmp_path: Path) -> None:
    profile = ModelProfile(provider="mock", model="deterministic-mock")

    assert profile.credential_is_configured(tmp_path)
    assert isinstance(profile.build_adapter(tmp_path), MockLanguageModel)
    assert profile.to_run_spec().provider == "mock"
    assert profile.to_run_spec().parameters == {
        "temperature": 0,
        "response_format": "json_object",
        "max_output_tokens": 1200,
    }


def test_openai_profile_uses_key_name_not_key_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "local-only-secret")
    profile = ModelProfile(provider="openai", model="example-model")

    adapter = profile.build_adapter(tmp_path)

    assert isinstance(adapter, OpenAICompatibleLanguageModel)
    assert adapter.base_url == "https://api.openai.com/v1"
    assert adapter.provider == "openai"
    assert "local-only-secret" not in profile.model_dump_json()
    assert "local-only-secret" not in repr(adapter)


def test_profile_rejects_embedded_secret() -> None:
    with pytest.raises(ValidationError, match="api_key"):
        ModelProfile.model_validate(
            {
                "provider": "openai",
                "model": "example-model",
                "api_key": "do-not-store-this",
            }
        )


def test_project_env_file_requires_private_permissions(tmp_path: Path) -> None:
    profile = ModelProfile(provider="openai", model="example-model")
    local_env = tmp_path / ".env.local"
    local_env.write_text("OPENAI_API_KEY=local-only-secret\n", encoding="utf-8")
    local_env.chmod(0o644)

    with pytest.raises(PermissionError, match="0600"):
        profile.build_adapter(tmp_path)


def test_agent_cli_profile_builds_without_an_api_key(tmp_path: Path) -> None:
    executable = tmp_path / "agent"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    profile = ModelProfile(
        provider="agent-cli",
        model="gpt-5.6-terra",
        executable=Path("agent"),
        reasoning_effort="medium",
    )

    adapter = profile.build_adapter(tmp_path)

    assert profile.credential_is_configured(tmp_path)
    assert isinstance(adapter, AgentCliLanguageModel)
    assert adapter.executable == executable
    assert adapter.reasoning_effort == "medium"


def test_compatible_api_profile_rejects_agent_executable() -> None:
    with pytest.raises(ValidationError, match="executable"):
        ModelProfile(
            provider="openai-compatible",
            model="example-model",
            base_url="https://model.example/v1",
            executable=Path("agent"),
        )
