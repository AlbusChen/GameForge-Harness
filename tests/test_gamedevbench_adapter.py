from __future__ import annotations

import json
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

import gameforge.benchmarking.gamedevbench as gamedevbench
from gameforge.benchmarking.gamedevbench import (
    GameDevBenchOfficialEvaluator,
    GameDevBenchSource,
    GameDevBenchTaskRoute,
    GodotEngineCrash,
    GodotEnvironmentError,
    GodotHarnessEngineAdapter,
    _build_model_visual_context,
    _capture_fixed_camera,
    _gamedevbench_game_task,
    _godot_process_slot,
    _isolated_godot_project,
    _isolated_godot_runtime,
    _retain_model_visual_context,
    _route_budgets,
    _route_task,
    _run_bounded_process,
)
from gameforge.harness.contracts import GateStatus
from gameforge.harness.game_tasks import AcceptanceDimension, RuntimeProtocol
from gameforge.harness.project_scope import ProjectAccessScope
from gameforge.orchestrator.policy import PolicyViolation


class FakeBenchmarkSource:
    def add_hidden_tests(self, validation: Path) -> None:
        (validation / "hidden-tests-added").write_text("yes", encoding="utf-8")

    def public_file_bytes(self, relative_path: str) -> bytes | None:
        del relative_path
        return None


def test_official_task_extraction_does_not_expose_hidden_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "official"
    archive_path = root / "tasks" / "task_0099.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("tasks/task_0099/project.godot", "[application]\n")
        archive.writestr("tasks/task_0099/scenes/UI.tscn", "[gd_scene format=3]\n")
        archive.writestr("tasks/task_0099/scenes/test.tscn", "hidden scene")
        archive.writestr("tasks/task_0099/scripts/test.gd", "hidden script")
        archive.writestr("tasks/task_0099/task_config.json", '{"instruction":"Build UI"}')
        archive.writestr("tasks/task_0099/.godot/cache", "hidden cache")
    monkeypatch.setattr(GameDevBenchSource, "validate", lambda self: None)
    source = GameDevBenchSource(root, "task_0099", ("scenes/UI.tscn",))

    workspace = source.prepare_workspace(tmp_path / "workspace")

    assert (workspace / "project.godot").is_file()
    assert (workspace / "scenes" / "UI.tscn").is_file()
    assert not (workspace / "scenes" / "test.tscn").exists()
    assert not (workspace / "scripts" / "test.gd").exists()
    assert not (workspace / "task_config.json").exists()
    assert not (workspace / ".godot").exists()


def test_programmable_game_task_separates_public_import_and_hidden_behavior() -> None:
    game_task = _gamedevbench_game_task()

    assert [item.id for item in game_task.public_projection().requirements] == ["public-import"]
    official = next(item for item in game_task.requirements if item.id == "official-behavior")
    assert official.dimension is AcceptanceDimension.BEHAVIOR
    assert not official.model_visible
    assert game_task.asset_policy.allow_text_scope_expansion


def test_programmable_route_budgets_allow_a_separate_finish_turn() -> None:
    legacy = _route_budgets("simple")
    programmable = _route_budgets("simple", runtime_protocol=RuntimeProtocol.PROGRAMMABLE_V1)

    assert legacy.max_turns == 6
    assert programmable.max_turns == 40
    assert programmable.max_tool_calls == 80


@pytest.mark.parametrize(
    ("test_output", "expected"),
    (
        ("", GateStatus.NOT_RUN),
        ("VALIDATION_PASSED", GateStatus.PASS),
        ("SCRIPT ERROR: Invalid operands", GateStatus.FAIL),
    ),
)
def test_official_evaluator_preserves_timeout_verdict_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_output: str,
    expected: GateStatus,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "project.godot").write_text("[application]\n", encoding="utf-8")
    commands: list[list[str]] = []

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0 if len(commands) == 1 else 124,
            "" if len(commands) == 1 else test_output,
            "timed out" if len(commands) == 2 else "",
        )

    monkeypatch.setattr("gameforge.benchmarking.gamedevbench._run_bounded_process", bounded)
    evaluator = GameDevBenchOfficialEvaluator(  # type: ignore[arg-type]
        FakeBenchmarkSource(),
        tmp_path / "godot",
        tmp_path / "run",
        timeout_seconds=1,
    )

    gate = evaluator._official(workspace)

    assert gate.status is expected
    assert (tmp_path / "run" / "official-evaluator.log").is_file()
    assert all("--log-file" in command for command in commands)
    assert str(tmp_path / "run" / "official-import-engine.log") in commands[0]
    assert str(tmp_path / "run" / "official-test-engine.log") in commands[1]


def test_explicit_official_pass_overrides_nonfatal_import_script_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "project.godot").write_text("[application]\n", encoding="utf-8")
    responses = iter(
        (
            subprocess.CompletedProcess(
                ["godot"],
                0,
                "SCRIPT ERROR: editor-only helper used a placeholder instance\n",
                "",
            ),
            subprocess.CompletedProcess(
                ["godot"],
                0,
                "VALIDATION_PASSED: authoritative task assertions passed\n",
                "",
            ),
        )
    )
    monkeypatch.setattr(
        "gameforge.benchmarking.gamedevbench._run_bounded_process",
        lambda command, *, timeout_seconds: next(responses),
    )
    evaluator = GameDevBenchOfficialEvaluator(  # type: ignore[arg-type]
        FakeBenchmarkSource(),
        tmp_path / "godot",
        tmp_path / "run",
        timeout_seconds=1,
    )

    gate = evaluator._official(workspace)

    assert gate.status is GateStatus.PASS


def test_official_evaluator_skips_hanging_test_for_new_fatal_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    script = workspace / "scripts" / "candidate.gd"
    script.parent.mkdir(parents=True)
    (workspace / "project.godot").write_text("[application]\n", encoding="utf-8")
    script.write_text("var broken := value\n", encoding="utf-8")
    commands: list[list[str]] = []

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            (
                "SCRIPT ERROR: Parse Error: warning treated as error\n"
                "   at: GDScript::reload (res://scripts/candidate.gd:1)\n"
            ),
            "",
        )

    monkeypatch.setattr("gameforge.benchmarking.gamedevbench._run_bounded_process", bounded)
    evaluator = GameDevBenchOfficialEvaluator(  # type: ignore[arg-type]
        FakeBenchmarkSource(),
        tmp_path / "godot",
        tmp_path / "run",
        timeout_seconds=1,
    )

    gate = evaluator._official(workspace)

    assert gate.status is GateStatus.FAIL
    assert len(commands) == 1
    assert "hidden test was not started" in (
        tmp_path / "run" / "official-test-engine.log"
    ).read_text(encoding="utf-8")


