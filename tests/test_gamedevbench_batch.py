from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.benchmarking.gamedevbench_batch import (
    _count_engine_crash_attempts,
    _count_feedback_attempts,
    _godot_infrastructure_failure_category,
    _has_persistent_engine_crash,
    _mcnemar_exact,
    _only_inconclusive_startup_timeouts,
    _retained_run_directory,
    _retryable_pre_request,
    _summarize,
    _validated_progress_records,
    _wilson_interval,
    compare_gamedevbench_paired_results,
)


def test_wilson_interval_is_bounded_and_contains_observed_rate() -> None:
    lower, upper = _wilson_interval(7, 10)

    assert lower is not None
    assert upper is not None
    assert 0 <= lower < 0.7 < upper <= 1
    assert _wilson_interval(0, 0) == (None, None)


def test_exact_mcnemar_uses_only_discordant_pairs() -> None:
    assert _mcnemar_exact(0, 0) == 1.0
    assert _mcnemar_exact(5, 0) == pytest.approx(0.0625)
    assert _mcnemar_exact(2, 2) == 1.0


def test_batch_summary_excludes_infrastructure_errors_from_pass_rate() -> None:
    records = [
        {
            "task_id": "task_0001",
            "stratum": "2d",
            "status": "PASS",
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "reasoning_output_tokens": 1,
            "cost_usd": 0.1,
            "tool_calls": 1,
            "duration_seconds": 2.0,
        },
        {
            "task_id": "task_0002",
            "stratum": "ui",
            "status": "FAIL",
            "input_tokens": 20,
            "cached_input_tokens": 4,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
            "cost_usd": 0.2,
            "tool_calls": 2,
            "duration_seconds": 3.0,
        },
        {
            "task_id": "task_0003",
            "stratum": "3d",
            "status": "INFRASTRUCTURE_ERROR",
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "cost_usd": 0.0,
            "tool_calls": 0,
            "duration_seconds": 1.0,
        },
    ]

    summary = _summarize(records, tasks_total=3)

    assert summary["evaluable_tasks"] == 2
    assert summary["passes"] == 1
    assert summary["pass_rate"] == pytest.approx(0.5)
    assert summary["input_tokens"] == 30
    assert summary["tool_calls"] == 3
    assert summary["status_counts"] == {
        "FAIL": 1,
        "INFRASTRUCTURE_ERROR": 1,
        "PASS": 1,
    }


def test_batch_progress_rejects_duplicates_and_stratum_changes() -> None:
    manifest = {
        "id": "pilot",
        "tasks": [{"task_id": "task_0001", "stratum": "2d"}],
    }
    valid = {
        "schema_version": 1,
        "manifest_id": "pilot",
        "tasks": [{"task_id": "task_0001", "stratum": "2d", "status": "PASS"}],
    }

    assert _validated_progress_records(valid, manifest) == valid["tasks"]
    with pytest.raises(ValueError, match="invalid task entry"):
        _validated_progress_records(
            {**valid, "tasks": [*valid["tasks"], *valid["tasks"]]}, manifest
        )
    with pytest.raises(ValueError, match="invalid task entry"):
        _validated_progress_records(
            {**valid, "tasks": [{**valid["tasks"][0], "stratum": "ui"}]},
            manifest,
        )


def test_paired_comparison_excludes_missing_usage_from_efficiency(
    tmp_path: Path,
) -> None:
    manifest = {
        "id": "pilot",
        "benchmark_commit": "fixed",
        "tasks": [
            {"task_id": "task_0001", "stratum": "2d"},
            {"task_id": "task_0002", "stratum": "ui"},
        ],
    }
    official = {
        "configuration": {"model": "model"},
        "tasks": [
            {
                "task_name": "task_0001",
                "success": True,
                "solver_success": True,
                "input_tokens": 100,
                "output_tokens": 10,
                "solver_duration": 2,
                "cost_usd": 0.1,
            },
            {
                "task_name": "task_0002",
                "success": False,
                "solver_success": False,
                "input_tokens": 200,
                "output_tokens": 20,
                "solver_duration": 3,
                "cost_usd": 0.2,
            },
        ],
    }
    harness = {
        "tasks": [
            {
                "task_id": "task_0001",
                "status": "PASS",
                "official_gate": "PASS",
                "preservation_gate": "PASS",
                "input_tokens": 50,
                "cached_input_tokens": 10,
                "output_tokens": 5,
                "duration_seconds": 1,
                "cost_usd": 0.05,
            },
            {
                "task_id": "task_0002",
                "status": "BLOCKED",
                "official_gate": "NOT_RUN",
                "preservation_gate": "NOT_RUN",
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "duration_seconds": 900,
                "cost_usd": 0,
                "failure_category": "ModelError",
            },
        ]
    }
    paths = {
        "manifest": tmp_path / "manifest.json",
        "official": tmp_path / "official.json",
        "harness": tmp_path / "harness.json",
        "output": tmp_path / "comparison.json",
    }
    for name, payload in (
        ("manifest", manifest),
        ("official", official),
        ("harness", harness),
    ):
        paths[name].write_text(json.dumps(payload), encoding="utf-8")

    result = compare_gamedevbench_paired_results(
        manifest_path=paths["manifest"],
        official_results_path=paths["official"],
        harness_progress_path=paths["harness"],
        output_path=paths["output"],
    )

    assert result["paired_tasks_evaluable"] == 2
    assert result["usage_complete_pairs"] == 1
    assert result["usage_missing_pairs"] == 1
    assert result["efficiency_official_total_tokens"] == 110
    assert result["efficiency_harness_total_tokens"] == 55
    assert result["strata"]["2d"]["harness_pass_rate"] == 1.0  # type: ignore[index]


