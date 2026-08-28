from __future__ import annotations

import re
from pathlib import Path

from gameforge.schemas.diagnostic import (
    DiagnosticFinding,
    DiagnosticReport,
    FailureCategory,
    SourceLocation,
)
from gameforge.schemas.evidence import Evidence

_CSHARP_ERROR = re.compile(
    r"^(?P<path>Assets/[^\r\n(]+)"
    r"\((?P<line>\d+),(?P<column>\d+)\): "
    r"error (?P<code>CS\d+): (?P<message>.+)$",
    re.MULTILINE,
)


def diagnose_compiler_log(log_path: Path) -> DiagnosticReport:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    source = str(log_path.resolve())
    findings: list[DiagnosticFinding] = []
    seen: set[tuple[str, int, int, str, str]] = set()

    for match in _CSHARP_ERROR.finditer(text):
        identity = (
            match["path"],
            int(match["line"]),
            int(match["column"]),
            match["code"],
            match["message"].strip(),
        )
        if identity in seen:
            continue
        seen.add(identity)
        findings.append(
            DiagnosticFinding(
                category=FailureCategory.COMPILATION,
                code=match["code"],
                message=match["message"].strip(),
                evidence_source=source,
                location=SourceLocation(
                    path=match["path"],
                    line=int(match["line"]),
                    column=int(match["column"]),
                ),
            )
        )

    if not findings:
        findings.append(
            DiagnosticFinding(
                category=FailureCategory.UNKNOWN,
                code="NO_STRUCTURED_COMPILER_ERROR",
                message="Unity failed without a parseable C# compiler diagnostic.",
                evidence_source=source,
            )
        )

    compiler_count = sum(finding.category is FailureCategory.COMPILATION for finding in findings)
    summary = (
        f"Found {compiler_count} unique C# compiler error(s)."
        if compiler_count
        else "No structured C# compiler error was found."
    )
    return DiagnosticReport(summary=summary, findings=tuple(findings))


def require_evidence(diagnosis: DiagnosticReport, evidence: list[Evidence]) -> None:
    available = {item.source for item in evidence}
    referenced = {item.evidence_source for item in diagnosis.findings}
    if diagnosis.proposed_repair is not None:
        referenced.update(diagnosis.proposed_repair.evidence_sources)
    missing = referenced - available
    if missing:
        raise ValueError(f"diagnosis references missing evidence: {sorted(missing)}")