def test_godot_process_crash_is_retried_without_becoming_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        (
            subprocess.CompletedProcess(
                ["godot"],
                -6,
                "handle_crash: Program crashed with signal 11",
                "",
            ),
            subprocess.CompletedProcess(["godot"], 0, "VALIDATION_PASSED", ""),
        )
    )

    def once(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        calls.append(command)
        result = next(responses)
        return subprocess.CompletedProcess(
            command,
            result.returncode,
            result.stdout,
            result.stderr,
        )

    monkeypatch.setattr(gamedevbench, "_run_process_once", once)
    log_path = tmp_path / "phase.log"

    result = _run_bounded_process(
        ["godot", "--log-file", str(log_path)],
        timeout_seconds=1,
    )

    assert result.returncode == 0
    assert len(calls) == 2
    assert calls[1][-1].endswith("phase.retry-1.log")
    evidence = next(tmp_path.glob("phase.engine-crash-attempt-1-invocation-*.log"))
    assert evidence.is_file()
    assert "return_code=-6" in evidence.read_text(encoding="utf-8")


def test_macos_godot_processes_use_private_core_foundation_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gamedevbench.sys, "platform", "darwin")
    runtime = tmp_path / "runtime"
    executable = runtime / "Godot.app" / "Contents" / "MacOS" / "Godot"
    executable.parent.mkdir(parents=True)
    executable.touch()
    (runtime / "._sc_").touch()

    environment = gamedevbench._godot_process_environment(str(executable))

    assert environment is not None
    assert Path(environment["HOME"]).parent == runtime / "home"
    assert environment["CFFIXED_USER_HOME"] == environment["HOME"]


def test_host_godot_process_slot_holds_cross_process_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if gamedevbench.fcntl is None:
        pytest.skip("POSIX file locking is unavailable")
    lock_path = tmp_path / "godot.lock"
    monkeypatch.setattr(gamedevbench, "_GODOT_PROCESS_LOCK", lock_path)

    with _godot_process_slot(1) as remaining:
        assert 0 < remaining <= 1
        with lock_path.open("a+b") as contender, pytest.raises(BlockingIOError):
            gamedevbench.fcntl.flock(
                contender.fileno(),
                gamedevbench.fcntl.LOCK_EX | gamedevbench.fcntl.LOCK_NB,
            )

    with lock_path.open("a+b") as contender:
        gamedevbench.fcntl.flock(
            contender.fileno(),
            gamedevbench.fcntl.LOCK_EX | gamedevbench.fcntl.LOCK_NB,
        )
        gamedevbench.fcntl.flock(contender.fileno(), gamedevbench.fcntl.LOCK_UN)


def test_host_godot_queue_wait_does_not_consume_process_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if gamedevbench.fcntl is None:
        pytest.skip("POSIX file locking is unavailable")
    lock_path = tmp_path / "godot.lock"
    monkeypatch.setattr(gamedevbench, "_GODOT_PROCESS_LOCK", lock_path)
    observed: list[float] = []

    def acquire_after_queue() -> None:
        with _godot_process_slot(0.01) as process_timeout:
            observed.append(process_timeout)

    with lock_path.open("a+b") as blocker:
        gamedevbench.fcntl.flock(
            blocker.fileno(),
            gamedevbench.fcntl.LOCK_EX | gamedevbench.fcntl.LOCK_NB,
        )
        contender = threading.Thread(target=acquire_after_queue)
        contender.start()
        time.sleep(0.05)
        assert contender.is_alive()
        gamedevbench.fcntl.flock(blocker.fileno(), gamedevbench.fcntl.LOCK_UN)
        contender.join(timeout=1)

    assert not contender.is_alive()
    assert observed == [0.01]


