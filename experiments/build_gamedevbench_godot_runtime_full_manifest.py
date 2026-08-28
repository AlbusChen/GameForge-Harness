#!/usr/bin/env python3
"""Freeze the Full332 manifest for the simplified post-v37 Godot runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "benchmarks/gamedevbench-full-qualified332-v36p4.json"
BASE_SHA256 = "a36974f033b323b86bf5c3c3747a2beff478b9feb6048e9e83b7e27a02a9aff1"
OUTPUT = ROOT / "benchmarks/gamedevbench-full-qualified332-v38p4.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if _sha256(BASE) != BASE_SHA256:
        raise RuntimeError("reviewed v36 Full332 manifest differs from its freeze")
    if OUTPUT.exists():
        raise RuntimeError(f"refusing to replace frozen manifest: {OUTPUT}")

    payload = json.loads(BASE.read_text(encoding="utf-8"))
    runtime_digest, runtime_count = _runtime_digest(ROOT)
    payload["id"] = "gamedevbench-full-qualified332-v38p4"
    payload["purpose"] = (
        "Complete GameDevBench evaluation of the v36 minimal-open-clean Harness plus the "
        "simplified general Godot runtime boundary against frozen v36 and same-model Official."
    )
    payload["code_freeze"] = {
        "runtime_scope": ["gameforge/**", "pyproject.toml", "uv.lock"],
        "runtime_file_count": runtime_count,
        "runtime_tree_sha256": runtime_digest,
        "pre_freeze_verification": (
            "Full pytest, Ruff, git diff --check, real Sol direct-binary block/broker smoke, "
            "real pinned Godot 4.4.1 import, no-new-crash-report checks, and the four-task "
            "high-risk real-run regression passed on 2026-08-19."
        ),
    }
    experiment = payload["experiment"]
    experiment["condition"] = "minimal-open-godot-runtime-v38p4-full332"
    experiment["quota_stop_remaining_percent"] = 0.1
    experiment["primary_endpoint"] = (
        "official evaluator PASS over all completed eligible tasks, targeting all 332"
    )
    experiment["secondary_endpoints"] = [
        "coverage",
        "paired conversion relative to frozen v36",
        "latency and tokens",
        "tool calls and model turns",
        "fixed Godot provenance",
        "broker return codes, execution timeouts, queue time, and crash retries",
        "new crash reports and residual processes",
    ]
    experiment["stop_rule"] = (
        "Run all 332 tasks while subscription quota remains available. Stop only before an "
        "attempt if reported weekly remaining quota is below 0.1%; retain all completed "
        "receipts if quota is exhausted. No task replacement or runtime change is permitted "
        "after protocol freeze."
    )

    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    print(f"sha256={_sha256(OUTPUT)}")
    print(f"runtime_tree_sha256={runtime_digest}")
    print(f"runtime_file_count={runtime_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
