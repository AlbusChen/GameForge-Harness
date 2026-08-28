from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gameforge.harness.godot_broker import (
    GodotHostBroker,
    broker_client_environment,
    run_client,
)


def _broker(tmp_path: Path, project: Path, *, timeout_seconds: float = 5.0) -> GodotHostBroker:
    return GodotHostBroker(
        executable=Path(sys.executable),
        project_root=project,
        exchange_root=tmp_path / "exchange",
        lock_file=tmp_path / "godot.lock",
        timeout_seconds=timeout_seconds,
    )


def test_broker_client_environment_survives_login_shell_path_reset(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    wrapper = tools / "godot"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")

    environment = broker_client_environment(tools, wrapper)

    assert environment["PATH"].split(os.pathsep)[0] == str(tools)
    assert environment["GODOT"] == str(wrapper)
    assert environment["GAMEFORGE_PINNED_GODOT_WRAPPER"] == str(wrapper)
    assert environment["ZDOTDIR"] == str(tools / "shell-config")
    assert (tools / "shell-config" / ".zshenv").read_text().startswith(f"export PATH={tools}:")


def test_host_broker_preserves_arguments_output_and_exit_code(
    tmp_path: Path, monkeypatch, capfd
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    source = (
        "import json, os, sys; "
        "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'home': os.environ['HOME'], 'secret': os.environ.get('DEMO_API_KEY')})); "
        "print('broker-stderr', file=sys.stderr); "
        "raise SystemExit(7)"
    )
    monkeypatch.setenv("DEMO_API_KEY", "must-not-reach-engine")

    with _broker(tmp_path, project) as broker:
        return_code = run_client(
            exchange_root=broker.exchange_root,
            token=broker._token,
            arguments=("-c", source, "one", "two"),
            timeout_seconds=10,
        )

    captured = capfd.readouterr()
    payload = json.loads(captured.out)
    assert return_code == 7
    assert payload["argv"] == ["one", "two"]
    assert payload["cwd"] == str(project)
    assert Path(payload["home"]).parent == broker.exchange_root / "homes"
    assert payload["secret"] is None
    assert captured.err == "broker-stderr\n"
    assert len(broker.events) == 1
    assert broker.events[0]["return_code"] == 7
    assert broker.events[0]["rejected"] is False
    assert broker.events[0]["arguments_sha256"]


def test_host_broker_rejects_cwd_outside_disposable_project(
    tmp_path: Path, monkeypatch, capfd
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    monkeypatch.chdir(outside)

    with _broker(tmp_path, project) as broker:
        return_code = run_client(
            exchange_root=broker.exchange_root,
            token=broker._token,
            arguments=("--version",),
            timeout_seconds=10,
        )

    assert return_code == 125
    assert "cwd must remain inside" in capfd.readouterr().err
    assert broker.events[0]["rejected"] is True


def test_host_broker_rejects_explicit_project_path_escape(
    tmp_path: Path, monkeypatch, capfd
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    monkeypatch.chdir(project)

    with _broker(tmp_path, project) as broker:
        return_code = run_client(
            exchange_root=broker.exchange_root,
            token=broker._token,
            arguments=("--path", str(outside), "--editor", "--quit"),
            timeout_seconds=10,
        )

    assert return_code == 125
    assert "--path must remain inside" in capfd.readouterr().err


def test_host_broker_does_not_interpret_non_project_selection_arguments(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    broker = _broker(tmp_path, project)

    broker._validate_explicit_paths(
        (
            "--script",
            str(outside / "probe.gd"),
            "--log-file",
            str(outside / "godot.log"),
            "--remote-fs",
            "127.0.0.1:6010",
        ),
        project,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ("--headless", "--write-movie", "frame.png", "--quit-after", "1"),
        ("--write-movie=frame.png", "--display-driver", "headless"),
        ("--display-driver=headless", "--write-movie=frame.avi"),
    ],
)
def test_host_broker_rejects_headless_movie_capture_without_starting_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    marker = project / "engine-started"
    executable = tmp_path / "fake-godot"
    executable.write_text(
        f"#!/bin/sh\nprintf started > {marker}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    broker = GodotHostBroker(
        executable=executable,
        project_root=project,
        exchange_root=tmp_path / "exchange",
        lock_file=tmp_path / "godot.lock",
    )

    with broker:
        return_code = run_client(
            exchange_root=broker.exchange_root,
            token=broker._token,
            arguments=arguments,
            timeout_seconds=10,
        )

    assert return_code == 125
    assert not marker.exists()
    assert "movie capture requires rendered frames" in capfd.readouterr().err
    assert broker.events[0]["rejected"] is True
    assert broker.events[0]["rejection_reason"] == "incompatible_rendering_options"


def test_host_broker_allows_movie_capture_with_rendering_enabled(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    broker = _broker(tmp_path, project)

    broker._validate_compatible_arguments(
        ("--path", ".", "--write-movie", "frame.png", "--quit-after", "1")
    )


def test_host_broker_terminates_timed_out_process(tmp_path: Path, monkeypatch, capfd) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    with _broker(tmp_path, project, timeout_seconds=0.05) as broker:
        return_code = run_client(
            exchange_root=broker.exchange_root,
            token=broker._token,
            arguments=("-c", "import time; time.sleep(30)"),
            timeout_seconds=10,
        )

    assert return_code == 124
    assert capfd.readouterr().err == ""
    assert broker.events[0]["timeout_stage"] == "execution"
    assert broker.events[0]["execution_timeout_seconds"] == 0.05


def test_host_broker_returns_exit_134_without_automatic_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    home_log = project / "homes.txt"
    executable = tmp_path / "fake-godot"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        f"with Path({str(home_log)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(os.environ['HOME'] + '\\n')\n"
        "os._exit(134)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    broker = GodotHostBroker(
        executable=executable,
        project_root=project,
        exchange_root=tmp_path / "exchange",
        lock_file=tmp_path / "godot.lock",
        timeout_seconds=2,
    )

    with broker:
        return_code = run_client(
            exchange_root=broker.exchange_root,
            token=broker._token,
            arguments=("--headless", "--version"),
            timeout_seconds=10,
        )

    captured = capfd.readouterr()
    homes = home_log.read_text(encoding="utf-8").splitlines()
    assert return_code == 134
    assert captured.out == ""
    assert captured.err == ""
    assert len(homes) == 1
    assert broker.events[0]["attempt_return_codes"] == [134]
    assert broker.events[0]["attempt_count"] == 1
    assert broker.events[0]["crash_retries"] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-specific")
def test_real_godot_broker_crosses_restricted_client_boundary(tmp_path: Path) -> None:
    godot = Path("/private/tmp/godot-4.4.1/Godot.app/Contents/MacOS/Godot")
    if not godot.is_file():
        pytest.skip("pinned Godot 4.4.1 is unavailable")
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text(
        '[application]\nconfig/name="Broker Integration Test"\n', encoding="utf-8"
    )
    exchange = tmp_path / "exchange"
    profile = f"""(version 1)
    (deny default)
    (allow process*)
    (allow file-read*)
    (allow file-write* (subpath \"{exchange}\"))
    (allow sysctl-read)
    (allow mach-lookup)
    """
    broker = GodotHostBroker(
        executable=godot,
        project_root=project,
        exchange_root=exchange,
        lock_file=tmp_path / "godot.lock",
        timeout_seconds=30,
    )

    with broker:
        completed = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                sys.executable,
                *broker.client_arguments,
                "--version",
            ],
            cwd=project,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        imported = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                sys.executable,
                *broker.client_arguments,
                "--headless",
                "--path",
                ".",
                "--editor",
                "--quit",
            ],
            cwd=project,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "4.4.1.stable.official.49a5bc7b6"
    assert imported.returncode == 0, imported.stderr
    assert [event["return_code"] for event in broker.events] == [0, 0]
