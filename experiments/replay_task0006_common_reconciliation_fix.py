#!/usr/bin/env python3
"""Replay the v21p4 raw-write failure through the common reconciliation boundary."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from experiments.run_unseen30_maturity import _sha256_file, _write_json
from gameforge.benchmarking.gamedevbench import (
    GameDevBenchOfficialEvaluator,
    GameDevBenchSource,
    GodotHarnessEngineAdapter,
)
from gameforge.harness.project_scope import ProjectAccessScope

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    ROOT / "runs/gamedevbench-workspaces/20260814T081943Z-gamedevbench-task_0006"
)
EDITABLE = (
    "project.godot",
    "scenes/battle_unit/ai/unit_ai.gd",
    "scenes/battle_unit/battle_unit.gd",
    "scenes/battle_unit/battle_unit.tscn",
    "scenes/main.tscn",
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
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "runs/experiments"
            / "gamedevbench-v22-common-reconciliation-replay-task0006/result.json"
        ),
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    benchmark_root = arguments.benchmark_root.resolve(strict=True)
    godot = arguments.godot.resolve(strict=True)
    source_workspace = arguments.source.resolve(strict=True)
    source_scene = source_workspace / "scenes/battle_unit/battle_unit.tscn"
    source_header = next(
        line
        for line in source_scene.read_text(encoding="utf-8").splitlines()
        if line.startswith('[node name="UnitAI"')
    )
    if "node_paths=PackedStringArray(" in source_header:
        raise RuntimeError("replay source no longer represents the v21p4 raw-write failure")

    source = GameDevBenchSource(benchmark_root, "task_0006", EDITABLE)
    with tempfile.TemporaryDirectory(prefix="gameforge-task0006-common-replay-") as temporary:
        temporary_path = Path(temporary)
        workspace = temporary_path / "project"
        shutil.copytree(source_workspace, workspace)
        run_directory = temporary_path / "run"
        adapter = GodotHarnessEngineAdapter(
            project=workspace,
            run_directory=run_directory,
            editable_paths=EDITABLE,
            godot=godot,
        )
        adapter.bind_project_scope(ProjectAccessScope(EDITABLE))
        reconciliation = adapter.reconcile_project_changes(
            ("scenes/battle_unit/battle_unit.tscn",)
        )
        reconciled_scene = workspace / "scenes/battle_unit/battle_unit.tscn"
        reconciled_header = next(
            line
            for line in reconciled_scene.read_text(encoding="utf-8").splitlines()
            if line.startswith('[node name="UnitAI"')
        )
        gate = GameDevBenchOfficialEvaluator(
            source=source,
            godot=godot,
            run_directory=run_directory,
        )._official(workspace)
        payload = {
            "schema_version": 1,
            "task_id": "task_0006",
            "source_workspace": str(source_workspace),
            "source_scene_sha256": _sha256_file(source_scene),
            "source_unit_ai_header": source_header,
            "reconciliation": reconciliation,
            "reconciled_unit_ai_header": reconciled_header,
            "node_paths_metadata_present": (
                "node_paths=PackedStringArray(" in reconciled_header
                and '"debug_label"' in reconciled_header
                and '"actor"' in reconciled_header
            ),
            "godot": str(godot),
            "official_gate": gate.status.value,
            "official_detail": gate.detail,
        }
    _write_json(arguments.output.resolve(), payload)
    print(arguments.output.resolve())
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["node_paths_metadata_present"] and gate.status.value == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
