from __future__ import annotations

from pathlib import Path

import pytest

from gameforge.benchmarking.project_tasks import (
    _write_report,
    run_project_task_benchmark,
)


def test_model_project_benchmark_rejects_missing_profile_before_creating_runs(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
name: project-reference
version: 1.0.0
engine: unity
control_policy: scripted-control-v1
tasks:
  - id: change-hud
    source: change
    instruction: Change the HUD.
    editable_paths: [Assets/HUD.cs]
    acceptance: [file_exists:Assets/HUD.cs]
    engine_gates: [specification]
    decisions:
      - kind: finish
        outcome: blocked
        summary: fixture only
        rationale: fixture only
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-mock model profile"):
        run_project_task_benchmark(
            root=tmp_path,
            catalog_path=catalog,
            template_project=tmp_path / "project",
            adapter_mode="model",
        )

    assert not (tmp_path / "runs").exists()


def test_model_project_report_is_not_labeled_as_a_control(tmp_path: Path) -> None:
    report = tmp_path / "report.html"

    _write_report(
        report,
        {
            "control_result": False,
            "model_performance_claim": True,
            "tasks_completed": 0,
        },
        [],
    )

    content = report.read_text(encoding="utf-8")
    assert "Project task model report" in content
    assert "model-backed result" in content
    assert "deterministic scripted control" not in content
