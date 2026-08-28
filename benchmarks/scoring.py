from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class BenchmarkRun:
    acceptance_passed: bool
    build_launched: bool
    repair_loops: int
    duration_seconds: float
    cost_usd: float


def summarize(runs: list[BenchmarkRun]) -> dict[str, float]:
    if not runs:
        raise ValueError("at least one benchmark run is required")
    count = len(runs)
    return {
        "run_count": float(count),
        "acceptance_success_rate": sum(run.acceptance_passed for run in runs) / count,
        "build_launch_success_rate": sum(run.build_launched for run in runs) / count,
        "median_repair_loops": float(median(run.repair_loops for run in runs)),
        "average_duration_seconds": sum(run.duration_seconds for run in runs) / count,
        "average_cost_usd": sum(run.cost_usd for run in runs) / count,
    }
