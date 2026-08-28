#!/usr/bin/env python3
"""Freeze outcome-blind, paired 64-task manifests for three sibling Harnesses."""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
BASE_MANIFEST = ROOT / "benchmarks" / "gamedevbench-full-qualified332-v36p4.json"
AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
SEED = "gamedevbench-blind64-threeway-20260821"
ALLOCATION = {"2d": 23, "3d": 2, "script_resource": 24, "ui": 15}
PILOT_TASK_IDS = frozenset(
    {
        "task_0019",
        "task_0062",
        "task_0071",
        "task_0093",
        "task_0204",
        "task_0208",
        "task_0216",
        "task_0227",
        "task_0238",
        "task_0239",
        "task_0249",
        "task_0251",
        "task_0263",
        "task_0266",
        "task_0281",
        "task_0292",
    }
)
CONDITIONS = (
    (
        "baseline-minimal-open",
        ROOT / "versions" / "baseline-minimal-open" / "source",
        "paired baseline",
    ),
    (
        "open-workspace-observation-toolkit",
        ROOT / "versions" / "open-workspace-observation-toolkit" / "source",
        "independent sibling: optional read-only observation primitives",
    ),
    (
        "minimal-godot-host-execution",
        ROOT / "variants" / "minimal-godot-host-execution" / "source",
        "independent sibling: minimal project-scoped host execution for pinned Godot",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _select(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available = [task for task in tasks if str(task["task_id"]) not in PILOT_TASK_IDS]
    by_stratum: dict[str, list[dict[str, Any]]] = {}
    for task in available:
        by_stratum.setdefault(str(task["stratum"]), []).append(task)
    selected: list[dict[str, Any]] = []
    for stratum, count in sorted(ALLOCATION.items()):
        candidates = sorted(by_stratum[stratum], key=lambda task: str(task["task_id"]))
        random.Random(f"{SEED}:{stratum}").shuffle(candidates)
        if len(candidates) < count:
            raise RuntimeError(f"not enough {stratum} tasks for blind selection")
        selected.extend(candidates[:count])
    random.Random(f"{SEED}:task-order").shuffle(selected)
    if len(selected) != 64 or len({task["task_id"] for task in selected}) != 64:
        raise RuntimeError("blind selection did not produce 64 unique tasks")
    if any(str(task["task_id"]) in PILOT_TASK_IDS for task in selected):
        raise RuntimeError("blind selection overlaps the outcome-aware pilot")
    if Counter(str(task["stratum"]) for task in selected) != Counter(ALLOCATION):
        raise RuntimeError("blind selection stratum allocation changed")
    return selected


def _write_condition(
    base: dict[str, Any],
    selected: list[dict[str, Any]],
    *,
    name: str,
    source: Path,
    role: str,
) -> Path:
    resolved_source = source.resolve(strict=True)
    digest, count = _runtime_digest(resolved_source)
    payload = {
        **base,
        "schema_version": 1,
        "id": f"gamedevbench-blind64-{name}",
        "selection": {
            "design": "outcome-blind stratified random sample from qualified332",
            "frozen_before_any_blind64_model_execution": True,
            "seed": SEED,
            "eligible_tasks_before_pilot_exclusion": len(base["tasks"]),
            "excluded_pilot_task_ids": sorted(PILOT_TASK_IDS),
            "allocation": ALLOCATION,
            "selection_inputs": ["task_id", "stratum"],
            "prohibited_selection_inputs": [
                "historical harness outcome",
                "official outcome",
                "candidate outcome",
                "exit 134 history",
            ],
            "task_ids_in_execution_order": [str(task["task_id"]) for task in selected],
        },
        "tasks": selected,
        "code_freeze": {
            "runtime_tree_sha256": digest,
            "runtime_file_count": count,
            "source": str(resolved_source),
        },
        "experiment": {
            **base["experiment"],
            "condition": name,
            "comparison_group": "gamedevbench-blind64-threeway-20260821",
            "comparison_role": role,
            "runtime_protocol": "minimal-open-v1",
            "task_count": 64,
            "parallelism": 4,
            "repetitions_per_task": 1,
            "quota_stop_remaining_percent": 0.1,
            "agent_executable_sha256": _sha256(AGENT),
            "execution_rotation": {
                "block_size": 8,
                "condition_orders": [
                    [
                        "baseline-minimal-open",
                        "open-workspace-observation-toolkit",
                        "minimal-godot-host-execution",
                    ],
                    [
                        "open-workspace-observation-toolkit",
                        "minimal-godot-host-execution",
                        "baseline-minimal-open",
                    ],
                    [
                        "minimal-godot-host-execution",
                        "baseline-minimal-open",
                        "open-workspace-observation-toolkit",
                    ],
                ],
                "concurrent_conditions": False,
            },
            "primary_endpoint": "paired Official evaluator PASS over the same blind 64 tasks",
            "secondary_endpoints": [
                "model-visible exit 134",
                "Official verdict coverage",
                "latency",
                "tokens",
                "tool calls",
                "observation-tool adoption",
                "Godot host-execution return codes and overhead",
            ],
            "interpretation": (
                "The 64 tasks are disjoint from the outcome-aware pilot and selected without "
                "using any result. They are a confirmatory screen, not a Full332 estimate."
            ),
        },
    }
    output = ROOT / "benchmarks" / f"gamedevbench-blind64-{name}.json"
    if output.exists():
        raise RuntimeError(f"refusing to overwrite frozen manifest: {output}")
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{output}: {digest}/{count}")
    return output


def main() -> int:
    base = json.loads(BASE_MANIFEST.read_text(encoding="utf-8"))
    selected = _select(base["tasks"])
    for name, source, role in CONDITIONS:
        _write_condition(base, selected, name=name, source=source, role=role)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