def test_persistent_godot_process_crash_raises_infrastructure_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def crash(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        del timeout_seconds
        calls += 1
        return subprocess.CompletedProcess(command, 134, "", "Abort trap: 6")

    monkeypatch.setattr(gamedevbench, "_run_process_once", crash)

    with pytest.raises(GodotEngineCrash, match="after 2 attempts"):
        _run_bounded_process(
            ["godot", "--log-file", str(tmp_path / "phase.log")],
            timeout_seconds=1,
        )

    assert calls == 2
    assert len(tuple(tmp_path.glob("phase.engine-crash-attempt-1-invocation-*.log"))) == 1
    assert len(tuple(tmp_path.glob("phase.engine-crash-attempt-2-invocation-*.log"))) == 1


def test_repeated_godot_crashes_retain_each_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        return subprocess.CompletedProcess(command, 134, "", "Abort trap: 6")

    monkeypatch.setattr(gamedevbench, "_run_process_once", crash)
    command = ["godot", "--log-file", str(tmp_path / "phase.log")]

    for _ in range(2):
        with pytest.raises(GodotEngineCrash):
            _run_bounded_process(command, timeout_seconds=1)

    artifacts = tuple(tmp_path.glob("phase.engine-crash-attempt-*-invocation-*.log"))
    assert len(artifacts) == 4
    assert len({path.name for path in artifacts}) == 4


def test_godot_user_data_permission_failure_is_not_scored_as_model_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def rejected(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        return subprocess.CompletedProcess(
            command,
            -11,
            "",
            (
                "ERROR: Error attempting to create data dir: "
                "/Users/test/Library/Application Support/Godot/app_userdata/run.\n"
                "handle_crash: Program crashed with signal 11"
            ),
        )

    monkeypatch.setattr(gamedevbench, "_run_process_once", rejected)

    with pytest.raises(GodotEnvironmentError, match="host environment rejected"):
        _run_bounded_process(
            ["godot", "--log-file", str(tmp_path / "phase.log")],
            timeout_seconds=1,
        )

    assert len(tuple(tmp_path.glob("phase.environment-failure-invocation-*.log"))) == 1


def test_godot_validation_copy_drops_cache_and_preserves_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = (
        "config_version=5\n\n"
        '[application]\nconfig/name="Original"\n\n'
        '[editor_plugins]\nenabled=PackedStringArray("res://addons/example/plugin.cfg")\n'
    )
    (source / "project.godot").write_text(original, encoding="utf-8")
    (source / ".godot").mkdir()
    (source / ".godot" / "stale-cache").write_text("stale", encoding="utf-8")

    with _isolated_godot_project(source) as sandbox:
        copied = (sandbox.project / "project.godot").read_text(encoding="utf-8")
        isolated_root = sandbox.project.parent
        assert sandbox.project != source
        assert not (sandbox.project / ".godot").exists()
        assert "enabled=PackedStringArray()" in copied
        assert "res://addons/example/plugin.cfg" not in copied
        assert 'config/name="Original"' in copied

    assert (source / "project.godot").read_text(encoding="utf-8") == original
    assert (source / ".godot" / "stale-cache").is_file()
    assert not isolated_root.exists()


def test_godot_validation_copy_adds_disabled_editor_plugin_section(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = 'config_version=5\n\n[application]\nconfig/name="Original"\n'
    (source / "project.godot").write_text(original, encoding="utf-8")

    with _isolated_godot_project(source) as sandbox:
        copied = (sandbox.project / "project.godot").read_text(encoding="utf-8")
        assert copied.endswith("[editor_plugins]\nenabled=PackedStringArray()\n")

    assert (source / "project.godot").read_text(encoding="utf-8") == original


def test_fixed_camera_imports_and_captures_the_same_isolated_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    calls: list[list[str]] = []

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        calls.append(command)
        sandbox = Path(command[command.index("--path") + 1])
        if "--import" in command:
            imported = sandbox / ".godot" / "imported"
            imported.mkdir(parents=True)
            (imported / "texture.ctex").write_text("ready", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "imported", "")
        assert (sandbox / ".godot" / "imported" / "texture.ctex").is_file()
        separator = command.index("--")
        screenshot = Path(command[separator + 1])
        metadata = Path(command[separator + 2])
        Image.new("RGB", (4, 3)).save(screenshot)
        metadata.write_text(
            '{"schema_version":1,"nodes":[],"capture_size":{"x":4,"y":3}}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            0,
            "FIXED_CAPTURE_RESULT=0\nFIXED_CAPTURE_METADATA_RESULT=0\n",
            "",
        )

    monkeypatch.setattr(gamedevbench.sys, "platform", "darwin")
    monkeypatch.setattr(gamedevbench, "_run_bounded_process", bounded)
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=(),
        godot=tmp_path / "godot",
    )

    result = _capture_fixed_camera(
        adapter,
        tmp_path / "capture.png",
        tmp_path / "capture.log",
    )

    assert result["status"] == "success"
    assert result["same_imported_copy_for_capture"] is True
    assert result["fidelity_status"] == "reliable"
    assert result["coordinate_evidence"]["capture_size"] == {"x": 4, "y": 3}
    assert len(calls) == 2
    assert calls[0][calls[0].index("--path") + 1] == calls[1][calls[1].index("--path") + 1]


def test_engine_schema_introspection_uses_pinned_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    commands: list[list[str]] = []

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        commands.append(command)
        output_path = Path(command[-1])
        output_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "class_name": "AnimatedSprite2D",
                    "parent_class": "Node2D",
                    "properties": [{"name": "speed_scale", "type": 3}],
                    "methods": [{"name": "play", "args": []}],
                    "signals": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "GAMEFORGE_ENGINE_SCHEMA_OK\n", "")

    monkeypatch.setattr(gamedevbench, "_run_bounded_process", bounded)
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=(),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke(
        "inspect_engine_schema",
        {"class_name": "AnimatedSprite2D", "name_filter": "speed"},
    )

    assert result["status"] == "success"  # type: ignore[index]
    assert result["properties"][0]["name"] == "speed_scale"  # type: ignore[index]
    request = json.loads(Path(commands[0][-2]).read_text(encoding="utf-8"))
    assert request == {
        "class_name": "AnimatedSprite2D",
        "include_inherited": True,
        "name_filter": "speed",
    }
    assert "inspect_engine_schema" in adapter.available_tools()


def test_scene_evidence_capture_returns_primary_model_image_without_a_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    run = tmp_path / "run"
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=run,
        editable_paths=(),
        godot=tmp_path / "godot",
    )

    def capture(
        engine: GodotHarnessEngineAdapter,
        screenshot_path: Path,
        log_path: Path,
    ) -> dict[str, object]:
        del engine, log_path
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3)).save(screenshot_path)
        return {
            "status": "success",
            "fidelity_status": "reliable",
            "same_imported_copy_for_capture": True,
            "coordinate_evidence": {
                "schema_version": 1,
                "nodes": [],
                "capture_size": {"x": 4, "y": 3},
            },
        }

    monkeypatch.setattr(gamedevbench, "_capture_fixed_camera", capture)

    result = adapter.invoke("capture_scene_evidence", {})

    assert result["status"] == "success"  # type: ignore[index]
    assert "verdict" not in result  # type: ignore[operator]
    assert result["_model_image_paths"] == [  # type: ignore[index]
        str((run / "scene-evidence-01.png").resolve())
    ]
    assert result["evidence_contract"] == {  # type: ignore[index]
        "state_phase": "runtime_after_scene_scripts_and_settle_frames",
        "certifies_serialized_resource_identity": False,
        "certifies_all_animation_frames": False,
        "interpretation": (
            "The screenshot and node metadata describe live runtime state after scene "
            "scripts have run. Texture and animation provenance is reported when the "
            "engine exposes it, but runtime appearance cannot by itself prove that named "
            "assets, resource wrappers, or every animation frame were serialized as "
            "requested."
        ),
    }
    evidence = json.loads((run / "scene-evidence-01.json").read_text(encoding="utf-8"))
    assert evidence["contains_verdict"] is False
    assert evidence["evidence_contract"]["certifies_serialized_resource_identity"] is False
    assert "capture_scene_evidence" in adapter.available_tools()


def test_bounded_capture_retains_runtime_texture_and_animation_provenance() -> None:
    capture = {
        "status": "success",
        "coordinate_evidence": {
            "schema_version": 1,
            "state_phase": "runtime_after_scene_scripts_and_settle_frames",
            "nodes": [
                {
                    "path": "Player/AnimatedSprite2D",
                    "class": "AnimatedSprite2D",
                    "animated_sprite_2d": {
                        "sprite_frames": {
                            "animations": [
                                {
                                    "name": "idle0",
                                    "frames": [
                                        {"texture": {"resource_path": "res://assets/idle.png"}}
                                    ],
                                }
                            ]
                        }
                    },
                },
                {
                    "path": "UI/Frame",
                    "class": "NinePatchRect",
                    "texture_control": {
                        "slots": {"texture": {"resource_path": "res://assets/frame.res"}}
                    },
                },
            ],
        },
    }

    bounded = gamedevbench._bounded_capture_evidence(capture)

    metadata = bounded["coordinate_evidence"]
    assert metadata["state_phase"] == "runtime_after_scene_scripts_and_settle_frames"
    assert (
        metadata["nodes"][0]["animated_sprite_2d"]["sprite_frames"]["animations"][0]["frames"][0][
            "texture"
        ]["resource_path"]
        == "res://assets/idle.png"
    )
    assert (
        metadata["nodes"][1]["texture_control"]["slots"]["texture"]["resource_path"]
        == "res://assets/frame.res"
    )


