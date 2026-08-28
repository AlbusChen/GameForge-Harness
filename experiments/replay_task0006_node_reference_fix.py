#!/usr/bin/env python3
"""Replay the exact v20 structured mutation and run task_0006's Official evaluator."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from experiments.run_unseen30_maturity import _write_json
from gameforge.benchmarking.gamedevbench import (
    GameDevBenchOfficialEvaluator,
    GameDevBenchSource,
    GodotHarnessEngineAdapter,
)
from gameforge.harness.project_scope import ProjectAccessScope

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACE = (
    ROOT / "runs/gamedevbench-harness/20260814T075827Z-gamedevbench-task_0006/agent-result.json"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("/private/tmp/gamedevbench-e3868-pinned"),
    )
    parser.add_argument(
        "--godot",
        type=Path,
        default=Path("/private/tmp/godot-4.4.1-bin/godot"),
    )
    parser.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "runs/experiments"
            / "gamedevbench-v21-node-reference-deterministic-replay-task0006/result.json"
        ),
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    godot = arguments.godot.resolve(strict=True)
    trace_path = arguments.trace.resolve(strict=True)
    trace = json.loads(trace_path.read_text(encoding="utf-8"))["trace"]
    matching = [
        item["decision"]["arguments"]
        for item in trace
        if item.get("event") == "model_program_decision"
        and item.get("decision", {}).get("capability") == "mutate_resource_objects"
    ]
    if len(matching) != 1:
        raise RuntimeError("v20 task_0006 trace must contain one structured mutation")
    mutation = matching[0]
    editable = (
        "project.godot",
        "scenes/battle_unit/ai/unit_ai.gd",
        "scenes/battle_unit/battle_unit.gd",
        "scenes/battle_unit/battle_unit.tscn",
        "scenes/main.tscn",
    )
    source = GameDevBenchSource(benchmark_root, "task_0006", editable)
    with tempfile.TemporaryDirectory(prefix="gameforge-task0006-replay-") as temporary:
        temporary_path = Path(temporary)
        workspace = temporary_path / "project"
        shutil.copytree(benchmark_root / "tasks/task_0006", workspace)
        run_directory = temporary_path / "run"
        adapter = GodotHarnessEngineAdapter(
            project=workspace,
            run_directory=run_directory,
            editable_paths=editable,
            godot=godot,
        )
        adapter.bind_project_scope(ProjectAccessScope(editable))
        mutation_result = adapter.invoke("mutate_resource_objects", mutation)
        scene = (workspace / "scenes/battle_unit/battle_unit.tscn").read_text(encoding="utf-8")
        unit_ai_header = next(
            line for line in scene.splitlines() if line.startswith('[node name="UnitAI"')
        )
        gate = GameDevBenchOfficialEvaluator(
            source=source,
            godot=godot,
            run_directory=run_directory,
        )._official(workspace)
        payload = {
            "schema_version": 1,
            "task_id": "task_0006",
            "source_trace": str(trace_path),
            "mutation_result": mutation_result,
            "unit_ai_header": unit_ai_header,
            "node_paths_metadata_present": (
                "node_paths=PackedStringArray(" in unit_ai_header
                and '"debug_label"' in unit_ai_header
                and '"actor"' in unit_ai_header
            ),
            "official_gate": gate.status.value,
            "official_detail": gate.detail,
        }
    _write_json(arguments.output.resolve(), payload)
    print(arguments.output.resolve())
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["node_paths_metadata_present"] and gate.status.value == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
