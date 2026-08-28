from pathlib import Path

from gameforge.orchestrator.diagnostics import diagnose_compiler_log
from gameforge.schemas.diagnostic import FailureCategory


def test_compiler_log_deduplicates_unity_errors(tmp_path: Path) -> None:
    message = (
        "Assets/Scripts/GameStatus.cs(9,37): error CS0103: "
        "The name 'MissingSymbol' does not exist in the current context\n"
    )
    log = tmp_path / "compile.log"
    log.write_text(message + message, encoding="utf-8")

    report = diagnose_compiler_log(log)

    assert report.summary == "Found 1 unique C# compiler error(s)."
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.category is FailureCategory.COMPILATION
    assert finding.code == "CS0103"
    assert finding.location is not None
    assert finding.location.path == "Assets/Scripts/GameStatus.cs"
    assert finding.location.line == 9


def test_unstructured_failure_is_reported_truthfully(tmp_path: Path) -> None:
    log = tmp_path / "compile.log"
    log.write_text("Unity stopped unexpectedly.\n", encoding="utf-8")

    report = diagnose_compiler_log(log)

    assert report.findings[0].category is FailureCategory.UNKNOWN
    assert report.findings[0].code == "NO_STRUCTURED_COMPILER_ERROR"
