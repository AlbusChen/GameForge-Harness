#!/usr/bin/env python3
"""Replay task_0016's exact typed mutation with canonical header expressions."""

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
DEFAULT_RUN = ROOT / "runs/gamedevbench-harness/20260814T084939Z-gamedevbench-task_0016"


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
    parser.add_argument("--source-run", type=Path, default=DEFAULT_RUN)
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "runs/experiments"
            / "gamedevbench-v24-header-expression-replay-task0016/result.json"
        ),
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    godot = arguments.godot.resolve(strict=True)
    source_run = arguments.source_run.resolve(strict=True)
    agent_result = json.loads(
        (source_run / "agent-result.json").read_text(encoding="utf-8")
    )
    mutations = [
        item["decision"]["arguments"]
        for item in agent_result["trace"]
        if item.get("event") == "model_program_decision"
        and item.get("decision", {}).get("capability") == "mutate_resource_objects"
    ]
    if len(mutations) != 1:
        raise RuntimeError("task_0016 source trace must contain one typed resource mutation")
    manifest = json.loads((source_run / "output-manifest.json").read_text(encoding="utf-8"))
    editable = tuple(str(path) for path in manifest["writable_paths"])
    source = GameDevBenchSource(benchmark_root, "task_0016", editable)
    with tempfile.TemporaryDirectory(prefix="gameforge-task0016-header-replay-") as temporary:
        temporary_path = Path(temporary)
        workspace = temporary_path / "project"
        shutil.copytree(benchmark_root / "tasks/task_0016", workspace)
        run_directory = temporary_path / "run"
        adapter = GodotHarnessEngineAdapter(
            project=workspace,
            run_directory=run_directory,
            editable_paths=editable,
            godot=godot,
        )
        adapter.bind_project_scope(ProjectAccessScope(editable))
        mutation_result = adapter.invoke("mutate_resource_objects", mutations[0])
        scene = (workspace / "scenes/cultist_prefab.tscn").read_text(encoding="utf-8")
        matching_headers = [
            line
            for line in scene.splitlines()
            if line.startswith("[node ") and "FlashlightPoint" in line
        ]
        if len(matching_headers) != 1:
            raise RuntimeError(
                "typed mutation did not render one FlashlightPoint header: "
                f"{mutation_result}; tail={scene[-1000:]!r}"
            )
        header = matching_headers[0]
        gate = GameDevBenchOfficialEvaluator(
            source=source,
            godot=godot,
            run_directory=run_directory,
        )._official(workspace)
        payload = {
            "schema_version": 1,
            "task_id": "task_0016",
            "source_run": str(source_run),
            "mutation_result": mutation_result,
            "flashlight_header": header,
            "instance_expression_unquoted": (
                'instance=ExtResource("1_sd2cr")' in header
                and 'instance="ExtResource' not in header
            ),
            "godot": str(godot),
            "official_gate": gate.status.value,
            "official_detail": gate.detail,
        }
    output = arguments.output.resolve()
    _write_json(output, payload)
    print(output)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["instance_expression_unquoted"] and gate.status.value == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
