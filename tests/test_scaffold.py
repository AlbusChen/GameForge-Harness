from __future__ import annotations

from pathlib import Path

import pytest

from gameforge.scaffold import initialize_workspace


def test_initialize_workspace_copies_version_matched_starter(tmp_path: Path) -> None:
    destination = tmp_path / "starter"

    copied = initialize_workspace(destination)

    assert {path.name for path in copied} == {"benchmarks", "config", "specs", "unity"}
    assert (destination / "config" / "model.example.yaml").is_file()
    project_version = (
        destination / "unity" / "ArenaTemplate" / "ProjectSettings" / "ProjectVersion.txt"
    )
    assert project_version.is_file()


def test_initialize_workspace_refuses_to_merge_with_existing_files(tmp_path: Path) -> None:
    destination = tmp_path / "starter"
    destination.mkdir()
    (destination / "human.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(ValueError, match="absent or empty"):
        initialize_workspace(destination)

    assert (destination / "human.txt").read_text(encoding="utf-8") == "keep\n"
