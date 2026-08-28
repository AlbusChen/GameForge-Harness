from gameforge.evaluators.result import GateResult, GateStatus


def not_run() -> GateResult:
    return GateResult("gameplay", GateStatus.NOT_RUN, detail="Play Mode tests have not run")
