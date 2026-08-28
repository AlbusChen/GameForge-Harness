from gameforge.evaluators.result import GateResult, GateStatus


def not_run() -> GateResult:
    return GateResult("build", GateStatus.NOT_RUN, detail="No macOS player has been built")