def test_retry_eligibility_requires_a_pre_request_visual_failure() -> None:
    eligible = {
        "status": "BLOCKED",
        "failure_category": "ModelError",
        "complexity_route": "visual",
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": 0,
        "duration_seconds": 0.5,
    }

    assert _retryable_pre_request(eligible)
    assert not _retryable_pre_request({**eligible, "input_tokens": 1})
    assert not _retryable_pre_request({**eligible, "complexity_route": "interactive"})
    assert not _retryable_pre_request({**eligible, "duration_seconds": 5.0})


def test_feedback_attempt_count_includes_direct_and_automatic_calls(
    tmp_path: Path,
) -> None:
    for index in range(3):
        (tmp_path / f"visual-feedback-{index + 1:02d}.json").write_text(
            "{}", encoding="utf-8"
        )
    (tmp_path / "visual-evidence.json").write_text("{}", encoding="utf-8")

    assert _count_feedback_attempts(tmp_path) == 3


def test_engine_crash_classification_distinguishes_recovered_and_persistent(
    tmp_path: Path,
) -> None:
    recovered = tmp_path / "official.engine-crash-attempt-1.log"
    recovered.write_text("first attempt only", encoding="utf-8")

    assert _count_engine_crash_attempts(tmp_path) == 1
    assert not _has_persistent_engine_crash(None, tmp_path)

    (tmp_path / "agent-result.json").write_text(
        json.dumps({"error": "Godot engine crashed after 2 attempts"}),
        encoding="utf-8",
    )
    assert _has_persistent_engine_crash(None, tmp_path)
    assert _has_persistent_engine_crash("evaluator_GodotEngineCrash", tmp_path)


def test_godot_environment_failure_is_excluded_as_infrastructure(
    tmp_path: Path,
) -> None:
    assert (
        _godot_infrastructure_failure_category("GodotEnvironmentError", tmp_path)
        == "GodotEnvironmentError"
    )
    (tmp_path / "failure.json").write_text(
        json.dumps(
            {"detail": "Godot host environment rejected a required data path"}
        ),
        encoding="utf-8",
    )
    assert (
        _godot_infrastructure_failure_category(None, tmp_path)
        == "GodotEnvironmentError"
    )


def test_persistent_engine_crash_in_events_is_excluded_as_infrastructure(
    tmp_path: Path,
) -> None:
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "kind": "capability_failed",
                "payload": {
                    "type": "GodotEngineCrash",
                    "error": "Godot engine crashed after 2 attempts",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert _has_persistent_engine_crash(None, tmp_path)
    assert (
        _godot_infrastructure_failure_category("evaluation", tmp_path)
        == "GodotEngineCrash"
    )


def test_retained_run_directory_prefers_record_then_latest_candidate(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs" / "gamedevbench-harness"
    older = run_root / "20260101T000000Z-gamedevbench-task_0001"
    newer = run_root / "20260102T000000Z-gamedevbench-task_0001"
    older.mkdir(parents=True)
    newer.mkdir()

    assert _retained_run_directory(tmp_path, "task_0001", {}) == newer
    assert _retained_run_directory(
        tmp_path, "task_0001", {"run_directory": str(older)}
    ) == older


def test_startup_timeout_adjudication_requires_clean_import_evidence(
    tmp_path: Path,
) -> None:
    payload = {
        "trace": [
            {
                "event": "automatic_validation",
                "turn": 1,
                "result": {
                    "import_return_code": 0,
                    "startup_return_code": 124,
                    "diagnostics": [],
                },
            },
            {
                "event": "automatic_validation",
                "turn": 2,
                "result": {
                    "import_return_code": 0,
                    "startup_return_code": 124,
                    "diagnostics": [],
                },
            },
        ]
    }
    (tmp_path / "agent-result.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    assert _only_inconclusive_startup_timeouts(tmp_path)
    payload["trace"][1]["result"]["diagnostics"] = ["SCRIPT ERROR"]  # type: ignore[index]
    (tmp_path / "agent-result.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    assert not _only_inconclusive_startup_timeouts(tmp_path)
