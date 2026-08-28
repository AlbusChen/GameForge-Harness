from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.harness.contracts import EngineName, HarnessProfile, RunSpec, RunSpecV2
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    EngineEnvironment,
    GameTaskSpec,
    RequirementEnforcement,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.run_factory import create_run_spec, create_run_spec_v2
from gameforge.harness.task_sources import ChangeRequestTaskSource


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _game_task() -> GameTaskSpec:
    return GameTaskSpec(
        engine_profile="godot-headless",
        entry_points=("scenes/Main.tscn",),
        target_platforms=("linux",),
        requirements=(
            AcceptanceRequirement(
                id="structure",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="public-structure",
            ),
            AcceptanceRequirement(
                id="hidden-gameplay",
                dimension=AcceptanceDimension.BEHAVIOR,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="official-gameplay",
                model_visible=False,
            ),
        ),
    )


def test_run_spec_v1_serialization_is_unchanged(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Inspect the project.").load(tmp_path)
    run_spec = create_run_spec(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )

    assert type(run_spec) is RunSpec
    assert run_spec.version == 1
    assert "game_task" not in run_spec.model_dump(mode="json")


def test_run_spec_v2_pins_game_environment_and_public_projection(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Add a deterministic jump.").load(tmp_path)
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=_game_task(),
        engine_environment=EngineEnvironment(
            engine_version="4.4",
            target_platform="linux",
        ),
        capability_digests={"inspect_resource": _digest("inspect")},
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )

    assert isinstance(run_spec, RunSpecV2)
    assert run_spec.version == 2
    assert [item.id for item in run_spec.game_task.public_projection().requirements] == [
        "structure"
    ]


def test_run_spec_v2_rejects_environment_mismatch(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Add a deterministic jump.").load(tmp_path)
    with pytest.raises(ValidationError, match="version must match"):
        create_run_spec_v2(
            task,
            ModelProfile(provider="mock", model="test"),
            engine=EngineName.GODOT,
            engine_version="4.4",
            profile=HarnessProfile.PROJECT,
            game_task=_game_task(),
            engine_environment=EngineEnvironment(
                engine_version="4.3",
                target_platform="linux",
            ),
        )
