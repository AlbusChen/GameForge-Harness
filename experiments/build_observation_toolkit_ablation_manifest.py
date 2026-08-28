#!/usr/bin/env python3
"""Freeze the paired baseline/observation-toolkit GameDevBench ablation manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
BASE_MANIFEST = ROOT / "benchmarks" / "gamedevbench-full-qualified332-v36p4.json"
BASELINE_SOURCE = ROOT / "versions" / "baseline-minimal-open" / "source"
CANDIDATE_SOURCE = ROOT / "variants" / "open-workspace-observation-toolkit" / "source"
AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

TASK_IDS = (
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
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_condition(
    base: dict[str, object],
    *,
    name: str,
    source: Path,
    note: str,
) -> Path:
    digest, count = _runtime_digest(source)
    by_id = {str(task["task_id"]): task for task in base["tasks"]}  # type: ignore[index]
    tasks = [by_id[task_id] for task_id in TASK_IDS]
    payload = {
        **base,
        "schema_version": 1,
        "id": f"gamedevbench-ablation16-{name}",
        "selection": {
            "design": "paired high-signal observation-toolkit ablation with pass controls",
            "frozen_before_candidate_execution": True,
            "task_ids": list(TASK_IDS),
            "rationale": (
                "Ten baseline failures cover visual grounding and exact resource semantics; "
                "six baseline passes are matched family/stratum regression controls."
            ),
        },
        "tasks": tasks,
        "code_freeze": {
            "runtime_tree_sha256": digest,
            "runtime_file_count": count,
            "source": str(source.resolve(strict=True)),
        },
        "experiment": {
            **base["experiment"],  # type: ignore[index]
            "condition": name,
            "runtime_protocol": "minimal-open-v1",
            "task_count": len(tasks),
            "parallelism": 4,
            "repetitions_per_task": 1,
            "quota_stop_remaining_percent": 0.1,
            "agent_executable_sha256": _sha256(AGENT),
            "comparison_role": (
                "paired-baseline" if name == "baseline-minimal-open" else "candidate"
            ),
            "note": note,
        },
    }
    output = ROOT / "benchmarks" / f"gamedevbench-ablation16-{name}.json"
    if output.exists():
        raise RuntimeError(f"refusing to overwrite frozen manifest: {output}")
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{output}: {digest}/{count}")
    return output


def main() -> int:
    base = json.loads(BASE_MANIFEST.read_text(encoding="utf-8"))
    _write_condition(
        base,
        name="baseline-minimal-open",
        source=BASELINE_SOURCE,
        note="Current-client rerun of the unchanged semantic baseline.",
    )
    _write_condition(
        base,
        name="open-workspace-observation-toolkit",
        source=CANDIDATE_SOURCE,
        note="Baseline plus one optional, read-only, bounded observation command.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
