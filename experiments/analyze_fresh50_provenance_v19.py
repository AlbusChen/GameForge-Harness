#!/usr/bin/env python3
"""Compare the frozen v19 provenance run with v18, v17, and Official."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.analyze_fresh50_general_substrate_v17 import (
    _decision_audit,
    _paired,
    _resource_delta,
    _workspace_timeout_audit,
)

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260819
VALIDATION_FAILURE = re.compile(r"VALIDATION_FAILED(?::\s*(.+))?")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--v19-experiment",
        type=Path,
        default=ROOT
        / "runs/experiments/"
        "gamedevbench-fresh50-programmable-open-provenance-v19-run1",
    )
    parser.add_argument(
        "--v18-experiment",
        type=Path,
        default=ROOT
        / "runs/experiments/"
        "gamedevbench-fresh50-programmable-open-evidence-loop-v18-run1",
    )
    parser.add_argument(
        "--v17-experiment",
        type=Path,
        default=ROOT
        / "runs/experiments/"
        "gamedevbench-fresh50-programmable-open-general-substrate-v17-run1",
    )
    return parser.parse_args()


def _rows(experiment: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]


def _statuses(rows: list[dict[str, Any]]) -> dict[str, bool]:
    return {
        str(row["task_id"]): row["normalized_status"] == "PASS" for row in rows
    }


def _failure_messages(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    messages: dict[str, list[str]] = {}
    for row in rows:
        if row["normalized_status"] == "PASS":
            continue
        log_path = Path(row["record"]["run_directory"]) / "official-evaluator.log"
        found = [
            match.group(1) or "unspecified evaluator failure"
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if (match := VALIDATION_FAILURE.search(line))
        ]
        messages[str(row["task_id"])] = found
    return messages


def _capture_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks: list[dict[str, object]] = []
    fidelity: Counter[str] = Counter()
    animated_nodes = 0
    texture_control_nodes = 0
    frames_reported = 0
    for row in rows:
        run_directory = Path(row["record"]["run_directory"])
        captures = sorted(run_directory.glob("scene-evidence-*.capture.json"))
        if not captures:
            continue
        task_captures: list[dict[str, object]] = []
        for path in captures:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            nodes = metadata.get("nodes", [])
            task_animated = 0
            task_texture_controls = 0
            task_frames = 0
            if isinstance(nodes, list):
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    animated = node.get("animated_sprite_2d")
                    if isinstance(animated, dict):
                        task_animated += 1
                        sprite_frames = animated.get("sprite_frames")
                        if isinstance(sprite_frames, dict):
                            animations = sprite_frames.get("animations")
                            if isinstance(animations, list):
                                task_frames += sum(
                                    int(animation.get("frames_returned", 0))
                                    for animation in animations
                                    if isinstance(animation, dict)
                                )
                    if isinstance(node.get("texture_control"), dict):
                        task_texture_controls += 1
            animated_nodes += task_animated
            texture_control_nodes += task_texture_controls
            frames_reported += task_frames
            task_captures.append(
                {
                    "path": path.name,
                    "state_phase": metadata.get("state_phase"),
                    "animated_sprite_nodes": task_animated,
                    "texture_control_nodes": task_texture_controls,
                    "animation_frames_reported": task_frames,
                }
            )
        evidence_paths = sorted(run_directory.glob("scene-evidence-*.json"))
        task_fidelity = []
        for path in evidence_paths:
            evidence = json.loads(path.read_text(encoding="utf-8"))
            capture_evidence = evidence.get("capture_evidence")
            if not isinstance(capture_evidence, dict):
                continue
            value = str(capture_evidence.get("fidelity_status", "unknown"))
            fidelity[value] += 1
            task_fidelity.append(value)
        tasks.append(
            {
                "task_id": str(row["task_id"]),
                "status": str(row["normalized_status"]),
                "captures": task_captures,
                "fidelity": task_fidelity,
            }
        )
    return {
        "tasks_using_scene_evidence": len(tasks),
        "fidelity": dict(sorted(fidelity.items())),
        "animated_sprite_nodes_reported": animated_nodes,
        "texture_control_nodes_reported": texture_control_nodes,
        "animation_frames_reported": frames_reported,
        "tasks": tasks,
    }


def _summary(
    statuses: dict[str, bool],
    rows_by_id: dict[str, dict[str, Any]],
) -> dict[str, object]:
    strata = sorted({str(row["stratum"]) for row in rows_by_id.values()})
    return {
        "attempts": len(statuses),
        "passes": sum(statuses.values()),
        "pass_rate": statistics.fmean(statuses.values()),
        "by_stratum": {
            stratum: {
                "attempts": len(task_ids),
                "passes": sum(statuses[task_id] for task_id in task_ids),
                "pass_rate": statistics.fmean(statuses[task_id] for task_id in task_ids),
            }
            for stratum in strata
            if (
                task_ids := [
                    task_id
                    for task_id, row in rows_by_id.items()
                    if row["stratum"] == stratum
                ]
            )
        },
    }


def main() -> int:
    arguments = _arguments()
    v19_experiment = arguments.v19_experiment.resolve(strict=True)
    v18_experiment = arguments.v18_experiment.resolve(strict=True)
    v17_experiment = arguments.v17_experiment.resolve(strict=True)
    complete = json.loads(
        (v19_experiment / "complete.json").read_text(encoding="utf-8")
    )
    v19_rows = _rows(v19_experiment)
    v18_rows = _rows(v18_experiment)
    v17_rows = _rows(v17_experiment)
    if len(v19_rows) != 50 or complete.get("attempts_completed") != 50:
        raise RuntimeError("v19 formal run is incomplete")
    order = [str(row["task_id"]) for row in v19_rows]
    if order != [str(row["task_id"]) for row in v18_rows] or order != [
        str(row["task_id"]) for row in v17_rows
    ]:
        raise RuntimeError("experiment task order differs")
    v19 = _statuses(v19_rows)
    v18 = _statuses(v18_rows)
    v17 = _statuses(v17_rows)
    v18_analysis = json.loads(
        (v18_experiment / "analysis.json").read_text(encoding="utf-8")
    )
    official = {
        str(task["task_id"]): task["official"] == "PASS"
        for task in v18_analysis["tasks"]
    }
    rows_by_id = {str(row["task_id"]): row for row in v19_rows}
    condition = json.loads(
        (v19_experiment / "protocol-freeze.json").read_text(encoding="utf-8")
    )["condition"]
    v19_analysis = json.loads(
        (v19_experiment / "analysis.json").read_text(encoding="utf-8")
    )
    v19_resources = v19_analysis["overall"][condition]["resources"]
    v18_resources = v18_analysis["overall"][v18_analysis["inputs"]["condition"]][
        "resources"
    ]
    v17_analysis = json.loads(
        (v17_experiment / "analysis.json").read_text(encoding="utf-8")
    )
    v17_resources = v17_analysis["overall"][v17_analysis["inputs"]["condition"]][
        "resources"
    ]
    result = {
        "schema_version": 1,
        "validation": {
            "attempts": len(v19_rows),
            "complete_marker": complete.get("attempts_completed") == 50,
            "analysis_valid": v19_analysis["validation"]["valid"],
            "blocked": sum(row["normalized_status"] == "BLOCKED" for row in v19_rows),
        },
        "headline": {
            "official": _summary(official, rows_by_id),
            "v17": _summary(v17, rows_by_id),
            "v18": _summary(v18, rows_by_id),
            "v19": _summary(v19, rows_by_id),
        },
        "paired": {
            "v19_vs_official": _paired(v19, official, order, seed=SEED),
            "v19_vs_v17": _paired(v19, v17, order, seed=SEED + 1),
            "v19_vs_v18": _paired(v19, v18, order, seed=SEED + 2),
        },
        "resources": {
            "v17": v17_resources,
            "v18": v18_resources,
            "v19": v19_resources,
            "v19_fractional_change_vs_v17": _resource_delta(
                v19_resources, v17_resources
            ),
            "v19_fractional_change_vs_v18": _resource_delta(
                v19_resources, v18_resources
            ),
        },
        "decision_substrate": _decision_audit(v19_rows),
        "workspace": _workspace_timeout_audit(v19_rows),
        "capture_provenance": _capture_audit(v19_rows),
        "failure_messages": _failure_messages(v19_rows),
        "controls": {
            "preservation": dict(
                sorted(
                    Counter(
                        str(row["record"].get("preservation_gate", "NOT_RUN"))
                        for row in v19_rows
                    ).items()
                )
            ),
            "policy_violations": sum(
                int(row["record"].get("policy_violations", 0)) for row in v19_rows
            ),
        },
    }
    output = v19_experiment / "comparative-analysis.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["headline"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
