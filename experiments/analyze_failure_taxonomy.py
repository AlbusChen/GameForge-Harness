#!/usr/bin/env python3
"""Validate manual failure annotations and summarize overlapping cause flags."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    packet: list[dict[str, Any]] = json.loads(
        arguments.packet.resolve(strict=True).read_text(encoding="utf-8")
    )
    annotation_payload = json.loads(
        arguments.annotations.resolve(strict=True).read_text(encoding="utf-8")
    )
    annotations: list[dict[str, Any]] = annotation_payload["annotations"]
    packet_ids = [str(item["task_id"]) for item in packet]
    annotation_ids = [str(item["task_id"]) for item in annotations]
    errors: list[str] = []
    if len(annotation_ids) != len(set(annotation_ids)):
        errors.append("annotation task ids are not unique")
    if set(packet_ids) != set(annotation_ids):
        errors.append("annotation task ids do not match failure packet")
    by_id = {str(item["task_id"]): item for item in annotations}
    ordered = [by_id[task_id] for task_id in packet_ids if task_id in by_id]
    primary = Counter(str(item["primary_category"]) for item in ordered)
    flags = {
        key: sum(item[key] is True for item in ordered)
        for key in (
            "visual_or_geometry_grounding",
            "visible_requirement_omission",
            "harness_contribution",
        )
    }
    official_pass = {
        str(item["task_id"]): item["official_status"] == "PASS" for item in packet
    }
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {"valid": not errors, "errors": errors},
        "failure_count": len(packet),
        "primary_categories": dict(sorted(primary.items())),
        "overlapping_flags": flags,
        "official_pass_subset": {
            "failures": sum(official_pass.values()),
            "visual_or_geometry_grounding": sum(
                official_pass[item["task_id"]]
                and item["visual_or_geometry_grounding"] is True
                for item in ordered
            ),
            "visible_requirement_omission": sum(
                official_pass[item["task_id"]]
                and item["visible_requirement_omission"] is True
                for item in ordered
            ),
            "harness_contribution": sum(
                official_pass[item["task_id"]]
                and item["harness_contribution"] is True
                for item in ordered
            ),
        },
        "annotations": ordered,
    }
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(summary["validation"], sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
