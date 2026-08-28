#!/usr/bin/env python3
"""Build a public-evidence packet for failures in a completed Fresh-50 candidate run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.build_fresh50_taxonomy_packet import _agent_trace, _failure_lines, _public_config


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    experiment = _arguments().experiment.resolve(strict=True)
    analysis = json.loads((experiment / "analysis.json").read_text(encoding="utf-8"))
    condition = str(analysis["inputs"]["condition"])
    receipts: list[dict[str, Any]] = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    by_id = {str(row["task_id"]): row for row in receipts}
    packet: list[dict[str, Any]] = []
    for task in analysis["tasks"]:
        if task["candidate"] != "FAIL":
            continue
        task_id = str(task["task_id"])
        receipt = by_id[task_id]
        run_directory = Path(receipt["record"]["run_directory"])
        packet.append(
            {
                "task_id": task_id,
                "task_name": task["task_name"],
                "stratum": task["stratum"],
                "instruction": _public_config(task_id).get("instruction"),
                "official_status": task["official"],
                "v4_status": task["v4"],
                "candidate_condition": condition,
                "candidate_terminal": task["candidate_terminal"],
                "candidate_evaluator_failures": _failure_lines(
                    run_directory / "official-evaluator.log"
                ),
                "candidate_agent": _agent_trace(run_directory / "agent-result.json"),
                "annotation": {
                    "primary_category": None,
                    "visual_or_geometry_grounding": None,
                    "visible_requirement_omission": None,
                    "harness_contribution": None,
                    "confidence": None,
                    "rationale": None,
                },
            }
        )
    output = experiment / "failure-taxonomy-review-packet.json"
    output.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(f"candidate failures: {len(packet)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
