import subprocess
from pathlib import Path

import pytest

from gameforge.adapters.unity import (
    UnityBatchGateway,
    UnityToolError,
    UnityToolErrorCode,
    _environment_without_credentials,
    _validate_runtime_snapshot,
)


def test_gateway_rejects_missing_editor(tmp_path: Path) -> None:
    gateway = UnityBatchGateway(
        editor=tmp_path / "missing-unity",
        project=tmp_path / "project",
        run_directory=tmp_path / "run",
    )

    with pytest.raises(UnityToolError) as error:
        gateway.health_check()

    assert error.value.code is UnityToolErrorCode.EDITOR_NOT_FOUND


def test_gateway_rejects_arbitrary_test_platform(tmp_path: Path) -> None:
    gateway = UnityBatchGateway(tmp_path / "unity", tmp_path / "project", tmp_path / "run")

    with pytest.raises(ValueError, match="unsupported"):
        gateway.run_tests("AnyCommand")


def test_smoke_test_requires_a_built_application(tmp_path: Path) -> None:
    gateway = UnityBatchGateway(tmp_path / "unity", tmp_path / "project", tmp_path / "run")

    with pytest.raises(UnityToolError) as error:
        gateway.launch_build_smoke_test()

    assert error.value.code is UnityToolErrorCode.BUILD_NOT_FOUND


def test_smoke_timeout_preserves_last_probe_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "run" / "build" / "ArenaDemo.app" / "Contents" / "MacOS" / "Arena"
    executable.parent.mkdir(parents=True)
    executable.write_text("player", encoding="utf-8")
    gateway = UnityBatchGateway(tmp_path / "unity", tmp_path / "project", tmp_path / "run")

    def timeout(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        checkpoint = Path(command[command.index("-gameforgeSmokeCheckpoint") + 1])
        log = Path(command[command.index("-logFile") + 1])
        checkpoint.write_text('{"stage":"state_written"}\n', encoding="utf-8")
        log.write_text("probe log\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(UnityToolError, match="last smoke stage=state_written") as error:
        gateway.launch_build_smoke_test(timeout_seconds=1)

    assert error.value.code is UnityToolErrorCode.TIMEOUT
    assert (tmp_path / "run" / "build-smoke.checkpoint.json").is_file()


def test_gateway_does_not_forward_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret-model-key")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai-key")
    monkeypatch.setenv("VENDOR_API_KEY", "secret-vendor-key")
    monkeypatch.setenv("VENDOR_ACCESS_TOKEN", "secret-vendor-token")
    monkeypatch.setenv("GH_TOKEN", "secret-git-key")
    monkeypatch.setenv("SAFE_TEST_VALUE", "visible")

    environment = _environment_without_credentials()

    assert "LLM_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "VENDOR_API_KEY" not in environment
    assert "VENDOR_ACCESS_TOKEN" not in environment
    assert "GH_TOKEN" not in environment
    assert environment["SAFE_TEST_VALUE"] == "visible"


def test_runtime_snapshot_requires_structured_playing_state(tmp_path: Path) -> None:
    snapshot = tmp_path / "state.json"
    snapshot.write_text(
        """{
          "frame": 42,
          "gameStatus": "playing",
          "player": {"health": 100, "maxHealth": 100, "position": [0, 1, 0]},
          "enemies": [{"id": "enemy-001"}],
          "enemiesAlive": 1,
          "currentWave": 1,
          "ui": {"endScreenVisible": false},
          "errors": []
        }""",
        encoding="utf-8",
    )

    _validate_runtime_snapshot(snapshot)


def test_runtime_snapshot_rejects_count_mismatch(tmp_path: Path) -> None:
    snapshot = tmp_path / "state.json"
    snapshot.write_text(
        """{
          "frame": 42,
          "gameStatus": "playing",
          "player": {"health": 100, "maxHealth": 100, "position": [0, 1, 0]},
          "enemies": [],
          "enemiesAlive": 1,
          "currentWave": 1,
          "ui": {"endScreenVisible": false},
          "errors": []
        }""",
        encoding="utf-8",
    )

    with pytest.raises(UnityToolError, match="invariants"):
        _validate_runtime_snapshot(snapshot)
