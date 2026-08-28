from gameforge.evaluators.result import GateResult, GateStatus


def not_run() -> GateResult:
    return GateResult("visual", GateStatus.NOT_RUN, detail="No screenshot has been captured")
