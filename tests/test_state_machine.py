import pytest

from gameforge.orchestrator.state_machine import (
    InvalidTransitionError,
    RunState,
    StateMachine,
)


def test_valid_spec_path_reaches_plan() -> None:
    machine = StateMachine()
    for state in (
        RunState.VALIDATE_SPEC,
        RunState.CREATE_CHECKPOINT,
        RunState.INSPECT_PROJECT,
        RunState.PLAN,
    ):
        machine.transition(state)

    assert machine.state is RunState.PLAN


def test_compile_cannot_be_skipped() -> None:
    machine = StateMachine()

    with pytest.raises(InvalidTransitionError):
        machine.transition(RunState.PLAY_TEST)
