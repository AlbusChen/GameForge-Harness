from gameforge.evaluators.result import GateResult, GateStatus


def not_run() -> GateResult:
    return GateResult("compilation", GateStatus.NOT_RUN, detail="Unity has not been invoked")
