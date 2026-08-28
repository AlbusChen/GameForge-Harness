from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gameforge.orchestrator.executor import run_baseline
from gameforge.orchestrator.repair_demo import run_repair_demo


@dataclass(frozen=True)
class BenchmarkResult:
    benchmark_id: str
    directory: Path
    summary: dict[str, object]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def percentile_nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def aggregate_records(records: list[dict[str, object]]) -> dict[str, object]:
    total = len(records)
    if total == 0:
        raise ValueError("benchmark requires at least one run record")

    def passed(key: str) -> int:
        return sum(record.get(key) == "PASS" for record in records)

    durations = [float(record.get("duration_seconds", 0.0)) for record in records]
    costs = [float(record.get("api_cost_usd", 0.0)) for record in records]
    repair_attempts = [int(record.get("repair_attempts", 0)) for record in records]
    end_to_end = sum(record.get("status") == "PASS" for record in records)
    preserved = sum(
        record.get("git_clean_after_run", True) and record.get("source_restored", True)
        for record in records
    )
    failures = Counter(
        str(record.get("failure_category", "none"))
        for record in records
        if record.get("status") != "PASS"
    )

    return {
        "runs_completed": total,
        "spec_validation_success_rate": round(passed("spec_validation") / total, 4),
        "compile_success_rate": round(
            max(passed("unity_compile"), passed("retest_compile")) / total,
            4,
        ),
        "structure_success_rate": round(passed("structure_tests") / total, 4),
        "play_mode_success_rate": round(passed("play_mode_tests") / total, 4),
        "end_to_end_success_rate": round(end_to_end / total, 4),
        "build_launch_success_rate": round(
            sum(
                record.get("build") == "PASS" and record.get("smoke_test") == "PASS"
                for record in records
            )
            / total,
            4,
        ),
        "average_duration_seconds": round(statistics.mean(durations), 3),
        "p95_duration_seconds": round(percentile_nearest_rank(durations, 0.95), 3),
        "median_repair_attempts": statistics.median(repair_attempts),
        "average_api_cost_usd": round(statistics.mean(costs), 8),
        "p95_api_cost_usd": round(percentile_nearest_rank(costs, 0.95), 8),
        "user_change_preservation_rate": round(preserved / total, 4),
        "manual_takeover_rate": round((total - end_to_end) / total, 4),
        "failure_categories": dict(sorted(failures.items())),
    }


def _failure_category(summary: dict[str, object]) -> str:
    detail = str(summary.get("failure", "")).lower()
    for category, markers in (
        ("environment", ("editor", "license", "permission", "project is missing")),
        ("timeout", ("timeout", "exceeded")),
        ("compilation", ("compile", "compiler", "cs0", "cs1", "cs2", "cs8")),
        ("test", ("test", "acceptance")),
        ("build", ("build",)),
        ("smoke", ("smoke", "launch")),
    ):
        if any(marker in detail for marker in markers):
            return category
    return "unknown"


def run_benchmark(
    root: Path,
    spec_path: Path,
    project_path: Path,
    runs_path: Path,
    *,
    repetitions: int = 20,
    mode: str = "repair-demo",
) -> BenchmarkResult:
    if not 1 <= repetitions <= 100:
        raise ValueError("benchmark repetitions must be between 1 and 100")
    if mode not in {"baseline", "repair-demo"}:
        raise ValueError(f"unsupported benchmark mode: {mode}")

    started_at = datetime.now(UTC)
    benchmark_id = started_at.strftime(f"%Y%m%dT%H%M%SZ-{mode}-{repetitions}")
    directory = runs_path / "benchmarks" / benchmark_id
    directory.mkdir(parents=True, exist_ok=False)
    records_path = directory / "runs.jsonl"
    records: list[dict[str, object]] = []

    for index in range(1, repetitions + 1):
        print(f"Benchmark run {index}/{repetitions} started ({mode})", flush=True)
        try:
            if mode == "repair-demo":
                result = run_repair_demo(root, spec_path, project_path, runs_path)
            else:
                result = run_baseline(root, spec_path, project_path, runs_path)
            record = {
                "index": index,
                "run_id": result.run_id,
                "run_directory": str(result.run_directory),
                **result.summary,
            }
            if record.get("status") != "PASS":
                record["failure_category"] = _failure_category(result.summary)
        except Exception as error:
            record = {
                "index": index,
                "run_id": None,
                "status": "FAIL",
                "failure": str(error),
                "failure_category": _failure_category({"failure": str(error)}),
                "spec_validation": "NOT_RUN",
                "unity_compile": "NOT_RUN",
                "retest_compile": "NOT_RUN",
                "structure_tests": "NOT_RUN",
                "play_mode_tests": "NOT_RUN",
                "build": "NOT_RUN",
                "smoke_test": "NOT_RUN",
                "repair_attempts": 0,
                "api_cost_usd": 0.0,
            }
        records.append(record)
        with records_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        print(
            f"Benchmark run {index}/{repetitions} completed: {record['status']}",
            flush=True,
        )

    ended_at = datetime.now(UTC)
    metrics = aggregate_records(records)
    summary: dict[str, object] = {
        "benchmark_id": benchmark_id,
        "mode": mode,
        "runs_requested": repetitions,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "wall_duration_seconds": round((ended_at - started_at).total_seconds(), 3),
        **metrics,
    }
    _write_json(directory / "summary.json", summary)
    _write_json(directory / "runs.json", records)
    _write_benchmark_html(directory / "report.html", summary, records)
    return BenchmarkResult(benchmark_id, directory, summary)


def _write_benchmark_html(
    destination: Path,
    summary: dict[str, object],
    records: list[dict[str, object]],
) -> None:
    import html

    metric_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    run_rows = "".join(
        "<tr>"
        f"<td>{record['index']}</td>"
        f"<td>{html.escape(str(record.get('run_id')))}</td>"
        f"<td>{html.escape(str(record.get('status')))}</td>"
        f"<td>{html.escape(str(record.get('duration_seconds', 'n/a')))}</td>"
        f"<td>{html.escape(str(record.get('repair_attempts', 0)))}</td>"
        f"<td>{html.escape(str(record.get('failure_category', 'none')))}</td>"
        "</tr>"
        for record in records
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GameForge Harness benchmark</title>
  <style>
    body {{
      font-family: system-ui, sans-serif; max-width: 1100px;
      margin: 2rem auto; padding: 0 1rem;
    }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
    th, td {{ border: 1px solid #ccd2dc; padding: .55rem; text-align: left; }}
    th {{ background: #f2f5f9; }}
  </style>
</head>
<body>
  <h1>Benchmark report</h1>
  <p>Every requested run is included. Failed runs are not filtered out.</p>
  <h2>Metrics</h2>
  <table>{metric_rows}</table>
  <h2>Runs</h2>
  <table>
    <thead><tr><th>#</th><th>Run</th><th>Status</th><th>Seconds</th><th>Repairs</th><th>Failure</th></tr></thead>
    <tbody>{run_rows}</tbody>
  </table>
</body>
</html>
"""
    destination.write_text(document, encoding="utf-8")
