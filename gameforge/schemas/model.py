from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanningDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["mock", "plan", "repair"]
    decision: Literal["use_deterministic_plan", "propose_repair", "no_safe_repair"]
    rationale: str = Field(min_length=1)
    evidence_sources: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, text: str) -> PlanningDecision:
        return cls.model_validate(json.loads(text))
