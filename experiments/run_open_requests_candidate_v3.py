#!/usr/bin/env python3
"""Rerun the frozen seven-case open-request suite on the v3 Harness only."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.run_unseen30_maturity import (
    _replace_json,
    _runtime_digest,
    _sha256_file,
    _weekly_used_percent,
    _write_json,
)
from gameforge.telemetry.subscription import capture_subscription_snapshot

CONDITION = "programmable-open-global-v3"


def _arguments() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment-directory",
        type=Path,
        default=(
            root
            / f"runs/experiments/open-requests-{CONDITION.removeprefix('programmable-')}-run1"
        ),
    )
    parser.add_argument(
        "--agent-executable",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "query-enemy-speed",
        "mode": "query",
        "question": "敌人的移动速度在哪里控制？当前默认值是多少？请只解释，不要修改项目。",
        "paths": ["Assets/Scripts/EnemyController.cs"],
    },
    {
        "id": "query-end-screen",
        "mode": "query",
        "question": (
            "游戏结束界面在什么条件下显示，并且如何决定显示 Victory 还是 Defeat？"
            "请串联相关状态变化，只解释、不修改。"
        ),
        "paths": [
            "Assets/Scripts/HudController.cs",
            "Assets/Scripts/GameController.cs",
            "Assets/Scripts/GameStatus.cs",
        ],
    },
    {
        "id": "reject-ambiguous-mutation",
        "mode": "unapproved",
        "request": "让游戏更有趣。",
        "editable_paths": ["Assets/Scripts/GameController.cs"],
        "acceptance": [
            "file_contains:Assets/Scripts/GameController.cs::namespace VerifiedGameBuilder.Game"
        ],
        "gates": ["compilation"],
    },
    {
        "id": "change-weapon-fire-rate",
        "mode": "change",
        "request": "Increase the arena weapon fire rate from four to six shots per second.",
        "editable_paths": ["Assets/Scripts/WeaponController.cs"],
        "acceptance": [
            "file_contains:Assets/Scripts/WeaponController.cs::private float fireRate = 6f;",
            "file_not_contains:Assets/Scripts/WeaponController.cs::private float fireRate = 4f;",
        ],
        "gates": ["compilation", "gameplay"],
    },
    {
        "id": "change-enemy-speed",
        "mode": "change",
        "request": "Increase the default enemy movement speed from three to 3.5.",
        "editable_paths": ["Assets/Scripts/EnemySpawner.cs"],
        "acceptance": [
            "file_contains:Assets/Scripts/EnemySpawner.cs::private float speed = 3.5f;",
            "file_not_contains:Assets/Scripts/EnemySpawner.cs::private float speed = 3f;",
        ],
        "gates": ["compilation", "gameplay"],
    },
    {
        "id": "change-hud-health-label",
        "mode": "change",
        "request": (
            "Shorten the HUD health label from Health to HP without changing other HUD text."
        ),
        "editable_paths": ["Assets/Scripts/HudController.cs"],
        "acceptance": [
            (
                "file_contains:Assets/Scripts/HudController.cs::healthText.text = "
                "$\"HP: {playerHealth.CurrentHealth}/{playerHealth.MaxHealth}\";"
            ),
            (
                "file_contains:Assets/Scripts/HudController.cs::enemiesText.text = "
                "$\"Enemies: {gameController.EnemiesAlive}\";"
            ),
        ],
        "gates": ["compilation", "gameplay"],
    },
    {
        "id": "create-arena-tuning-script",
        "mode": "create",
        "brief": "benchmarks/briefs/create-arena-tuning-script.md",
        "editable_paths": ["Assets/Scripts/ArenaTuning.cs"],
        "acceptance": [
            "file_exists:Assets/Scripts/ArenaTuning.cs",
            (
                "file_contains:Assets/Scripts/ArenaTuning.cs::"
                "public const int DifficultyTier = 2;"
            ),
        ],
        "gates": [
            "specification",
            "compilation",
            "structure",
            "gameplay",
            "build",
            "smoke",
        ],
    },
)


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _case_command(root: Path, uv: str, case: dict[str, Any]) -> list[str]:
    base = [uv, "run", "gameforge"]
    if case["mode"] == "query":
        command = [
            *base,
            "project-query",
            "--question",
            str(case["question"]),
            "--project",
            "unity/ArenaTemplate",
            "--model-profile",
            "model.local.yaml",
        ]
        for path in case["paths"]:
            command.extend(("--path", str(path)))
        return command

    command = [
        *base,
        "project-run",
        "--source",
        "create" if case["mode"] == "create" else "change",
        "--project",
        "unity/ArenaTemplate",
        "--model-profile",
        "model.local.yaml",
        "--runtime-protocol",
        "programmable-v1",
        "--game-task",
        "benchmarks/unity-programmable-open-suite-v1.json",
        "--target-platform",
        "macos-standalone",
    ]
    if case["mode"] == "create":
        command.extend(("--brief", str(case["brief"])))
    else:
        command.extend(("--request", str(case["request"])))
    for path in case["editable_paths"]:
        command.extend(("--editable-path", str(path)))
    for assertion in case["acceptance"]:
        command.extend(("--assert", str(assertion)))
    for gate in case["gates"]:
        command.extend(("--gate", str(gate)))
    if case["mode"] != "unapproved":
        command.append("--approve-mutations")
    return command


def _result_directories(root: Path, mode: str) -> set[Path]:
    parent = root / ("runs/project-queries" if mode == "query" else "runs/project-runs")
    if not parent.exists():
        return set()
    return {path.resolve() for path in parent.iterdir() if path.is_dir()}


def _write_progress(experiment: Path) -> None:
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    statuses = Counter(str(item["status"]) for item in receipts)
    _replace_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "cases_completed": len(receipts),
            "cases_total": len(CASES),
            "status_counts": dict(sorted(statuses.items())),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    root = Path(__file__).resolve().parents[1]
    experiment = arguments.experiment_directory.resolve()
    agent = arguments.agent_executable.resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")

    runtime_digest, runtime_count = _runtime_digest(root)
    source_project = root / "unity/ArenaTemplate"
    source_digest = _tree_sha256(source_project)
    experiment.mkdir(parents=True, exist_ok=True)
    _write_json(
        experiment / "protocol-freeze.json",
        {
            "schema_version": 1,
            "condition": CONDITION,
            "runtime_protocol": "programmable-v1",
            "runtime_tree_sha256": runtime_digest,
            "runtime_file_count": runtime_count,
            "model_profile": "model.local.yaml",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "source_project": "unity/ArenaTemplate",
            "source_project_sha256": source_digest,
            "game_task": "benchmarks/unity-programmable-open-suite-v1.json",
            "case_count": len(CASES),
            "quota_stop_remaining_percent": 50,
            "cases": list(CASES),
        },
    )
    _write_progress(experiment)
    if arguments.prepare_only:
        return 0

    for ordinal, case in enumerate(CASES, start=1):
        receipt_path = experiment / "receipts" / f"{ordinal:03d}-{case['id']}.json"
        if receipt_path.exists():
            continue
        current_digest, current_count = _runtime_digest(root)
        if (current_digest, current_count) != (runtime_digest, runtime_count):
            raise RuntimeError("Harness runtime changed after open-suite start")
        if _tree_sha256(source_project) != source_digest:
            raise RuntimeError("source Unity template changed after open-suite start")

        quota_path = experiment / "quota" / f"{ordinal:03d}-before.json"
        quota = capture_subscription_snapshot(agent)
        _replace_json(quota_path, quota)
        used_percent = _weekly_used_percent(quota)
        if used_percent is None:
            raise RuntimeError(f"weekly quota unavailable before case {ordinal}")
        remaining = 100.0 - used_percent
        if remaining < 50.0:
            _write_json(
                experiment / "quota-stop.json",
                {
                    "schema_version": 1,
                    "stopped_before_ordinal": ordinal,
                    "weekly_used_percent": used_percent,
                    "weekly_remaining_percent": remaining,
                },
            )
            return 0

        before = _result_directories(root, str(case["mode"]))
        command = _case_command(root, uv, case)
        started_at = datetime.now(UTC)
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=1800,
            check=False,
        )
        ended_at = datetime.now(UTC)
        after = _result_directories(root, str(case["mode"]))
        new_directories = sorted(after - before)
        log_path = experiment / "logs" / f"{ordinal:03d}-{case['id']}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            completed.stdout + "\n--- STDERR ---\n" + completed.stderr,
            encoding="utf-8",
        )

        result: dict[str, Any] | None = None
        result_path: Path | None = None
        if case["mode"] == "unapproved":
            passed = (
                completed.returncode == 2
                and "project-run requires --approve-mutations" in completed.stderr
                and not new_directories
                and _tree_sha256(source_project) == source_digest
            )
        else:
            if len(new_directories) != 1:
                raise RuntimeError(
                    f"case {case['id']} produced {len(new_directories)} result directories"
                )
            result_path = new_directories[0] / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            passed = result.get("status") == "PASS"
            if case["mode"] == "query":
                passed = passed and result.get("project_unchanged") is True

        receipt = {
            "schema_version": 1,
            "ordinal": ordinal,
            "case": case,
            "condition": CONDITION,
            "status": "PASS" if passed else "FAIL",
            "runner_exit_code": completed.returncode,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_seconds": round((ended_at - started_at).total_seconds(), 3),
            "weekly_used_percent_before": used_percent,
            "quota_snapshot": str(quota_path),
            "log_path": str(log_path),
            "log_sha256": _sha256_file(log_path),
            "result_path": str(result_path) if result_path else None,
            "result_sha256": _sha256_file(result_path) if result_path else None,
            "record": result,
            "source_project_unchanged": _tree_sha256(source_project) == source_digest,
        }
        _write_json(receipt_path, receipt)
        _write_progress(experiment)
        print(
            f"[{ordinal:02d}/{len(CASES):02d}] {case['id']}: {receipt['status']} "
            f"({remaining:.1f}% quota remaining)",
            flush=True,
        )

    _write_json(
        experiment / "complete.json",
        {
            "schema_version": 1,
            "completed_at": datetime.now(UTC).isoformat(),
            "case_count": len(CASES),
            "source_project_unchanged": _tree_sha256(source_project) == source_digest,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
