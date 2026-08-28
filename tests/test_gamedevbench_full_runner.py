from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments import run_unseen30_maturity
from experiments.run_gamedevbench_full_candidate import (
    _outer_timeout_record,
    _schedule,
    _update_progress,
)
from experiments.run_unseen30_maturity import _harness_attempt


def _write_receipt(directory: Path, ordinal: int, status: str = "PASS") -> None:
    path = directory / "receipts" / f"{ordinal:03d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ordinal": ordinal, "normalized_status": status}),
        encoding="utf-8",
    )


def test_parallel_progress_tracks_first_missing_ordinal(tmp_path: Path) -> None:
    _write_receipt(tmp_path, 2)
    _write_receipt(tmp_path, 4, "FAIL")

    _update_progress(tmp_path, total=4)

    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert progress["attempts_completed"] == 2
    assert progress["next_ordinal"] == 1
    assert progress["status_counts"] == {"FAIL": 1, "PASS": 1}

    _write_receipt(tmp_path, 1)
    _write_receipt(tmp_path, 3)
    _update_progress(tmp_path, total=4)

    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert progress["attempts_completed"] == 4
    assert progress["next_ordinal"] is None


def test_schedule_supports_repeated_preregistered_subset() -> None:
    tasks = [
        {"task_id": "task_0001", "name": "One", "stratum": "2d"},
        {"task_id": "task_0002", "name": "Two", "stratum": "ui"},
    ]

    schedule = _schedule(tasks, "broker-validation", repetitions=2)

    assert schedule["attempt_count"] == 4
    assert [attempt["task_id"] for attempt in schedule["attempts"]] == [
        "task_0001",
        "task_0002",
        "task_0001",
        "task_0002",
    ]
    assert [attempt["repetition"] for attempt in schedule["attempts"]] == [1, 1, 2, 2]
    assert len({attempt["attempt_id"] for attempt in schedule["attempts"]}) == 4


def test_outer_timeout_receipt_uses_immutable_log_snapshot(tmp_path: Path) -> None:
    attempt = {
        "attempt_id": "full332-task_0001-test",
        "task_id": "task_0001",
        "stratum": "2d",
    }
    log = tmp_path / "attempts" / attempt["attempt_id"] / "runner.log"
    log.parent.mkdir(parents=True)
    log.write_text("timeout boundary\n", encoding="utf-8")

    record, exit_code, source, retries = _outer_timeout_record(
        tmp_path,
        attempt,
        RuntimeError("attempt exceeded outer timeout: runner.log"),
    )
    log.write_text("later child output\n", encoding="utf-8")

    assert record["status"] == "BLOCKED"
    assert record["model_turns"] is None
    assert exit_code == 124
    assert retries == []
    assert source.name == "runner-timeout-snapshot.log"
    assert source.read_text(encoding="utf-8") == "timeout boundary\n"


def test_harness_attempt_classifies_missing_progress_as_zero_turn_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    experiment = root / "experiment"
    benchmark = tmp_path / "benchmark"
    manifest = tmp_path / "task.json"
    agent = tmp_path / "codex"
    root.mkdir()
    experiment.mkdir()
    benchmark.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    agent.write_text("", encoding="utf-8")

    def fail_before_progress(
        command: list[str],
        cwd: Path,
        log_path: Path,
        **_: object,
    ) -> int:
        assert command
        assert cwd == root
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("checkout commit found unknown\n", encoding="utf-8")
        return 2

    monkeypatch.setattr(run_unseen30_maturity, "_run_logged", fail_before_progress)
    record, exit_code, source = _harness_attempt(
        uv="uv",
        root=root,
        benchmark_root=benchmark,
        experiment=experiment,
        manifest_path=manifest,
        agent_executable=agent,
        attempt={
            "attempt_id": "task-1",
            "condition": "minimal-open-v1",
            "task_id": "task_0001",
        },
        model="gpt-test",
        effort="medium",
    )

    assert exit_code == 2
    assert source.name == "runner.log"
    assert record["status"] == "INFRASTRUCTURE_ERROR"
    assert record["model_turns"] == 0
    assert "checkout commit found unknown" in record["failure_detail"]
