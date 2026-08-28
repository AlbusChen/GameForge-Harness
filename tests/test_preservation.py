from __future__ import annotations

from pathlib import Path

from gameforge.harness.preservation import compare_snapshots, snapshot_project


def test_preservation_allows_only_exact_editable_file_and_its_meta(tmp_path: Path) -> None:
    approved = tmp_path / "Approved.cs"
    approved.write_text("before\n", encoding="utf-8")
    keep = tmp_path / "HumanChange.cs"
    keep.write_text("pre-existing human edit\n", encoding="utf-8")
    before = snapshot_project(tmp_path)

    approved.write_text("after\n", encoding="utf-8")
    (tmp_path / "Approved.cs.meta").write_text("new metadata\n", encoding="utf-8")
    after = snapshot_project(tmp_path)
    report = compare_snapshots(before, after, ("Approved.cs",))

    assert report.passed
    assert report.allowed_changed_paths == ("Approved.cs", "Approved.cs.meta")
    assert report.unrelated_changed_paths == ()


def test_preservation_detects_change_to_preexisting_human_file(tmp_path: Path) -> None:
    approved = tmp_path / "Approved.cs"
    approved.write_text("before\n", encoding="utf-8")
    keep = tmp_path / "HumanChange.cs"
    keep.write_text("pre-existing human edit\n", encoding="utf-8")
    before = snapshot_project(tmp_path)

    keep.write_text("overwritten\n", encoding="utf-8")
    after = snapshot_project(tmp_path)
    report = compare_snapshots(before, after, ("Approved.cs",))

    assert not report.passed
    assert report.unrelated_changed_paths == ("HumanChange.cs",)


def test_preservation_ignores_engine_generated_import_sidecars(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sidecar = project / "sprite.png.import"
    sidecar.write_text("old engine metadata\n", encoding="utf-8")
    before = snapshot_project(project)

    sidecar.write_text("new engine metadata\n", encoding="utf-8")
    after = snapshot_project(project)
    report = compare_snapshots(before, after, ())

    assert report.passed
    assert report.changed_paths == ()
