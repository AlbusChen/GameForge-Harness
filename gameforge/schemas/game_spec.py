from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gameforge.schemas.acceptance_spec import AcceptanceCondition


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Genre(StrEnum):
    TOP_DOWN_ARENA = "top_down_arena"


class BuildTarget(StrEnum):
    MACOS = "macos"


class GameDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    genre: Genre
    template: Literal["top_down_arena_v0"]
    build_target: BuildTarget


class MovementSpec(StrictModel):
    input: Literal["wasd"]
    speed: float = Field(gt=0, le=30)


class WeaponSpec(StrictModel):
    input: Literal["mouse_left"]
    damage: int = Field(gt=0, le=1000)
    fire_rate: float = Field(gt=0, le=30)
    range: float = Field(gt=0, le=100)


class PlayerSpec(StrictModel):
    max_health: int = Field(gt=0, le=10000)
    movement: MovementSpec
    weapon: WeaponSpec


class EnemySpec(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    behavior: Literal["chase_melee"]
    initial_count: int = Field(ge=1, le=100)
    health: int = Field(gt=0, le=10000)
    damage: int = Field(gt=0, le=10000)
    speed: float = Field(gt=0, le=30)


class RulesSpec(StrictModel):
    win_when: Literal["all_enemies_dead"]
    lose_when: Literal["player_health_zero"]
    restart_enabled: bool


class BudgetSpec(StrictModel):
    max_repair_loops: int = Field(ge=0, le=3)
    max_run_minutes: int = Field(ge=1, le=120)
    target_fps: int = Field(ge=30, le=240)


class GameSpec(StrictModel):
    version: Literal[0]
    game: GameDefinition
    player: PlayerSpec
    enemies: list[EnemySpec] = Field(min_length=1, max_length=8)
    rules: RulesSpec
    ui: list[Literal["health_bar", "enemies_remaining", "end_screen", "restart_button"]] = Field(
        min_length=1
    )
    budgets: BudgetSpec
    acceptance: list[AcceptanceCondition] = Field(min_length=1)

    @model_validator(mode="after")
    def ids_and_ui_must_be_unique(self) -> GameSpec:
        enemy_ids = [enemy.id for enemy in self.enemies]
        if len(enemy_ids) != len(set(enemy_ids)):
            raise ValueError("enemy ids must be unique")
        acceptance_ids = [condition.id for condition in self.acceptance]
        if len(acceptance_ids) != len(set(acceptance_ids)):
            raise ValueError("acceptance ids must be unique")
        if len(self.ui) != len(set(self.ui)):
            raise ValueError("ui entries must be unique")
        if self.rules.restart_enabled and "restart_button" not in self.ui:
            raise ValueError("restart_button is required when restart is enabled")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> GameSpec:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read GameSpec: {path}") from error
        except yaml.YAMLError as error:
            raise ValueError(f"invalid YAML in GameSpec: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("GameSpec root must be a mapping")
        return cls.model_validate(payload)