def test_macos_godot_runtime_uses_private_self_contained_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "source" / "Godot.app" / "Contents" / "MacOS" / "Godot"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(gamedevbench.sys, "platform", "darwin")

    with _isolated_godot_runtime(executable) as runtime:
        isolated_root = runtime.root
        assert runtime.executable != executable
        assert runtime.executable.is_file()
        assert runtime.mode == "macos-self-contained-app-clone"
        assert (runtime.root / "._sc_").is_file()
        assert (runtime.root / "editor_data").is_dir()
        assert not (tmp_path / "source" / "._sc_").exists()

    assert not isolated_root.exists()


def test_public_import_loads_editable_resources_and_rejects_parse_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    scene.write_text("[gd_scene format=3]\n", encoding="utf-8")
    commands: list[list[str]] = []

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        commands.append(command)
        if len(commands) == 1:
            return subprocess.CompletedProcess(command, 0, "imported", "")
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "ERROR: res://scenes/main.tscn:5 - Parse Error: invalid resource",
        )

    monkeypatch.setattr("gameforge.benchmarking.gamedevbench._run_bounded_process", bounded)
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("project.godot", "scenes/main.tscn"),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke("import_project", {})

    assert result["status"] == "failed"  # type: ignore[index]
    assert result["resource_return_code"] == 1  # type: ignore[index]
    assert result["startup_status"] == "not_applicable_no_main_scene"  # type: ignore[index]
    assert len(commands) == 2
    assert all(command[command.index("--path") + 1] != str(project) for command in commands)
    assert "--script" in commands[1]
    assert "--log-file" in commands[1]
    assert str(tmp_path / "run" / "godot-public-resource-load.log") in commands[1]
    assert "res://scenes/main.tscn" in commands[1]
    resource_script = tmp_path / "run" / "godot-public-resource-load.gd"
    assert "resource.instantiate()" in resource_script.read_text(encoding="utf-8")
    assert "Parse Error:" in result["diagnostics"][0]  # type: ignore[index]


def test_public_import_skips_startup_without_main_scene_after_resources_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    scene.write_text("[gd_scene format=3]\n", encoding="utf-8")
    commands: list[list[str]] = []

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("gameforge.benchmarking.gamedevbench._run_bounded_process", bounded)
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke("import_project", {})

    assert result["status"] == "success"  # type: ignore[index]
    assert result["resource_return_code"] == 0  # type: ignore[index]
    assert result["isolated_validation_copy"] is True  # type: ignore[index]
    assert result["editor_plugins_disabled_in_validation_copy"] is True  # type: ignore[index]
    assert result["startup_status"] == "not_applicable_no_main_scene"  # type: ignore[index]
    assert len(commands) == 2


def test_public_import_accepts_only_diagnostics_present_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    scene.write_text("before\n", encoding="utf-8")
    observed_scenes: list[str] = []

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        validation = Path(command[command.index("--path") + 1])
        observed_scenes.append((validation / "scenes" / "main.tscn").read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            "ERROR: Failed loading resource: res://addons/preexisting.glsl.",
        )

    monkeypatch.setattr("gameforge.benchmarking.gamedevbench._run_bounded_process", bounded)
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )
    adapter.bind_project_scope(ProjectAccessScope(("scenes/main.tscn",)))
    adapter.invoke("write_text_file", {"path": "scenes/main.tscn", "content": "after\n"})

    result = adapter.invoke("import_project", {})

    assert result["status"] == "success"  # type: ignore[index]
    assert result["raw_status"] == "failed"  # type: ignore[index]
    assert result["validation_basis"] == "relative_to_preexisting_baseline"  # type: ignore[index]
    assert result["diagnostics"] == []  # type: ignore[index]
    assert result["baseline_diagnostics"] == [  # type: ignore[index]
        "ERROR: Failed loading resource: res://addons/preexisting.glsl."
    ]
    assert observed_scenes == ["after\n", "before\n"]


def test_public_import_rejects_diagnostic_introduced_by_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    scene.write_text("before\n", encoding="utf-8")

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        validation = Path(command[command.index("--path") + 1])
        changed = (validation / "scenes" / "main.tscn").read_text(encoding="utf-8") == "after\n"
        diagnostics = ["ERROR: Failed loading resource: res://addons/preexisting.glsl."]
        if changed:
            diagnostics.append("Parse Error: candidate regression")
        return subprocess.CompletedProcess(command, 0, "", "\n".join(diagnostics))

    monkeypatch.setattr("gameforge.benchmarking.gamedevbench._run_bounded_process", bounded)
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )
    adapter.bind_project_scope(ProjectAccessScope(("scenes/main.tscn",)))
    adapter.invoke("write_text_file", {"path": "scenes/main.tscn", "content": "after\n"})

    result = adapter.invoke("import_project", {})

    assert result["status"] == "failed"  # type: ignore[index]
    assert result["validation_basis"] == "regression_against_preexisting_baseline"  # type: ignore[index]
    assert result["new_diagnostics"] == ["Parse Error: candidate regression"]  # type: ignore[index]
    assert result["regressions"] == [  # type: ignore[index]
        "new diagnostic: Parse Error: candidate regression"
    ]


def test_public_import_rejects_generic_engine_error_introduced_by_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    scene.write_text("before\n", encoding="utf-8")

    def bounded(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        validation = Path(command[command.index("--path") + 1])
        changed = (validation / "scenes" / "main.tscn").read_text(encoding="utf-8") == "after\n"
        diagnostic = (
            "ERROR: Index p_layer = 1 is out of bounds (layers.size() = 1)." if changed else ""
        )
        return subprocess.CompletedProcess(command, 0, "", diagnostic)

    monkeypatch.setattr("gameforge.benchmarking.gamedevbench._run_bounded_process", bounded)
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )
    adapter.bind_project_scope(ProjectAccessScope(("scenes/main.tscn",)))
    adapter.invoke("write_text_file", {"path": "scenes/main.tscn", "content": "after\n"})

    result = adapter.invoke("import_project", {})

    assert result["status"] == "failed"  # type: ignore[index]
    assert result["validation_basis"] == (  # type: ignore[index]
        "regression_against_preexisting_baseline"
    )
    assert result["new_diagnostics"] == [  # type: ignore[index]
        "ERROR: Index p_layer = 1 is out of bounds (layers.size() = 1)."
    ]


