from __future__ import annotations

import json
from pathlib import Path

from gameforge.benchmarking.heterogeneous import TaskCatalog
from gameforge.benchmarking.oracle import DeterministicOracleModel
from gameforge.harness.runtime import AgentDecision

ROOT = Path(__file__).resolve().parents[1]


def test_deterministic_control_uses_same_agent_decision_protocol() -> None:
    catalog = TaskCatalog.from_yaml(ROOT / "benchmarks" / "unity-heterogeneous-v1.yaml")
    task = catalog.task("gameplay-weapon-damage")
    model = DeterministicOracleModel(task)

    prompt = json.dumps({"observations": [{"status": "success"}]})
    repair = AgentDecision.from_json(model.complete(system="", prompt=prompt).text)
    compile_check = AgentDecision.from_json(model.complete(system="", prompt=prompt).text)
    finish = AgentDecision.from_json(model.complete(system="", prompt=prompt).text)

    assert repair.tool == "apply_code_patch"
    assert repair.arguments["expected"] == task.fixture.replacement
    assert compile_check.tool == "wait_for_compilation"
    assert finish.kind == "finish"
    assert finish.outcome == "success"
    assert "fixture" not in json.dumps(finish.model_dump(mode="json"))
