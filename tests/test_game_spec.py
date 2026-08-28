from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.schemas.game_spec import GameSpec

ROOT = Path(__file__).parents[1]


def test_arena_demo_is_valid() -> None:
    spec = GameSpec.from_yaml(ROOT / "specs" / "arena_demo.yaml")

    assert spec.game.id == "arena_demo"
    assert spec.enemies[0].initial_count == 5
    assert len(spec.acceptance) == 6
    assert spec.acceptance[-1].assertions == (
        "game_status_equals_playing",
        "player_health_equals_100",
        "enemies_alive_equals_5",
    )


def test_unknown_fields_are_rejected() -> None:
    payload = GameSpec.from_yaml(ROOT / "specs" / "arena_demo.yaml").model_dump(by_alias=True)
    payload["game"]["unsupported"] = True

    with pytest.raises(ValidationError, match="unsupported"):
        GameSpec.model_validate(payload)
