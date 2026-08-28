import json
from pathlib import Path

from gameforge.orchestrator.executor import run_mock

ROOT = Path(__file__).parents[1]


def test_mock_run_never_claims_engine_success(tmp_path: Path) -> None:
    result = run_mock(
        ROOT / "specs" / "arena_demo.yaml",
        ROOT / "unity" / "ArenaTemplate",
        tmp_path,
    )

    summary = json.loads((result.run_directory / "summary.json").read_text())
    assert summary["status"] == "MOCK_VALIDATED"
    assert summary["unity_compile"] == "NOT_RUN"
    assert summary["build"] == "NOT_RUN"
    assert (result.run_directory / "trace.jsonl").is_file()
    assert (result.run_directory / "report.html").is_file()