def test_common_godot_shader_sources_are_public_text_and_visual_review_is_safe(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "shared.txt").write_bytes("Copyright ©\n".encode("cp1252"))
    for name in ("cloud.comp", "display.rast", "common.gdshaderinc"):
        (project / name).write_text("shader source\n", encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=(),
        godot=tmp_path / "godot",
    )
    adapter.bind_project_scope(ProjectAccessScope((), allow_text_scope_expansion=True))

    listed = adapter.invoke("list_project_files", {"limit": 512})
    legacy_text = adapter.invoke("read_text_file", {"path": "shared.txt"})
    searched = adapter.invoke("search_project_text", {"query": "Copyright"})
    review = adapter.invoke("review_visual_change", {})

    assert {item["path"] for item in listed["files"]} == {  # type: ignore[index]
        "shared.txt",
        "cloud.comp",
        "display.rast",
        "common.gdshaderinc",
    }
    assert legacy_text["content"] == "Copyright ©\n"  # type: ignore[index]
    assert legacy_text["source_encoding"] == "windows-1252"  # type: ignore[index]
    assert searched["coverage_complete"] is True  # type: ignore[index]
    assert searched["matches"][0]["path"] == "shared.txt"  # type: ignore[index]
    assert "review_visual_change" in adapter.available_tools()
    assert review["status"] == "not_applicable"  # type: ignore[index]
    declaration = adapter.invoke(
        "declare_output_manifest",
        {"paths": ["shared.txt", "generated/icon.png"], "reason": "task outputs"},
    )
    assert declaration["added_paths"] == ["generated/icon.png", "shared.txt"]  # type: ignore[index]
    with pytest.raises(PolicyViolation, match="project caches or generated metadata"):
        adapter.invoke(
            "declare_output_manifest",
            {"paths": [".godot/editor/state"], "reason": "unsafe internal path"},
        )


def test_godot_text_write_is_limited_to_exact_approved_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "UI.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text("old\n", encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/UI.tscn",),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke(
        "write_text_file",
        {"path": "scenes/UI.tscn", "content": "new\n"},
    )

    assert result["status"] == "success"  # type: ignore[index]
    assert scene.read_text(encoding="utf-8") == "new\n"
    with pytest.raises(PolicyViolation, match="approved editable set"):
        adapter.invoke(
            "write_text_file",
            {"path": "project.godot", "content": "replacement"},
        )


