#!/usr/bin/env python3
"""Build the frozen single-condition Unity20 game showcase from the authored Godot genres."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("benchmarks/godot-open-game-creation20-v1.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/unity-open-game-creation20-v1.json"),
    )
    return parser.parse_args()


def _unity_brief(brief: str) -> str:
    replacements = (
        ("Use the available Godot 4.4.1 engine", "Use the available Unity 6000.3.20f1 engine"),
        ("Use Godot 4.4.1", "Use Unity 6000.3.20f1"),
    )
    transformed = brief
    for before, after in replacements:
        transformed = transformed.replace(before, after)
    if "Godot" in transformed:
        raise ValueError(f"untranslated engine name in brief: {transformed}")
    return transformed


def main() -> int:
    arguments = _arguments()
    source_path = arguments.source.resolve(strict=True)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    tasks = [
        {
            "task_id": task["task_id"],
            "genre": task["genre"],
            "brief": _unity_brief(task["brief"]),
        }
        for task in source["tasks"]
    ]
    if len(tasks) != 20 or len({task["task_id"] for task in tasks}) != 20:
        raise ValueError("source must contain exactly twenty unique game briefs")
    manifest = {
        "schema_version": 1,
        "id": "unity-open-game-creation20-v1",
        "purpose": (
            "A single-condition Unity showcase of the same twenty authored mechanic and "
            "presentation families used by the Godot open-creation suite. It demonstrates "
            "breadth and produces inspectable games; it is not an execution-profile comparison "
            "or an estimate of a natural request distribution."
        ),
        "design": {
            "type": "single-condition showcase",
            "frozen_before_model_execution": True,
            "task_count": 20,
            "condition": "unified-game-workspace-native-open",
            "repetitions_per_task": 1,
            "interpretation": (
                "Report engine/solver/infrastructure outcomes separately, then summarize "
                "playable breadth and select representative artifacts with disclosed criteria."
            ),
        },
        "execution": {
            "harness": "unified-game-workspace-harness",
            "command": "run-game-workspace",
            "engine_selection": "auto",
            "solver_backend": "codex-subscription",
            "execution_profile": "native-open",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "solver_timeout_seconds": 1200,
            "parallelism": 1,
            "engine": "Unity",
            "engine_version": "6000.3.20f1",
            "target": "desktop 2D",
            "starting_workspace": "minimal empty Unity project",
            "network": "model-selected under native-open",
            "intermediate_harness_validation": False,
            "automatic_repair": False,
        },
        "evaluation": {
            "solver_blind": True,
            "hard_gate": (
                "The delivered project imports and compiles in pinned Unity; an evaluator copy "
                "contains a discoverable scene, builds a macOS player, launches it, and captures "
                "runtime metadata plus initial and later screenshots."
            ),
            "generic_probe": (
                "After solving, the evaluator injects only a generic build/screenshot/quit probe "
                "into a separate project copy. It does not require project-specific APIs, object "
                "names, coordinates, hooks, or a prescribed solver workflow."
            ),
            "quality_dimensions": {
                "brief_fulfillment": "0-4",
                "gameplay_systems": "0-4",
                "visual_coherence": "0-4",
                "usability_and_feedback": "0-4",
            },
            "showcase_selection": (
                "Select representative games only after all twenty finish, prioritizing hard-gate "
                "pass, genre diversity, visible completeness, and artifact inspectability; disclose "
                "that this is curated showcase selection rather than a random sample."
            ),
        },
        "schedule": [
            {"ordinal": index, "task_id": task["task_id"]}
            for index, task in enumerate(tasks, start=1)
        ],
        "tasks": tasks,
    }
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != encoded:
        raise RuntimeError(f"refusing to replace different frozen manifest: {output}")
    if not output.exists():
        output.write_text(encoded, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
