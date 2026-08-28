#!/usr/bin/env python3
"""Run the versioned 20-case open-request proxy and capture mechanism metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
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

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/unity-open-request-proxy-v2.json"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
    )
    parser.add_argument(
        "--condition",
        default="programmable-open-adaptive-v5",
    )
    parser.add_argument(
        "--game-task",
        type=Path,
        help="Optional versioned game-task policy override recorded in the protocol freeze.",
    )
    parser.add_argument("--experiment-directory", type=Path)
    parser.add_argument(
        "--agent-executable",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _brief_path(experiment: Path, case: dict[str, Any]) -> Path:
    if "brief" in case:
        return ROOT / str(case["brief"])
    inline = case.get("inline_brief")
    if not isinstance(inline, str) or not inline.strip():
        raise ValueError(f"create case {case['id']} requires brief or inline_brief")
    target = experiment / "briefs" / f"{case['id']}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = f"# {case['id']}\n\n{inline.strip()}\n"
    if target.exists() and target.read_text(encoding="utf-8") != content:
        raise RuntimeError(f"refusing to replace frozen inline brief: {target}")
    target.write_text(content, encoding="utf-8")
    return target


def _case_command(
    *,
    experiment: Path,
    manifest: dict[str, Any],
    uv: str,
    case: dict[str, Any],
    game_task: Path,
) -> list[str]:
    base = [uv, "run", "gameforge"]
    if case["mode"] == "query":
        command = [
            *base,
            "project-query",
            "--question",
            str(case["question"]),
            "--project",
            str(manifest["source_project"]),
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
        str(manifest["source_project"]),
        "--model-profile",
        "model.local.yaml",
        "--runtime-protocol",
        "programmable-v1",
        "--game-task",
        str(game_task),
        "--target-platform",
        "macos-standalone",
    ]
    if case["mode"] == "create":
        command.extend(("--brief", str(_brief_path(experiment, case))))
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


def _result_directories(source: Path, mode: str) -> set[Path]:
    parent = source / ("project-queries" if mode == "query" else "project-runs")
    if not parent.exists():
        return set()
    return {path.resolve() for path in parent.iterdir() if path.is_dir()}


def _trace_metrics(result_path: Path | None) -> dict[str, Any]:
    if result_path is None:
        return {
            "first_pass": None,
            "rework_rounds": 0,
            "program_cell_errors": 0,
            "decision_errors": 0,
        }
    agent_result = result_path.parent / "agent-result.json"
    if not agent_result.is_file():
        return {
            "first_pass": True,
            "rework_rounds": 0,
            "program_cell_errors": 0,
            "decision_errors": 0,
        }
    payload = json.loads(agent_result.read_text(encoding="utf-8"))
    trace = payload.get("trace", [])
    cell_errors = sum(
        isinstance(item, dict) and item.get("event") == "program_cell_error"
        for item in trace
    )
    decision_errors = sum(
        isinstance(item, dict) and item.get("event") == "model_program_decision_error"
        for item in trace
    )
    rework = cell_errors + decision_errors
    return {
        "first_pass": rework == 0,
        "rework_rounds": rework,
        "program_cell_errors": cell_errors,
        "decision_errors": decision_errors,
    }


def _preservation_metrics(
    *,
    result_path: Path | None,
    source_unchanged: bool,
) -> dict[str, Any]:
    if result_path is None:
        return {
            "unrelated_files_preserved": source_unchanged,
            "changed_paths": [],
            "unrelated_changed_paths": [],
        }
    preservation = result_path.parent / "preservation.json"
    if not preservation.is_file():
        return {
            "unrelated_files_preserved": source_unchanged,
            "changed_paths": [],
            "unrelated_changed_paths": [],
        }
    payload = json.loads(preservation.read_text(encoding="utf-8"))
    unrelated = payload.get("unrelated_changed_paths", [])
    return {
        "unrelated_files_preserved": source_unchanged and not unrelated,
        "changed_paths": payload.get("changed_paths", []),
        "unrelated_changed_paths": unrelated,
    }


def _write_progress(experiment: Path, case_count: int) -> None:
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
            "cases_total": case_count,
            "status_counts": dict(sorted(statuses.items())),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    game_task = (
        arguments.game_task.resolve(strict=True)
        if arguments.game_task is not None
        else (ROOT / str(manifest["game_task"])).resolve(strict=True)
    )
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != 20:
        raise ValueError("open-request proxy v2 requires exactly 20 cases")
    if len({str(case["id"]) for case in cases}) != len(cases):
        raise ValueError("open-request case ids must be unique")

    experiment = (
        arguments.experiment_directory
        or ROOT / f"runs/experiments/open-request-proxy-v2-{arguments.condition}-run1"
    ).resolve()
    agent = arguments.agent_executable.resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")

    runtime_digest, runtime_count = _runtime_digest(ROOT)
    source_project = (ROOT / str(manifest["source_project"])).resolve(strict=True)
    source_digest = _tree_sha256(source_project)
    experiment.mkdir(parents=True, exist_ok=True)
    protocol = {
        "schema_version": 1,
        "condition": arguments.condition,
        "runtime_protocol": "programmable-v1",
        "runtime_tree_sha256": runtime_digest,
        "runtime_file_count": runtime_count,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "model_profile": "model.local.yaml",
        "model": manifest["model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "source_project": manifest["source_project"],
        "source_project_sha256": source_digest,
        "game_task": str(game_task),
        "game_task_sha256": _sha256_file(game_task),
        "case_count": len(cases),
        "quota_stop_remaining_percent": 50,
        "metrics": {
            "clarification_or_approval_required": (
                "Observed safe refusal for deliberately unapproved/underspecified mutation; "
                "not a natural clarification-rate estimate."
            ),
            "first_pass": (
                "Successful executed case with no program-cell or structured-decision errors."
            ),
            "rework_rounds": "Count of program-cell plus structured-decision error events.",
            "unrelated_files_preserved": (
                "Source template unchanged and preservation artifact has no unrelated paths."
            ),
        },
        "cases": cases,
    }
    protocol_path = experiment / "protocol-freeze.json"
    if protocol_path.exists():
        existing = json.loads(protocol_path.read_text(encoding="utf-8"))
        if existing != protocol:
            raise RuntimeError("open-request protocol differs from frozen protocol")
    else:
        _write_json(protocol_path, protocol)
    _write_progress(experiment, len(cases))
    if arguments.prepare_only:
        return 0

    for ordinal, raw_case in enumerate(cases, start=1):
        case = dict(raw_case)
        receipt_path = experiment / "receipts" / f"{ordinal:03d}-{case['id']}.json"
        if receipt_path.exists():
            continue
        current_digest, current_count = _runtime_digest(ROOT)
        if (current_digest, current_count) != (runtime_digest, runtime_count):
            raise RuntimeError("Harness runtime changed after open-request proxy start")
        if _tree_sha256(source_project) != source_digest:
            raise RuntimeError("source Unity template changed after proxy start")

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

        before = _result_directories(ROOT / "runs", str(case["mode"]))
        command = _case_command(
            experiment=experiment,
            manifest=manifest,
            uv=uv,
            case=case,
            game_task=game_task,
        )
        started_at = datetime.now(UTC)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=1800,
            check=False,
        )
        ended_at = datetime.now(UTC)
        after = _result_directories(ROOT / "runs", str(case["mode"]))
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

        source_unchanged = _tree_sha256(source_project) == source_digest
        trace_metrics = _trace_metrics(result_path)
        if not passed and trace_metrics["first_pass"] is True:
            trace_metrics["first_pass"] = False
        preservation_metrics = _preservation_metrics(
            result_path=result_path,
            source_unchanged=source_unchanged,
        )
        receipt = {
            "schema_version": 1,
            "ordinal": ordinal,
            "case": case,
            "condition": arguments.condition,
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
            "source_project_unchanged": source_unchanged,
            "clarification_or_approval_required": (
                bool(passed) if case["mode"] == "unapproved" else False
            ),
            **trace_metrics,
            **preservation_metrics,
        }
        _write_json(receipt_path, receipt)
        _write_progress(experiment, len(cases))
        print(
            f"[{ordinal:02d}/{len(cases):02d}] {case['id']}: {receipt['status']} "
            f"({remaining:.1f}% quota remaining)",
            flush=True,
        )

    _write_json(
        experiment / "complete.json",
        {
            "schema_version": 1,
            "completed_at": datetime.now(UTC).isoformat(),
            "cases_completed": len(cases),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
