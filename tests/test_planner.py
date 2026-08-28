from pathlib import Path

from gameforge.orchestrator.planner import create_deterministic_plan
from gameforge.schemas.game_spec import GameSpec

ROOT = Path(__file__).parents[1]


def test_plan_is_ordered_and_covers_acceptance() -> None:
    spec = GameSpec.from_yaml(ROOT / "specs" / "arena_demo.yaml")
    plan = create_deterministic_plan(spec)

    assert plan.tasks[0].id == "inspect_project"
    assert plan.tasks[-1].id == "write_report"
    assert plan.tasks[4].acceptance_ids == [item.id for item in spec.acceptance]
