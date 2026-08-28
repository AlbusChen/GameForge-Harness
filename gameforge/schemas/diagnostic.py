from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FailureCategory(StrEnum):
    COMPILATION = "compilation"
    STRUCTURE_TEST = "structure_test"
    PLAY_MODE_TEST = "play_mode_test"
    BUILD = "build"
    SMOKE_TEST = "smoke_test"
    TIMEOUT = "timeout"
    ENVIRONMENT = "environment"
    UNKNOWN = "unknown"


class SourceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)


class DiagnosticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: FailureCategory
    code: str
    message: str
    evidence_source: str
    location: SourceLocation | None = None


class TextReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["apply_code_patch"] = "apply_code_patch"
    path: str
    expected: str = Field(min_length=1)
    replacement: str
    rationale: str = Field(min_length=1)
    evidence_sources: tuple[str, ...] = Field(min_length=1)


class DiagnosticReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: tuple[DiagnosticFinding, ...]
    proposed_repair: TextReplacement | None = None


class PatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    replacements: int
    bytes_before: int
    bytes_after: int
    sha256_before: str
    sha256_after: str
