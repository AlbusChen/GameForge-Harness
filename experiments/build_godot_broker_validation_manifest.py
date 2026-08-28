#!/usr/bin/env python3
"""Freeze the preregistered high-signal Godot broker validation manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
BASE_MANIFEST = ROOT / "benchmarks/gamedevbench-full-qualified332-v36p4.json"
BASE_MANIFEST_SHA256 = "a36974f033b323b86bf5c3c3747a2beff478b9feb6048e9e83b7e27a02a9aff1"
OUTPUT = ROOT / "benchmarks/gamedevbench-godot-broker-highsignal20-v37p4-r2.json"
CONDITION = "minimal-open-godot-broker-v37p4-highsignal20-r2"

EXPLICIT_CRASH_BLOCKED = {
    "task_0031",
    "task_0071",
    "task_0094",
    "task_0108",
    "task_0231",
    "task_0236",
    "task_0281",
}
WRONG_GODOT_VERSION_FAIL = {
    "task_0141",
    "task_0143",
    "task_0156",
    "task_0162",
    "task_0185",
}
V31_PASS_V36_FAIL_WITH_EXIT_134 = {
    "task_0103",
    "task_0108",
    "task_0141",
    "task_0156",
    "task_0157",
    "task_0196",
}
LOCAL_OFFICIAL_PASS_V36_FAIL_WITH_EXIT_134 = {
    "task_0044",
    "task_0078",
    "task_0100",
    "task_0146",
    "task_0297",
}
SELECTED = tuple(
    sorted(
        EXPLICIT_CRASH_BLOCKED
        | WRONG_GODOT_VERSION_FAIL
        | V31_PASS_V36_FAIL_WITH_EXIT_134
        | LOCAL_OFFICIAL_PASS_V36_FAIL_WITH_EXIT_134
    )
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-manifest", type=Path, default=BASE_MANIFEST)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selection_reasons(task_id: str) -> list[str]:
    reasons: list[str] = []
    for reason, members in (
        ("trajectory explicitly reported Godot crash blocking validation", EXPLICIT_CRASH_BLOCKED),
        ("v36 reported validating with unpinned Godot 4.7.1 and failed", WRONG_GODOT_VERSION_FAIL),
        ("v31 PASS, v36 FAIL, and v36 recorded exit 134", V31_PASS_V36_FAIL_WITH_EXIT_134),
        (
            "local Official PASS, v36 FAIL, and v36 recorded exit 134",
            LOCAL_OFFICIAL_PASS_V36_FAIL_WITH_EXIT_134,
        ),
    ):
        if task_id in members:
            reasons.append(reason)
    return reasons


def main() -> int:
    arguments = _arguments()
    base_path = arguments.base_manifest.resolve(strict=True)
    output = arguments.output.resolve()
    if _sha256(base_path) != BASE_MANIFEST_SHA256:
        raise RuntimeError("v36 base manifest differs from the reviewed freeze")
    if output.exists():
        raise RuntimeError(f"refusing to replace frozen validation manifest: {output}")
    base = json.loads(base_path.read_text(encoding="utf-8"))
    indexed = {str(task["task_id"]): task for task in base["tasks"]}
    if len(SELECTED) != 20 or any(task_id not in indexed for task_id in SELECTED):
        raise RuntimeError("high-signal selection is incomplete")
    tasks = [
        {**indexed[task_id], "selection_reasons": _selection_reasons(task_id)}
        for task_id in SELECTED
    ]
    runtime_digest, runtime_count = _runtime_digest(ROOT)
    strata = Counter(str(task["stratum"]) for task in tasks)
    payload = {
        "schema_version": 1,
        "id": "gamedevbench-godot-broker-highsignal20-v37p4-r2",
        "benchmark_commit": base["benchmark_commit"],
        "purpose": (
            "Post-fix diagnostic validation of the project-scoped Host Godot broker on 20 "
            "outcome-aware high-signal tasks. This deliberately selected subset is not an "
            "unbiased benchmark accuracy estimate."
        ),
        "qualification": base["qualification"],
        "official_reference": base["official_reference"],
        "selection": {
            "method": (
                "Union of four categories fixed before rerun: explicit crash-blocked FAIL, "
                "wrong-Godot-version FAIL, v31-only PASS with exit 134, and local-Official-only "
                "PASS with exit 134."
            ),
            "task_ids": list(SELECTED),
            "outcome_aware": True,
            "headline_accuracy_eligible": False,
            "interpretation": (
                "Use for mechanism recovery, within-task PASS conversion, repeat agreement, "
                "and resource deltas only; never present its raw pass rate as Full332 accuracy."
            ),
        },
        "stratum_counts": dict(sorted(strata.items())),
        "stratum_method": base["stratum_method"],
        "code_freeze": {
            "runtime_scope": ["gameforge/**", "pyproject.toml", "uv.lock"],
            "runtime_file_count": runtime_count,
            "runtime_tree_sha256": runtime_digest,
            "pre_freeze_verification": (
                "Full Ruff, pytest, real Sol workspace-write broker smoke, real pinned Godot "
                "4.4.1 headless import, and git diff --check passed on 2026-08-19."
            ),
        },
        "experiment": {
            "condition": CONDITION,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "provider_sampling": "provider-default",
            "runtime_protocol": "minimal-open-v1",
            "task_count": len(tasks),
            "repetitions_per_task": 2,
            "parallelism": 4,
            "quota_stop_remaining_percent": 25.0,
            "primary_endpoints": [
                "model-visible exit-134 events",
                "broker invocation return codes and fixed 4.4.1 provenance",
                "within-task PASS conversion relative to frozen v36",
                "two-repetition agreement",
            ],
            "secondary_endpoints": [
                "tokens",
                "solver latency",
                "tool calls",
                "broker call count",
                "broker call latency",
            ],
            "stop_rule": (
                "Stop before an attempt if weekly subscription remaining is below 25%. No task "
                "replacement or runtime change is permitted after protocol freeze."
            ),
            "infrastructure_policy": base["experiment"]["infrastructure_policy"],
        },
        "tasks": tasks,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output}")
    print(f"sha256={_sha256(output)}")
    print(f"runtime_tree_sha256={runtime_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