def test_godot_text_read_covers_public_project_without_expanding_write_scope(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shader = project / "scripts" / "effect.gdshader"
    shader.parent.mkdir(parents=True)
    shader.write_text("shader_type spatial;\n", encoding="utf-8")
    (project / "outside.gd").write_text("extends Node\n", encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scripts/effect.gdshader",),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke("read_text_file", {"path": "scripts/effect.gdshader"})

    assert result["status"] == "success"  # type: ignore[index]
    assert result["content"] == "shader_type spatial;\n"  # type: ignore[index]
    assert len(result["sha256"]) == 64  # type: ignore[arg-type,index]
    outside = adapter.invoke("read_text_file", {"path": "outside.gd"})
    assert outside["content"] == "extends Node\n"  # type: ignore[index]
    with pytest.raises(PolicyViolation, match="approved editable set"):
        adapter.invoke(
            "write_text_file",
            {"path": "outside.gd", "content": "extends Node2D\n"},
        )


def test_godot_text_read_pages_large_utf8_files_without_growing_observations(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "scripts" / "large.gd"
    source.parent.mkdir(parents=True)
    expected = "extends Node\n" + ("é" * 40_000)
    source.write_text(expected, encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scripts/large.gd",),
        godot=tmp_path / "godot",
    )

    pages: list[str] = []
    cursor: int | None = 0
    while cursor is not None:
        page = adapter.invoke(
            "read_text_file",
            {"path": "scripts/large.gd", "cursor": cursor, "limit": 65_535},
        )
        pages.append(str(page["content"]))  # type: ignore[index]
        assert len(str(page["content"]).encode("utf-8")) <= 65_535  # type: ignore[index]
        cursor = page["next_cursor"]  # type: ignore[assignment,index]

    assert "".join(pages) == expected
    assert page["coverage_complete"] is True  # type: ignore[index]
    assert page["total_bytes"] == len(expected.encode("utf-8"))  # type: ignore[index]
    with pytest.raises(ValueError, match="UTF-8 boundary"):
        adapter.invoke(
            "read_text_file",
            {"path": "scripts/large.gd", "cursor": 14, "limit": 64},
        )
    beyond_eof = adapter.invoke(
        "read_text_file",
        {"path": "scripts/large.gd", "cursor": 1_000_000, "limit": 64},
    )
    assert beyond_eof["content"] == ""  # type: ignore[index]
    assert beyond_eof["coverage_complete"] is True  # type: ignore[index]
    assert beyond_eof["cursor_clamped"] is True  # type: ignore[index]
    search = adapter.invoke("search_project_text", {"query": "ééé"})
    assert search["large_files_skipped"] == 1  # type: ignore[index]
    assert search["coverage_complete"] is False  # type: ignore[index]
    assert search["truncated"] is True  # type: ignore[index]


def test_project_discovery_and_audited_output_manifest_are_separate(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    script = project / "scripts" / "player.gd"
    script.parent.mkdir(parents=True)
    script.write_text("extends Node\nvar speed = 4\n", encoding="utf-8")
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("project.godot",),
        godot=tmp_path / "godot",
    )
    scope = ProjectAccessScope(
        ("project.godot",),
        allow_text_scope_expansion=True,
        maximum_writable_paths=4,
    )
    adapter.bind_project_scope(scope)

    listed = adapter.invoke("list_project_files", {})
    searched = adapter.invoke("search_project_text", {"query": "speed"})
    declaration = adapter.invoke(
        "declare_output_manifest",
        {
            "paths": ["scripts/player.gd", "scripts/camera.gd"],
            "reason": "update the player and add its requested camera helper",
        },
    )
    written = adapter.invoke(
        "write_text_file",
        {"path": "scripts/camera.gd", "content": "extends Camera2D\n"},
    )
    batch = adapter.invoke(
        "read_text_files",
        {"paths": ["scripts/player.gd", "scripts/camera.gd"]},
    )
    review = adapter.invoke("review_project_changes", {})

    assert {item["path"] for item in listed["files"]} == {  # type: ignore[index]
        "project.godot",
        "scripts/player.gd",
    }
    assert searched["matches"][0]["path"] == "scripts/player.gd"  # type: ignore[index]
    assert declaration["added_paths"] == ["scripts/camera.gd", "scripts/player.gd"]  # type: ignore[index]
    assert written["status"] == "success"  # type: ignore[index]
    assert [item["path"] for item in batch["files"]] == [  # type: ignore[index]
        "scripts/player.gd",
        "scripts/camera.gd",
    ]
    assert review["changed_paths"] == ["scripts/camera.gd"]  # type: ignore[index]
    assert "+++ after/scripts/camera.gd" in review["files"][0]["diff"]  # type: ignore[index]
    assert scope.snapshot()["declarations"]


def test_batch_structured_resource_inspection_is_read_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scenes = project / "scenes"
    scenes.mkdir(parents=True)
    for name in ("one.tscn", "two.tscn"):
        (scenes / name).write_text(
            f'[gd_scene format=3]\n[node name="{name}" type="Node"]\n',
            encoding="utf-8",
        )
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/one.tscn",),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke(
        "inspect_resources",
        {"paths": ["scenes/one.tscn", "scenes/two.tscn"]},
    )

    assert [item["path"] for item in result["resources"]] == [  # type: ignore[index]
        "scenes/one.tscn",
        "scenes/two.tscn",
    ]


def test_scene_node_path_resolver_uses_exact_scene_hierarchy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "[gd_scene format=3]\n\n"
        '[node name="Main" type="Node3D"]\n\n'
        '[node name="Cultist" type="Node3D" parent="."]\n\n'
        '[node name="Rig" type="Skeleton3D" parent="Cultist"]\n\n'
        '[node name="IK" type="SkeletonIK3D" parent="Cultist/Rig"]\n\n'
        '[node name="TargetContainer" type="Node3D" parent="Cultist"]\n\n'
        '[node name="Target" type="Marker3D" parent="Cultist/TargetContainer"]\n',
        encoding="utf-8",
    )
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke(
        "resolve_scene_node_path",
        {
            "scene": "scenes/main.tscn",
            "property_owner_node": "Main/Cultist/Rig/IK",
            "to_node": "Main/Cultist/TargetContainer/Target",
        },
    )

    assert result["node_path"] == "../../TargetContainer/Target"  # type: ignore[index]
    assert result["common_ancestor"] == "Main/Cultist"  # type: ignore[index]

    planned = adapter.invoke(
        "resolve_scene_node_path",
        {
            "scene": "scenes/main.tscn",
            "property_owner_node": "Main/Cultist/Rig/PlannedIK",
            "to_node": "Main/Cultist/TargetContainer/Target",
        },
    )
    assert planned["node_path"] == "../../TargetContainer/Target"  # type: ignore[index]
    assert planned["source_planned"] is True  # type: ignore[index]
    assert planned["property_owner_node"] == "Main/Cultist/Rig/PlannedIK"  # type: ignore[index]

    planned_target = adapter.invoke(
        "resolve_scene_node_path",
        {
            "scene": "scenes/main.tscn",
            "property_owner_node": "Main/Cultist/Rig/IK",
            "to_node": "Main/Cultist/TargetContainer/PlannedTarget",
        },
    )
    assert planned_target["node_path"] == "../../TargetContainer/PlannedTarget"  # type: ignore[index]
    assert planned_target["target_planned"] is True  # type: ignore[index]


def test_public_auto_scope_includes_existing_and_named_new_text_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "official"
    archive_path = root / "tasks" / "task_0042.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("tasks/task_0042/project.godot", "[application]\n")
        archive.writestr("tasks/task_0042/scenes/main.tscn", "[gd_scene format=3]\n")
        archive.writestr("tasks/task_0042/scenes/test.tscn", "hidden scene")
        archive.writestr(
            "tasks/task_0042/task_config.json",
            '{"instruction":"Create res://scripts/new_feature.gd and update the scene."}',
        )
    monkeypatch.setattr(GameDevBenchSource, "validate", lambda self: None)
    source = GameDevBenchSource(root, "task_0042", ())

    scope = source.suggested_editable_paths()

    assert scope == ("project.godot", "scenes/main.tscn", "scripts/new_feature.gd")
    assert "scenes/test.tscn" not in scope


def test_programmable_scope_does_not_guess_directory_for_bare_new_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "official"
    archive_path = root / "tasks" / "task_0042.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("tasks/task_0042/project.godot", "[application]\n")
        archive.writestr("tasks/task_0042/scenes/main.tscn", "[gd_scene format=3]\n")
        archive.writestr("tasks/task_0042/scripts/pong.gd", "extends Node\n")
        archive.writestr(
            "tasks/task_0042/task_config.json",
            '{"instruction":"Add Camera2D.gd and update pong.gd."}',
        )
    monkeypatch.setattr(GameDevBenchSource, "validate", lambda self: None)
    source = GameDevBenchSource(root, "task_0042", ())

    assert "Camera2D.gd" in source.suggested_editable_paths()
    programmable = source.suggested_editable_paths(allow_deferred_declaration=True)
    assert "Camera2D.gd" not in programmable
    assert programmable == (
        "project.godot",
        "scenes/main.tscn",
        "scripts/pong.gd",
    )


def test_programmable_scope_can_defer_large_unnamed_outputs_to_runtime_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "official"
    archive_path = root / "tasks" / "task_0043.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("tasks/task_0043/project.godot", "[application]\n")
        archive.writestr("tasks/task_0043/scenes/main.tscn", "[gd_scene format=3]\n")
        archive.writestr(
            "tasks/task_0043/task_config.json",
            '{"instruction":"Improve the project architecture and behavior."}',
        )
    monkeypatch.setattr(GameDevBenchSource, "validate", lambda self: None)
    monkeypatch.setattr(gamedevbench, "_context_fits", lambda paths, sizes: False)
    source = GameDevBenchSource(root, "task_0043", ())

    with pytest.raises(ValueError, match="too large"):
        source.suggested_editable_paths()

    assert source.suggested_editable_paths(allow_deferred_declaration=True) == ()


def test_public_auto_scope_includes_explicit_engine_native_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "official"
    archive_path = root / "tasks" / "task_0044.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("tasks/task_0044/project.godot", "[application]\n")
        archive.writestr("tasks/task_0044/materials/effect.material", b"RSCC-binary")
        archive.writestr(
            "tasks/task_0044/task_config.json",
            '{"instruction":"Set a property in res://materials/effect.material."}',
        )
    monkeypatch.setattr(GameDevBenchSource, "validate", lambda self: None)
    source = GameDevBenchSource(root, "task_0044", ())

    assert source.suggested_editable_paths() == (
        "project.godot",
        "materials/effect.material",
    )


def test_public_auto_scope_includes_explicit_existing_binary_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "official"
    archive_path = root / "tasks" / "task_0044.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("tasks/task_0044/project.godot", "[application]\n")
        archive.writestr("tasks/task_0044/assets/sprites/item_icon.png", b"broken-png")
        archive.writestr(
            "tasks/task_0044/task_config.json",
            '{"instruction":"Use the icon from res://assets/sprites/item_icon.png."}',
        )
    monkeypatch.setattr(GameDevBenchSource, "validate", lambda self: None)
    source = GameDevBenchSource(root, "task_0044", ())

    assert source.suggested_editable_paths() == (
        "project.godot",
        "assets/sprites/item_icon.png",
    )


def test_engine_native_resource_mutation_is_scoped_typed_and_rollback_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    material = project / "materials" / "effect.material"
    material.parent.mkdir(parents=True)
    material.write_bytes(b"RSCC-before")
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("materials/effect.material",),
        godot=tmp_path / "godot",
    )

    def successful(
        command: list[str], *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        del command, timeout_seconds
        material.write_bytes(b"RSCC-after")
        return subprocess.CompletedProcess([], 0, "GAMEFORGE_ENGINE_RESOURCE_MUTATION_OK", "")

    monkeypatch.setattr(gamedevbench, "_run_bounded_process", successful)
    result = adapter.invoke(
        "mutate_engine_resource_properties",
        {
            "path": "materials/effect.material",
            "operations": [
                {
                    "property": "shader_parameter/seed",
                    "value": {"type": "Vector2", "x": 0.4, "y": -0.2},
                }
            ],
        },
    )

    assert result["status"] == "success"  # type: ignore[index]
    assert result["sha256_before"] != result["sha256_after"]  # type: ignore[index]
    assert material.read_bytes() == b"RSCC-after"
    with pytest.raises(ValueError, match="unsupported shape"):
        adapter.invoke(
            "mutate_engine_resource_properties",
            {
                "path": "materials/effect.material",
                "operations": [
                    {
                        "property": "shader_parameter/seed",
                        "value": {"type": "Vector2", "x": 0.4},
                    }
                ],
            },
        )

    def failing(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        del command, timeout_seconds
        material.write_bytes(b"partial-corruption")
        return subprocess.CompletedProcess([], 2, "", "save failed")

    monkeypatch.setattr(gamedevbench, "_run_bounded_process", failing)
    with pytest.raises(ValueError, match="engine resource mutation failed"):
        adapter.invoke(
            "mutate_engine_resource_properties",
            {
                "path": "materials/effect.material",
                "operations": [{"property": "render_priority", "value": 1}],
            },
        )
    assert material.read_bytes() == b"RSCC-after"


def test_project_text_index_pages_large_projects_and_searches_beyond_first_page(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    for index in range(600):
        (scripts / f"file_{index:04d}.gd").write_text(
            f"extends Node\nvar ordinary_{index} = {index}\n",
            encoding="utf-8",
        )
    (scripts / "zz_target.gd").write_text(
        "extends Node\nconst INDEX_SENTINEL = true\n",
        encoding="utf-8",
    )
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("project.godot",),
        godot=tmp_path / "godot",
    )

    first = adapter.invoke("list_project_files", {"limit": 512, "path_prefix": "scripts"})
    root_page = adapter.invoke("list_project_files", {"limit": 1, "path_prefix": ""})
    second = adapter.invoke(
        "list_project_files",
        {"cursor": first["next_cursor"], "limit": 512, "path_prefix": "scripts"},
    )
    searched = adapter.invoke("search_project_text", {"query": "INDEX_SENTINEL"})

    assert len(first["files"]) == 512  # type: ignore[arg-type,index]
    assert len(root_page["files"]) == 1  # type: ignore[arg-type,index]
    assert first["next_cursor"] == 512  # type: ignore[index]
    assert first["coverage_complete"] is True  # type: ignore[index]
    assert second["next_cursor"] is None  # type: ignore[index]
    assert second["filtered_file_count"] == 601  # type: ignore[index]
    assert any(  # type: ignore[index]
        item["path"] == "scripts/zz_target.gd" for item in second["files"]
    )
    assert searched["matches"][0]["path"] == "scripts/zz_target.gd"  # type: ignore[index]
    assert searched["coverage_complete"] is True  # type: ignore[index]


def test_godot_batch_text_write_validates_every_path_before_mutating(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text("old\n", encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn", "scripts/new.gd"),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke(
        "write_text_files",
        {
            "files": [
                {"path": "scenes/main.tscn", "content": "new\n"},
                {"path": "scripts/new.gd", "content": "extends Node\n"},
            ]
        },
    )

    assert result["status"] == "success"  # type: ignore[index]
    assert scene.read_text(encoding="utf-8") == "new\n"
    assert (project / "scripts" / "new.gd").read_text(encoding="utf-8") == "extends Node\n"

    scene.write_text("old again\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="approved editable set"):
        adapter.invoke(
            "write_text_files",
            {
                "files": [
                    {"path": "scenes/main.tscn", "content": "should not land\n"},
                    {"path": "outside.gd", "content": "extends Node\n"},
                ]
            },
        )
    assert scene.read_text(encoding="utf-8") == "old again\n"


def test_godot_exact_text_replacement_is_scoped_and_unique(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text("before unique after\n", encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke(
        "replace_text",
        {
            "path": "scenes/main.tscn",
            "expected": "unique",
            "replacement": "replaced",
        },
    )

    assert result["status"] == "success"  # type: ignore[index]
    assert scene.read_text(encoding="utf-8") == "before replaced after\n"
    with pytest.raises(ValueError, match="missing"):
        adapter.invoke(
            "replace_text",
            {
                "path": "scenes/main.tscn",
                "expected": "not present",
                "replacement": "anything",
            },
        )


def test_visual_route_attaches_only_public_referenced_images(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "main.tscn"
    image = project / "assets" / "platformer.jpg"
    scene.parent.mkdir(parents=True)
    image.parent.mkdir(parents=True)
    scene.write_text(
        '[ext_resource path="res://assets/platformer.jpg" type="Texture2D" id="1"]\n',
        encoding="utf-8",
    )
    image.write_bytes(b"bounded-image")

    route = _route_task(
        project,
        "Place tight circles over each coin in the image.",
        ("scenes/main.tscn",),
    )

    assert route.name == "visual"
    assert route.visual_relative_paths == ("assets/platformer.jpg",)
    assert route.visual_paths == (image,)


def test_visual_route_prioritizes_instruction_named_asset_beyond_existing_refs(
    tmp_path: Path,
) -> None:
    scene = tmp_path / "scenes" / "main.tscn"
    assets = tmp_path / "assets"
    scene.parent.mkdir(parents=True)
    assets.mkdir(parents=True)
    scene.write_text(
        '[ext_resource path="res://assets/campfire_base.png" type="Texture2D" id="1"]\n'
        '[ext_resource path="res://assets/flamelet.png" type="Texture2D" id="2"]\n',
        encoding="utf-8",
    )
    for name in ("campfire_base.png", "flamelet.png", "smokelet.png"):
        (assets / name).write_bytes(name.encode("utf-8"))

    route = _route_task(
        tmp_path,
        "Add particles that use the smokelet texture and form visible smoke ribbons.",
        ("scenes/main.tscn",),
    )

    assert route.name == "visual"
    assert route.visual_relative_paths[0] == "assets/smokelet.png"
    assert set(route.visual_relative_paths) == {
        "assets/campfire_base.png",
        "assets/flamelet.png",
        "assets/smokelet.png",
    }
    smoke = next(asset for asset in route.asset_manifest if asset.path.endswith("smokelet.png"))
    assert smoke.attached
    assert "named_in_instruction" in smoke.relevance


def test_nonvisual_route_keeps_referenced_public_image_context(tmp_path: Path) -> None:
    resource = tmp_path / "assets" / "tiles.tres"
    image = tmp_path / "assets" / "tiles.png"
    resource.parent.mkdir(parents=True)
    resource.write_text(
        '[ext_resource type="Texture2D" path="res://assets/tiles.png" id="1"]\n',
        encoding="utf-8",
    )
    Image.new("RGBA", (32, 32), "green").save(image)

    route = _route_task(
        tmp_path,
        "Add terrain metadata without changing the atlas layout.",
        ("assets/tiles.tres",),
    )

    assert route.name == "interactive"
    assert route.visual_paths == (image,)
    assert route.visual_relative_paths == ("assets/tiles.png",)


def test_visual_context_builds_directionally_diverse_sprite_catalog(
    tmp_path: Path,
) -> None:
    scene = tmp_path / "scenes" / "Player.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text("[gd_scene]\n", encoding="utf-8")
    for action in ("attack", "idle", "walk"):
        for shadow in (False, True):
            directory = tmp_path / "assets" / "crusader" / action
            if shadow:
                directory /= "_shadows"
            directory.mkdir(parents=True, exist_ok=True)
            prefix = "shadow-crusader" if shadow else "crusader"
            for direction in range(8):
                for frame in range(2):
                    path = directory / f"{prefix}_{action}_{direction}{frame:04d}.png"
                    Image.new(
                        "RGBA",
                        (24, 24),
                        (direction * 25, frame * 80, 120, 255),
                    ).save(path)

    route = _route_task(
        tmp_path,
        "Use the complete Crusader idle set and walk set for eight directional sprites.",
        ("scenes/Player.tscn",),
    )
    context = _build_model_visual_context(
        tmp_path,
        route,
        ("scenes/Player.tscn",),
        tmp_path / "run",
    )

    assert all("_shadows" not in path for path in route.visual_relative_paths[:2])
    assert any("/idle/" in path for path in route.catalog_relative_paths)
    assert any("/walk/" in path for path in route.catalog_relative_paths)
    catalog = next(record for record in context.records if record["kind"] == "visual-catalog-v1")
    source_paths = catalog["source_paths"]
    assert isinstance(source_paths, list)
    assert len([path for path in source_paths if "/idle/" in path]) == 8
    assert len([path for path in source_paths if "/walk/" in path]) == 8
    assert context.paths[-1].is_file()


def test_model_visual_context_derives_auditable_atlas_grid_without_project_write(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    image = project / "assets" / "atlas.png"
    resource = project / "assets" / "tiles.tres"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (39, 39), "blue").save(image)
    original = image.read_bytes()
    resource.write_text(
        '[ext_resource type="Texture2D" path="res://assets/atlas.png" id="1"]\n'
        'texture = ExtResource("1")\n'
        "texture_region_size = Vector2i(19, 19)\n"
        "separation = Vector2i(1, 1)\n",
        encoding="utf-8",
    )
    route = GameDevBenchTaskRoute(
        "visual",
        visual_paths=(image,),
        visual_relative_paths=("assets/atlas.png",),
    )

    context = _build_model_visual_context(
        project,
        route,
        ("assets/tiles.tres",),
        tmp_path / "run",
    )

    assert image.read_bytes() == original
    assert context.paths[0].is_file()
    assert context.paths[0].is_relative_to(tmp_path / "run")
    assert context.records[0]["region_size"] == [19, 19]
    with Image.open(context.paths[0]) as rendered:
        assert rendered.width > 39
        assert rendered.height > 39

    retained_run = tmp_path / "retained-run"
    _retain_model_visual_context(context, tmp_path / "run", retained_run)
    assert (retained_run / context.records[0]["artifact_path"]).is_file()


def test_godot_scene_inspection_returns_nodes_resources_and_properties(
    tmp_path: Path,
) -> None:
    scene = tmp_path / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "[gd_scene load_steps=2 format=3]\n\n"
        '[ext_resource type="Texture2D" path="res://assets/icon.png" id="1"]\n\n'
        '[node name="Root" type="Node2D"]\n'
        "position = Vector2(10, 20)\n",
        encoding="utf-8",
    )
    adapter = GodotHarnessEngineAdapter(
        project=tmp_path,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )

    result = adapter.invoke("inspect_scene", {"scene": "scenes/main.tscn"})

    assert result["status"] == "success"  # type: ignore[index]
    sections = result["sections"]  # type: ignore[index]
    assert any(section["section"] == "ext_resource" for section in sections)
    node = next(section for section in sections if section["section"] == "node")
    assert node["attributes"]["name"] == "Root"
    assert node["properties"]["position"] == "Vector2(10, 20)"
