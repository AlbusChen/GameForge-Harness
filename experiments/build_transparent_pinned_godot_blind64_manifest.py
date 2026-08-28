#!/usr/bin/env python3
"""Freeze the same-task blind64 regression manifest for the transparent transport."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_MANIFEST = (
    ROOT / "benchmarks" / "gamedevbench-blind64-minimal-godot-host-execution.json"
)
SOURCE_RUNTIME = ROOT / "variants" / "transparent-pinned-godot-transport" / "source"
OUTPUT = ROOT / "benchmarks" / "gamedevbench-blind64-transparent-pinned-godot-transport.json"
AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if OUTPUT.exists():
        raise RuntimeError(f"refusing to overwrite frozen manifest: {OUTPUT}")
    source = SOURCE_RUNTIME.resolve(strict=True)
    digest, file_count = _runtime_digest(source)
    payload = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    payload.update(
        {
            "id": "gamedevbench-blind64-transparent-pinned-godot-transport",
            "purpose": (
                "Same-task paired regression of a transparent pinned-Godot host transport "
                "independently derived from baseline-minimal-open."
            ),
            "code_freeze": {
                "runtime_tree_sha256": digest,
                "runtime_file_count": file_count,
                "source": str(source),
            },
        }
    )
    payload["selection"] = {
        **payload["selection"],
        "candidate_frozen_before_its_model_execution": True,
        "reuse_note": (
            "The task sample and order were originally selected outcome-blind on 2026-08-21. "
            "This candidate was designed after the prior three-way results were observed, so its "
            "run is a same-task comparable regression, not a new outcome-blind candidate screen."
        ),
    }
    payload["experiment"] = {
        **payload["experiment"],
        "condition": "transparent-pinned-godot-transport",
        "comparison_group": "gamedevbench-blind64-transparent-transport-20260822",
        "comparison_role": (
            "independent baseline sibling: transparent argv and workspace/temp cwd transport "
            "for pinned Godot"
        ),
        "agent_executable_sha256": _sha256(AGENT),
        "execution_rotation": None,
        "primary_endpoint": (
            "paired Official evaluator PASS against the existing baseline on the same 64 tasks"
        ),
        "secondary_endpoints": [
            "model-visible crash-class exits",
            "Official verdict coverage",
            "latency",
            "tokens",
            "tool calls",
            "pinned-Godot transport return codes, cwd scopes, rejection count, and overhead",
        ],
        "interpretation": (
            "The task sample is exactly the prior blind64 set for direct paired comparability. "
            "Because this transport was designed after observing the earlier results, promotion "
            "still requires an unseen or complete benchmark confirmation."
        ),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(OUTPUT)
    print(f"runtime_tree_sha256={digest}")
    print(f"runtime_file_count={file_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
