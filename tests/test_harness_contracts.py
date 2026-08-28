from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.harness.contracts import (
    EngineName,
    HarnessProfile,
    RunSpec,
    TaskSourceKind,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.run_factory import create_run_spec
from gameforge.harness.task_sources import ChangeRequestTaskSource, CreateBriefTaskSource


def test_create_and_change_sources_normalize_to_same_contract(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Create a small arena with a restart button.\n", encoding="utf-8")

    create_task = CreateBriefTaskSource(brief, acceptance=("game starts",)).load(project)
    change_task = ChangeRequestTaskSource(
        "Increase weapon damage.",
        editable_paths=("Assets/Scripts/Weapon.cs",),
    ).load(project)

    assert create_task.source is TaskSourceKind.CREATE
    assert change_task.source is TaskSourceKind.CHANGE
    assert create_task.project_path == project.resolve()
    assert change_task.editable_paths == ("Assets/Scripts/Weapon.cs",)
    assert create_task.source_sha256 != change_task.source_sha256


def test_run_spec_separates_task_model_engine_and_profile(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource("Add one enemy.").load(project)
    model = ModelProfile(provider="mock", model="deterministic-mock")

    run_spec = create_run_spec(
        task,
        model,
        engine=EngineName.UNITY,
        engine_version="6000.3.20f1",
        profile=HarnessProfile.PROJECT,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert run_spec.run_id == "20260805T000000Z-project-change"
    assert run_spec.model.model == "deterministic-mock"
    assert run_spec.task.instruction == "Add one enemy."
    assert run_spec.evaluation.required_gates[-1] == "smoke"


def test_official_compatible_profile_requires_benchmark_task(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Change HUD color.").load(tmp_path)
    model = ModelProfile(provider="mock", model="mock")

    with pytest.raises(ValidationError, match="benchmark task"):
        create_run_spec(
            task,
            model,
            engine=EngineName.UNITY,
            engine_version="6000.3.20f1",
            profile=HarnessProfile.OFFICIAL_COMPATIBLE,
        )


def test_run_spec_rejects_embedded_api_key(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Change HUD color.").load(tmp_path)
    model = ModelProfile(provider="mock", model="mock")
    payload = create_run_spec(
        task,
        model,
        engine=EngineName.UNITY,
        engine_version="6000.3.20f1",
        profile=HarnessProfile.PROJECT,
    ).model_dump(mode="json")
    payload["model"]["api_key"] = "must-not-be-accepted"

    with pytest.raises(ValidationError, match="api_key"):
        RunSpec.model_validate(payload)
