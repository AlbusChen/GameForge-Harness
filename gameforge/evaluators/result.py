from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gameforge.schemas.evidence import Evidence


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: GateStatus
    evidence: tuple[Evidence, ...] = ()
    detail: str = ""
