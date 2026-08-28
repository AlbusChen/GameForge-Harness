import pytest

from gameforge.benchmark import aggregate_records, percentile_nearest_rank


def test_nearest_rank_p95() -> None:
    assert percentile_nearest_rank([float(value) for value in range(1, 21)], 0.95) == 19


def test_aggregate_includes_failures() -> None:
    passing = {
        "status": "PASS",
        "spec_validation": "PASS",
        "retest_compile": "PASS",
        "structure_tests": "PASS",
        "play_mode_tests": "PASS",
        "build": "PASS",
        "smoke_test": "PASS",
        "duration_seconds": 20,
        "repair_attempts": 1,
        "api_cost_usd": 0,
        "git_clean_after_run": True,
        "source_restored": True,
    }
    failing = {
        "status": "FAIL",
        "spec_validation": "PASS",
        "retest_compile": "NOT_RUN",
        "structure_tests": "NOT_RUN",
        "play_mode_tests": "NOT_RUN",
        "build": "NOT_RUN",
        "smoke_test": "NOT_RUN",
        "duration_seconds": 5,
        "repair_attempts": 0,
        "api_cost_usd": 0,
        "git_clean_after_run": True,
        "source_restored": True,
        "failure_category": "compilation",
    }

    metrics = aggregate_records([passing, failing])

    assert metrics["end_to_end_success_rate"] == 0.5
    assert metrics["compile_success_rate"] == 0.5
    assert metrics["manual_takeover_rate"] == 0.5
    assert metrics["failure_categories"] == {"compilation": 1}


def test_aggregate_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        aggregate_records([])
