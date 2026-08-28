#!/usr/bin/env python3
"""Retry each zero-turn Fresh-50 agent-CLI failure once without replacing first attempts."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.run_unseen30_maturity import (
    _harness_attempt,
    _replace_json,
    _runtime_digest,
    _sha256_file,
    _weekly_used_percent,
    _write_json,
)
from gameforge.telemetry.subscription import capture_subscription_snapshot

ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-experiment", type=Path, required=True)
    return parser.parse_args()


def _is_retryable_cli_failure(receipt: dict[str, Any]) -> bool:
    record = receipt.get("record", {})
    return bool(
        receipt.get("normalized_status") == "BLOCKED"
        and record.get("failure_category") == "ModelError"
        and record.get("failure_detail") == "agent CLI returned exit code 1"
        and record.get("model_turns") == 0
    )


def main() -> int:
    arguments = _arguments()
    primary = arguments.primary_experiment.resolve(strict=True)
    protocol = json.loads((primary / "protocol-freeze.json").read_text(encoding="utf-8"))
    if not (primary / "complete.json").is_file():
        raise RuntimeError("primary Fresh-50 experiment is incomplete")
    digest, count = _runtime_digest(ROOT)
    if digest != protocol["runtime_tree_sha256"]:
        raise RuntimeError("Harness runtime differs from the frozen primary experiment")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    agent = Path(protocol["agent_executable"]).resolve(strict=True)
    benchmark_root = Path(protocol["benchmark_root"]).resolve(strict=True)
    primary_rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((primary / "receipts").glob("*.json"))
    ]
    retry_rows = [row for row in primary_rows if _is_retryable_cli_failure(row)]
    supplemental = primary / "supplemental-cli-retry1"
    supplemental.mkdir(parents=True, exist_ok=True)
    freeze = {
        "schema_version": 1,
        "policy": "one retry for zero-turn agent CLI exit-code-1 failures only",
        "primary_experiment": str(primary),
        "primary_complete_sha256": _sha256_file(primary / "complete.json"),
        "runtime_tree_sha256": digest,
        "runtime_file_count": count,
        "model": protocol["model"],
        "reasoning_effort": protocol["reasoning_effort"],
        "runtime_protocol": protocol["runtime_protocol"],
        "retry_count": len(retry_rows),
        "task_ids": [row["task_id"] for row in retry_rows],
    }
    _write_json(supplemental / "protocol-freeze.json", freeze)

    for index, primary_row in enumerate(retry_rows, start=1):
        receipt_path = supplemental / "receipts" / f"{index:03d}.json"
        if receipt_path.exists():
            continue
        current_digest, current_count = _runtime_digest(ROOT)
        if current_digest != digest or current_count != count:
            raise RuntimeError("Harness runtime changed during supplemental retries")
        quota = capture_subscription_snapshot(agent)
        quota_path = supplemental / "quota" / f"{index:03d}-before.json"
        _replace_json(quota_path, quota)
        used = _weekly_used_percent(quota)
        if used is None or 100.0 - used < 50.0:
            raise RuntimeError("weekly quota is unavailable or below the frozen safety floor")
        task_id = str(primary_row["task_id"])
        attempt = {
            "ordinal": index,
            "attempt_id": f"retry1-{task_id}",
            "task_id": task_id,
            "task_name": primary_row["task_name"],
            "stratum": primary_row["stratum"],
            "condition": "programmable-v1",
        }
        print(
            f"[{index:03d}/{len(retry_rows):03d}] retry {task_id} "
            f"({100.0 - used:.1f}% quota remaining)",
            flush=True,
        )
        record, exit_code, source = _harness_attempt(
            uv=uv,
            root=ROOT,
            benchmark_root=benchmark_root,
            experiment=supplemental,
            manifest_path=primary / "control-manifests" / f"{task_id}.json",
            agent_executable=agent,
            attempt=attempt,
            model=str(protocol["model"]),
            effort=str(protocol["reasoning_effort"]),
        )
        normalized = str(record.get("status", "ERROR"))
        if normalized not in {"PASS", "FAIL", "BLOCKED"}:
            raise RuntimeError(f"invalid supplemental retry status: {normalized}")
        _write_json(
            receipt_path,
            {
                **attempt,
                "schema_version": 1,
                "normalized_status": normalized,
                "runner_exit_code": exit_code,
                "result_source": str(source),
                "result_source_sha256": _sha256_file(source),
                "quota_snapshot": str(quota_path),
                "weekly_used_percent_before": used,
                "primary_ordinal": primary_row["ordinal"],
                "primary_receipt": str(
                    primary / "receipts" / f"{int(primary_row['ordinal']):03d}.json"
                ),
                "primary_status": primary_row["normalized_status"],
                "record": record,
            },
        )
        print(f"  -> {normalized}", flush=True)

    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((supplemental / "receipts").glob("*.json"))
    ]
    statuses = Counter(str(row["normalized_status"]) for row in receipts)
    _write_json(
        supplemental / "complete.json",
        {
            "schema_version": 1,
            "completed_at": datetime.now(UTC).isoformat(),
            "attempts_completed": len(receipts),
            "status_counts": dict(sorted(statuses.items())),
            "runtime_tree_sha256": digest,
        },
    )
    print(f"Supplemental retries complete: {dict(sorted(statuses.items()))}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
