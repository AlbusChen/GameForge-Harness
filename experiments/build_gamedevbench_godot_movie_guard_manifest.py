#!/usr/bin/env python3
"""Freeze the v36-based targeted MovieWriter compatibility candidate."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "benchmarks/gamedevbench-full-qualified332-v36p4.json"
BASE_SHA256 = "a36974f033b323b86bf5c3c3747a2beff478b9feb6048e9e83b7e27a02a9aff1"
OUTPUT = ROOT / "benchmarks/gamedevbench-godot-movie-guard9-v39p4.json"
TASK_IDS = (
    "task_0181",
    "task_0214",
    "task_0217",
    "task_0218",
    "task_0219",
    "task_0220",
    "task_0224",
    "task_0232",
    "task_0246",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if _sha256(BASE) != BASE_SHA256:
        raise RuntimeError("reviewed v36 Full332 manifest differs from its freeze")
    if OUTPUT.exists():
        raise RuntimeError(f"refusing to replace frozen manifest: {OUTPUT}")

    payload = json.loads(BASE.read_text(encoding="utf-8"))
    base_tasks = {task["task_id"]: task for task in payload["tasks"]}
    if any(task_id not in base_tasks for task_id in TASK_IDS):
        raise RuntimeError("targeted task is absent from the frozen v36 manifest")
    tasks = [{**base_tasks[task_id]} for task_id in TASK_IDS]
    runtime_digest, runtime_count = _runtime_digest(ROOT)
    payload["id"] = "gamedevbench-godot-movie-guard9-v39p4"
    payload["purpose"] = (
        "Outcome-aware runtime regression of the minimal MovieWriter compatibility guard, "
        "derived directly from frozen v36 rather than any post-v36 candidate."
    )
    payload["tasks"] = tasks
    payload["stratum_counts"] = dict(Counter(task["stratum"] for task in tasks))
    payload["code_freeze"] = {
        "runtime_scope": ["gameforge/**", "pyproject.toml", "uv.lock"],
        "runtime_file_count": runtime_count,
        "runtime_tree_sha256": runtime_digest,
        "base_version": "v36",
        "base_manifest_sha256": BASE_SHA256,
        "pre_freeze_verification": (
            "Full pytest and Ruff passed; a real pinned Godot 4.4.1 smoke rejected "
            "headless MovieWriter before process launch, completed a two-frame "
            "rendering-enabled capture, and produced no new crash report on 2026-08-20."
        ),
    }
    payload["experiment"] = {
        "condition": "minimal-open-clean-v36-godot-movie-guard-v39p4",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "provider_sampling": "provider-default",
        "runtime_protocol": "minimal-open-v1",
        "parallelism": 4,
        "repetitions_per_task": 2,
        "task_count": len(tasks),
        "quota_stop_remaining_percent": 0.1,
        "infrastructure_policy": (
            "Normal model or evaluator FAIL is acceptable. A Godot process must not start for "
            "the known-incompatible headless MovieWriter combination; every attempt must "
            "complete an Official evaluator verdict without an unexplained process exit."
        ),
        "primary_endpoints": [
            "new Godot crash reports",
            "headless MovieWriter requests rejected before engine launch",
            "rendering-enabled MovieWriter completion",
            "Official evaluator verdict completion",
        ],
        "secondary_endpoints": [
            "PASS stability across two repetitions",
            "comparison with frozen v36 and v38 outcomes",
            "latency, tokens, broker return codes, and residual processes",
        ],
    }
    payload["official_reference"]["comparison_note"] = (
        "This outcome-aware nine-task diagnostic is not an aggregate accuracy comparison."
    )
    payload["qualification"]["method"] = (
        "Reuse the frozen v36 Full332 ground-truth qualification and the exact nine already-"
        "qualified tasks that triggered MovieWriter crashes in v38p4."
    )
    payload["selection"] = {
        "outcome_aware": True,
        "headline_accuracy_eligible": False,
        "method": (
            "All and only the nine v38p4 Full332 tasks whose broker audit contained an "
            "underlying return code 134 and whose macOS reports classified in MovieWriter."
        ),
        "task_ids": list(TASK_IDS),
        "interpretation": (
            "Use for runtime-crash elimination and within-task recovery evidence only; do not "
            "substitute its PASS rate for Full332."
        ),
    }

    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    print(f"sha256={_sha256(OUTPUT)}")
    print(f"runtime_tree_sha256={runtime_digest}")
    print(f"runtime_file_count={runtime_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
