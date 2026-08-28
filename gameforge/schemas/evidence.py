from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceKind(StrEnum):
    SPEC = "spec"
    CONSOLE = "console"
    STRUCTURE = "structure"
    GAME_STATE = "game_state"
    SCREENSHOT = "screenshot"
    TEST_RESULT = "test_result"
    BUILD = "build"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind
    source: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]
    artifact_path: str | None = None
