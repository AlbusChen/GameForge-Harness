from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class InvalidTransitionError(RuntimeError):
    pass


class RunState(StrEnum):
    INIT = "INIT"
    VALIDATE_SPEC = "VALIDATE_SPEC"
    CREATE_CHECKPOINT = "CREATE_CHECKPOINT"
    INSPECT_PROJECT = "INSPECT_PROJECT"
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    COMPILE = "COMPILE"
    STRUCTURE_TEST = "STRUCTURE_TEST"
    PLAY_TEST = "PLAY_TEST"
    DIAGNOSE = "DIAGNOSE"
    REPAIR = "REPAIR"
    RETEST = "RETEST"
    BUILD = "BUILD"
    BUILD_SMOKE_TEST = "BUILD_SMOKE_TEST"
    REPORT = "REPORT"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.INIT: {RunState.VALIDATE_SPEC, RunState.FAILED},
    RunState.VALIDATE_SPEC: {RunState.CREATE_CHECKPOINT, RunState.FAILED},
    RunState.CREATE_CHECKPOINT: {RunState.INSPECT_PROJECT, RunState.FAILED},
    RunState.INSPECT_PROJECT: {RunState.PLAN, RunState.FAILED},
    RunState.PLAN: {RunState.IMPLEMENT, RunState.REPORT, RunState.FAILED},
    RunState.IMPLEMENT: {RunState.COMPILE, RunState.FAILED},
    RunState.COMPILE: {RunState.STRUCTURE_TEST, RunState.DIAGNOSE, RunState.FAILED},
    RunState.STRUCTURE_TEST: {RunState.PLAY_TEST, RunState.DIAGNOSE, RunState.FAILED},
    RunState.PLAY_TEST: {RunState.BUILD, RunState.DIAGNOSE, RunState.FAILED},
    RunState.DIAGNOSE: {RunState.REPAIR, RunState.REPORT, RunState.FAILED},
    RunState.REPAIR: {RunState.RETEST, RunState.FAILED},
    RunState.RETEST: {RunState.BUILD, RunState.DIAGNOSE, RunState.FAILED},
    RunState.BUILD: {RunState.BUILD_SMOKE_TEST, RunState.DIAGNOSE, RunState.FAILED},
    RunState.BUILD_SMOKE_TEST: {RunState.REPORT, RunState.DIAGNOSE, RunState.FAILED},
    RunState.REPORT: {RunState.COMPLETE, RunState.FAILED},
    RunState.COMPLETE: set(),
    RunState.FAILED: {RunState.REPORT},
}


class StateMachine:
    def __init__(
        self,
        on_transition: Callable[[RunState, RunState, str | None], None] | None = None,
    ) -> None:
        self.state = RunState.INIT
        self._on_transition = on_transition

    def transition(self, target: RunState, reason: str | None = None) -> None:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransitionError(f"cannot transition from {self.state} to {target}")
        previous = self.state
        self.state = target
        if self._on_transition:
            self._on_transition(previous, target, reason)
