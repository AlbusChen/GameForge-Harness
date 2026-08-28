#!/usr/bin/env python3
"""Build a public-instruction/evaluator packet for manual Fresh-50 failure audit."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = Path("/private/tmp/gamedevbench-e3868-pinned")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        type=Path,
        default=ROOT / "runs/experiments/gamedevbench-fresh50-official-v4-v1",
    )
    return parser.parse_args()


def _public_config(task_id: str) -> dict[str, Any]:
    archive = BENCHMARK_ROOT / "tasks" / f"{task_id}.zip"
    with zipfile.ZipFile(archive) as source:
        return json.loads(source.read(f"tasks/{task_id}/task_config.json"))


def _failure_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines = [ANSI.sub("", line).strip() for line in path.read_text(errors="replace").splitlines()]
    selected = [
        line
        for line in lines
        if line
        and (
            "VALIDATION_FAILED" in line
            or "SCRIPT ERROR" in line
            or "Parse Error" in line
            or "ERROR:" in line
        )
    ]
    return selected[-40:]


def _agent_trace(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"summary": None, "errors": [], "decision_rationales": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    trace = payload.get("trace", [])
    return {
        "summary": payload.get("summary"),
        "errors": [
            str(item.get("error", "unknown"))
            for item in trace
            if isinstance(item, dict)
            and item.get("event")
            in {"program_cell_error", "model_program_decision_error"}
        ],
        "decision_rationales": [
            str(item["decision"].get("rationale", ""))
            for item in trace
            if isinstance(item, dict)
            and item.get("event") == "model_program_decision"
            and isinstance(item.get("decision"), dict)
        ],
    }


def main() -> int:
    experiment = _arguments().experiment.resolve(strict=True)
    analysis = json.loads((experiment / "analysis.json").read_text(encoding="utf-8"))
    receipt_rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    receipts = {
        (str(row["task_id"]), str(row["condition"])): row for row in receipt_rows
    }
    packet: list[dict[str, Any]] = []
    for task in analysis["tasks"]:
        if task["v4_evaluator"] != "FAIL":
            continue
        task_id = str(task["task_id"])
        candidate = receipts[(task_id, "programmable-open-global-diagnostic-v4")]
        official = receipts[(task_id, "official-default")]
        run_directory = Path(candidate["record"]["run_directory"])
        config = _public_config(task_id)
        packet.append(
            {
                "task_id": task_id,
                "task_name": task["task_name"],
                "stratum": task["stratum"],
                "instruction": config.get("instruction"),
                "official_status": task["official"],
                "official_message": official["record"].get("message"),
                "v4_terminal": task["v4_terminal"],
                "v4_evaluator_failures": _failure_lines(
                    run_directory / "official-evaluator.log"
                ),
                "v4_agent": _agent_trace(run_directory / "agent-result.json"),
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
