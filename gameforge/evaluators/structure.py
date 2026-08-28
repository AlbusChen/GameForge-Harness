from gameforge.evaluators.result import GateResult, GateStatus


def not_run() -> GateResult:
    return GateResult("structure", GateStatus.NOT_RUN, detail="Edit Mode tests have not run")
