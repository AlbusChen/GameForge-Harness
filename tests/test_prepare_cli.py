import json
from pathlib import Path

from gameforge.cli import main


def test_prepare_change_prints_run_spec(tmp_path: Path, capsys: object) -> None:
    project = tmp_path / "project"
    project.mkdir()

    exit_code = main(
        [
            "prepare",
            "--source",
            "change",
            "--request",
            "Increase weapon damage without changing the HUD.",
            "--project",
            str(project),
            "--model-profile",
            "config/model.mock.yaml",
            "--editable-path",
            "Assets/Scripts/Weapon.cs",
        ]
    )
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["profile"] == "project"
    assert payload["task"]["source"] == "change"
    assert payload["task"]["editable_paths"] == ["Assets/Scripts/Weapon.cs"]
    assert "api_key" not in output


def test_prepare_programmable_emits_v2_with_reviewed_game_task(
    tmp_path: Path, capsys: object
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    game_task = tmp_path / "game-task.json"
    game_task.write_text(
        json.dumps(
            {
                "engine_profile": "godot-headless",
                "entry_points": ["scenes/Main.tscn"],
                "target_platforms": ["linux"],
                "requirements": [
                    {
                        "id": "structure",
                        "dimension": "structure",
                        "enforcement": "required",
                        "evaluator_ref": "import_project",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "prepare",
            "--source",
            "change",
            "--request",
            "Create the main scene.",
            "--project",
            str(project),
            "--engine",
            "godot",
            "--engine-version",
            "4.4",
            "--model-profile",
            "config/model.mock.yaml",
            "--runtime-protocol",
            "programmable-v1",
            "--game-task",
            str(game_task),
            "--target-platform",
            "linux",
        ]
    )
    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]

    assert exit_code == 0
    assert payload["version"] == 2
    assert payload["runtime_protocol"] == "programmable-v1"
    assert payload["game_task"]["requirements"][0]["dimension"] == "structure"
    assert payload["engine_environment"]["target_platform"] == "linux"
