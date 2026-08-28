from __future__ import annotations

import json
from dataclasses import dataclass, field

from gameforge.adapters.llm import ModelResponse, ModelUsage
from gameforge.benchmarking.heterogeneous import HeterogeneousTask


@dataclass
class DeterministicOracleModel:
    """Fixture-aware control adapter used only to validate the harness and task plumbing."""

    task: HeterogeneousTask
    calls: int = field(default=0, init=False)

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system
        self.calls += 1
        if self.calls == 1:
            decision: dict[str, object] = {
                "kind": "tool",
                "tool": "apply_code_patch",
                "arguments": {
                    "path": self.task.fixture.path,
                    "expected": self.task.fixture.replacement,
                    "replacement": self.task.fixture.expected,
                },
                "rationale": "Apply the fixture-known control repair through the public tool.",
                "evidence_sources": ["deterministic_control_fixture"],
            }
        elif self.calls == 2:
            decision = {
                "kind": "tool",
                "tool": "wait_for_compilation",
                "arguments": {},
                "rationale": "Verify compilation after the control repair.",
                "evidence_sources": ["apply_code_patch"],
            }
        else:
            try:
                observations = json.loads(prompt).get("observations", [])
                verification_passed = bool(observations) and observations[-1]["status"] == "success"
            except (json.JSONDecodeError, KeyError, TypeError):
                verification_passed = False
            decision = {
                "kind": "finish",
                "outcome": "success" if verification_passed else "blocked",
                "summary": (
                    "The deterministic control repair and compilation completed."
                    if verification_passed
                    else "The deterministic control compilation check failed."
                ),
                "rationale": (
                    "The typed repair and compilation tools returned successfully."
                    if verification_passed
                    else "The latest compilation observation is not successful."
                ),
                "evidence_sources": ["apply_code_patch", "wait_for_compilation"],
            }
        return ModelResponse(
            text=json.dumps(decision, sort_keys=True),
            usage=ModelUsage(0, 0, 0.0),
            provider="deterministic-control",
            model="fixture-repair-v1",
        )
