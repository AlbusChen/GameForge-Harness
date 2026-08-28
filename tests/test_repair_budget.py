import pytest

from gameforge.orchestrator.repair_loop import RepairBudget


def test_repair_budget_is_bounded() -> None:
    budget = RepairBudget(maximum=3)

    assert [budget.consume(), budget.consume(), budget.consume()] == [1, 2, 3]
    with pytest.raises(RuntimeError, match="exhausted"):
        budget.consume()
