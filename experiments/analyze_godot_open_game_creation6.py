#!/usr/bin/env python3
# ruff: noqa: E501
"""Blind-review and summarize the paired six-genre open Godot creation proxy."""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.run_godot_open_game_creation6 import (
    DEFAULT_AGENT,
    DEFAULT_EXPERIMENT,
    DEFAULT_MANIFEST,
    TEXT_SUFFIXES,
    _credential_scrubbed_environment,
    _parse_agent_jsonl,
    _replace_json,
    _sha256_file,
    _write_json,
)

ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS = (
    "brief_fulfillment",
    "gameplay_systems",
    "visual_coherence",
    "usability_and_feedback",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--agent-executable", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--max-new-judges", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def _source_excerpt(workspace: Path, maximum_bytes: int = 120_000) -> str:
    blocks: list[str] = []
    used = 0
    paths = sorted(
        path
        for path in workspace.rglob("*")
        if path.is_file()
        and ".godot" not in path.relative_to(workspace).parts
        and path.suffix.lower() in TEXT_SUFFIXES
    )
    for path in paths:
        relative = path.relative_to(workspace).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        encoded = content.encode("utf-8")
        if len(encoded) > 30_000:
            content = encoded[:30_000].decode("utf-8", errors="ignore") + "\n[TRUNCATED]"
        block = f"\n--- {relative} ---\n{content}\n"
        block_size = len(block.encode("utf-8"))
        if used + block_size > maximum_bytes:
            break
        blocks.append(block)
        used += block_size
    return "".join(blocks) or "[NO READABLE PROJECT SOURCE]"


def _bounded_runtime_evidence(evaluation: dict[str, Any]) -> dict[str, Any]:
    probe = evaluation.get("probe")
    metadata = probe.get("metadata") if isinstance(probe, dict) else None
    bounded_metadata: dict[str, Any] | None = None
    if isinstance(metadata, dict):
        before = metadata.get("before")
        after = metadata.get("after")
        bounded_metadata = {
            "scene_path": metadata.get("scene_path"),
            "before": {
                key: before.get(key)
                for key in ("node_count", "class_counts", "visible_canvas_items")
                if isinstance(before, dict)
            },
            "after": {
                key: after.get(key)
                for key in ("node_count", "class_counts", "visible_canvas_items")
                if isinstance(after, dict)
            },
        }
    return {
        "hard_gate": evaluation.get("hard_gate"),
        "reason": evaluation.get("reason"),
        "source_metrics": evaluation.get("source_metrics"),
        "import": evaluation.get("import"),
        "probe": (
            {
                "return_code": probe.get("return_code"),
                "timed_out": probe.get("timed_out"),
                "metadata": bounded_metadata,
            }
            if isinstance(probe, dict)
            else None
        ),
        "image_difference": evaluation.get("image_difference"),
    }


def _judge_prompt(
    brief: str,
    evidence_a: dict[str, Any],
    source_a: str,
    evidence_b: dict[str, Any],
    source_b: str,
) -> str:
    return f"""You are the independent blind evaluator for two anonymously labeled Godot game projects created from the same empty workspace and product brief.

Evaluate only the delivered artifacts and evidence below. Do not guess which system created either project. Do not reward code volume, architectural style, or superficial feature-name mentions. Ground gameplay scores in implemented source plus runtime evidence, and visual/usability scores in the attached initial and post-input screenshots. A deterministic hard gate says whether the project imported and rendered; it is evidence, not a substitute for the four quality scores.

Score each dimension from 0 to 4:
- brief_fulfillment: 0 absent, 1 fragmentary, 2 recognizable but incomplete, 3 substantially fulfills the brief, 4 unusually complete/coherent for a compact game.
- gameplay_systems: 0 no implemented play, 1 isolated mock behavior, 2 basic playable loop with major gaps, 3 coherent loop and state transitions, 4 multiple well-integrated mechanics with robust feedback.
- visual_coherence: 0 unusable/blank, 1 severe readability problems, 2 functional but crude, 3 clear and cohesive, 4 notably polished and expressive.
- usability_and_feedback: 0 cannot understand/control, 1 very unclear, 2 minimally understandable, 3 clear controls/state/outcomes, 4 strong onboarding and moment-to-moment feedback.

Choose pairwise_preference as A, B, or TIE based on the stronger usable game overall. Keep each reason concise and evidence-based.

Return only one strict JSON object with exactly this shape:
{{
  "A": {{"brief_fulfillment": 0, "gameplay_systems": 0, "visual_coherence": 0, "usability_and_feedback": 0, "reason": "..."}},
  "B": {{"brief_fulfillment": 0, "gameplay_systems": 0, "visual_coherence": 0, "usability_and_feedback": 0, "reason": "..."}},
  "pairwise_preference": "A",
  "preference_reason": "...",
  "confidence": "low|medium|high"
}}

PRODUCT BRIEF:
{brief}

CANDIDATE A DETERMINISTIC EVIDENCE:
{json.dumps(evidence_a, indent=2, sort_keys=True)}

CANDIDATE A SOURCE EXCERPT:
{source_a}

CANDIDATE B DETERMINISTIC EVIDENCE:
{json.dumps(evidence_b, indent=2, sort_keys=True)}

CANDIDATE B SOURCE EXCERPT:
{source_b}
"""


def _parse_judgment(message: str) -> dict[str, Any]:
    candidate = message.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    payload = json.loads(candidate)
    if set(payload) != {"A", "B", "pairwise_preference", "preference_reason", "confidence"}:
        raise ValueError("judge result has unexpected top-level keys")
    for label in ("A", "B"):
        record = payload[label]
        if not isinstance(record, dict) or set(record) != {*DIMENSIONS, "reason"}:
            raise ValueError(f"judge result has unexpected {label} keys")
        for dimension in DIMENSIONS:
            score = record[dimension]
            if not isinstance(score, int) or not 0 <= score <= 4:
                raise ValueError(f"judge score is out of range: {label}.{dimension}")
        if not isinstance(record["reason"], str) or not record["reason"].strip():
            raise ValueError(f"judge result has no reason for {label}")
    if payload["pairwise_preference"] not in {"A", "B", "TIE"}:
        raise ValueError("judge preference is invalid")
    if payload["confidence"] not in {"low", "medium", "high"}:
        raise ValueError("judge confidence is invalid")
    return payload


def _run_judge(
    *,
    task: dict[str, Any],
    label_rows: dict[str, dict[str, Any]],
    destination: Path,
    agent: Path,
    model: str,
    effort: str,
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="open-game-blind-judge-") as directory:
        neutral = Path(directory)
        image_paths: list[Path] = []
        evidence: dict[str, dict[str, Any]] = {}
        sources: dict[str, str] = {}
        for label in ("A", "B"):
            row = label_rows[label]
            evaluation = row["evaluation"]
            evidence[label] = _bounded_runtime_evidence(evaluation)
            workspace = Path(row["workspace"]).resolve(strict=True)
            sources[label] = _source_excerpt(workspace)
            for phase, key in (
                ("initial", "initial_screenshot"),
                ("post-input", "post_input_screenshot"),
            ):
                value = evaluation.get(key)
                if isinstance(value, str) and Path(value).is_file():
                    target = neutral / f"{label}-{phase}.png"
                    shutil.copy2(value, target)
                    image_paths.append(target)
        prompt = _judge_prompt(
            str(task["brief"]),
            evidence["A"],
            sources["A"],
            evidence["B"],
            sources["B"],
        )
        (destination / "judge-prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        command = [
            str(agent),
            "-a",
            "never",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "-m",
            model,
            "-s",
            "read-only",
            "-C",
            str(neutral),
            "-c",
            f'model_reasoning_effort="{effort}"',
        ]
        for image_path in image_paths:
            command.extend(("--image", str(image_path)))
        command.extend(("--", prompt))
        started = time.monotonic()
        completed = subprocess.run(
            command,
            env=_credential_scrubbed_environment(),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        duration = round(time.monotonic() - started, 3)
    (destination / "judge.jsonl").write_text(completed.stdout, encoding="utf-8")
    (destination / "judge.stderr.log").write_text(completed.stderr, encoding="utf-8")
    parsed_agent = _parse_agent_jsonl(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(f"judge CLI returned {completed.returncode}")
    judgment = _parse_judgment(str(parsed_agent["final_message"]))
    return {
        "schema_version": 1,
        "task_id": task["task_id"],
        "duration_seconds": duration,
        "image_count": len(image_paths),
        "agent_usage": parsed_agent["usage"],
        "judgment": judgment,
        "judge_log": str(destination / "judge.jsonl"),
        "judge_log_sha256": _sha256_file(destination / "judge.jsonl"),
    }


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _build_analysis(
    manifest: dict[str, Any],
    protocol: dict[str, Any],
    rows: list[dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    labels = manifest["judge_labels"]
    by_task_condition = {(row["task_id"], row["condition"]): row for row in rows}
    condition_scores: dict[str, dict[str, list[float]]] = {
        condition: {dimension: [] for dimension in (*DIMENSIONS, "total")}
        for condition in manifest["design"]["conditions"]
    }
    preference_counts: Counter[str] = Counter()
    task_records: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        task_id = str(task["task_id"])
        judgment = judgments[task_id]["judgment"]
        mapped_scores: dict[str, Any] = {}
        for label in ("A", "B"):
            condition = labels[task_id][label]
            score = judgment[label]
            total = sum(int(score[dimension]) for dimension in DIMENSIONS)
            mapped_scores[condition] = {**score, "total": total, "judge_label": label}
            for dimension in DIMENSIONS:
                condition_scores[condition][dimension].append(float(score[dimension]))
            condition_scores[condition]["total"].append(float(total))
        preference_label = judgment["pairwise_preference"]
        preferred_condition = (
            labels[task_id][preference_label] if preference_label in {"A", "B"} else "TIE"
        )
        preference_counts[preferred_condition] += 1
        task_records.append(
            {
                "task_id": task_id,
                "genre": task["genre"],
                "hard_gates": {
                    condition: by_task_condition[(task_id, condition)]["evaluation"]["hard_gate"]
                    for condition in manifest["design"]["conditions"]
                },
                "scores": mapped_scores,
                "pairwise_preference": preferred_condition,
                "preference_reason": judgment["preference_reason"],
                "confidence": judgment["confidence"],
            }
        )
    aggregates: dict[str, Any] = {}
    for condition in manifest["design"]["conditions"]:
        group = [row for row in rows if row["condition"] == condition]
        aggregates[condition] = {
            "attempts": len(group),
            "hard_gate_passes": sum(row["evaluation"]["hard_gate"] == "PASS" for row in group),
            "solver_successes": sum(row["solver_return_code"] == 0 for row in group),
            "duration_seconds_total": round(sum(row["duration_seconds"] for row in group), 3),
            "duration_seconds_median": round(
                statistics.median(row["duration_seconds"] for row in group), 3
            ),
            "input_tokens_total": sum(row["agent"]["usage"]["input_tokens"] for row in group),
            "tool_calls_total": sum(row["agent"]["tool_calls"] for row in group),
            "command_crash_exit_count": sum(
                row["agent"]["command_crash_exit_count"] for row in group
            ),
            "judge_score_means": {
                dimension: _mean(values)
                for dimension, values in condition_scores[condition].items()
            },
        }
    baseline = condition_scores["baseline-minimal-open"]["total"]
    official = condition_scores["official-local"]["total"]
    paired_deltas = [left - right for left, right in zip(baseline, official, strict=True)]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": {
            "valid": len(rows) == 12 and len(judgments) == 6,
            "receipt_count": len(rows),
            "judgment_count": len(judgments),
            "manifest_sha256": protocol["manifest_sha256"],
            "baseline_runtime_tree_sha256": protocol["baseline_runtime_tree_sha256"],
        },
        "design_warning": manifest["design"]["interpretation"],
        "aggregate": aggregates,
        "pairwise": {
            "preference_counts": dict(sorted(preference_counts.items())),
            "baseline_minus_official_total_score_deltas": paired_deltas,
            "mean_total_score_delta": _mean(paired_deltas),
            "median_total_score_delta": round(statistics.median(paired_deltas), 6),
        },
        "tasks": task_records,
    }


def main() -> int:
    arguments = _arguments()
    experiment = arguments.experiment.resolve(strict=True)
    manifest_path = arguments.manifest.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    protocol = json.loads((experiment / "protocol-freeze.json").read_text(encoding="utf-8"))
    complete = json.loads((experiment / "complete.json").read_text(encoding="utf-8"))
    agent = arguments.agent_executable.resolve(strict=True)
    if protocol["manifest_sha256"] != _sha256_file(manifest_path):
        raise RuntimeError("manifest differs from frozen execution protocol")
    if complete["attempts_completed"] != 12:
        raise RuntimeError("open creation experiment is incomplete")
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    if len(rows) != 12 or [row["ordinal"] for row in rows] != list(range(1, 13)):
        raise RuntimeError("receipt count/order differs from frozen schedule")
    for row in rows:
        log = Path(row["solver_log"])
        if not log.is_file() or _sha256_file(log) != row["solver_log_sha256"]:
            raise RuntimeError(f"solver log digest mismatch: {row['attempt_id']}")
    judge_protocol = {
        "schema_version": 1,
        "manifest_sha256": protocol["manifest_sha256"],
        "execution_protocol_sha256": _sha256_file(experiment / "protocol-freeze.json"),
        "analyzer_sha256": _sha256_file(Path(__file__).resolve()),
        "judge": str(agent),
        "judge_sha256": _sha256_file(agent),
        "model": manifest["execution"]["model"],
        "reasoning_effort": manifest["execution"]["reasoning_effort"],
        "dimensions": list(DIMENSIONS),
        "labels": manifest["judge_labels"],
        "condition_blinded": True,
        "trajectory_and_efficiency_blinded": True,
    }
    _write_json(experiment / "judge-protocol-freeze.json", judge_protocol)
    if arguments.prepare_only:
        print(experiment / "judge-protocol-freeze.json")
        return 0
    by_task_condition = {(row["task_id"], row["condition"]): row for row in rows}
    existing = {path.stem for path in (experiment / "judgments").glob("*.json")}
    pending = [task for task in manifest["tasks"] if task["task_id"] not in existing]
    if arguments.max_new_judges is not None:
        pending = pending[: arguments.max_new_judges]
    for index, task in enumerate(pending, start=1):
        task_id = str(task["task_id"])
        label_rows = {
            label: by_task_condition[(task_id, condition)]
            for label, condition in manifest["judge_labels"][task_id].items()
        }
        record = _run_judge(
            task=task,
            label_rows=label_rows,
            destination=experiment / "judge-artifacts" / task_id,
            agent=agent,
            model=str(manifest["execution"]["model"]),
            effort=str(manifest["execution"]["reasoning_effort"]),
        )
        _write_json(experiment / "judgments" / f"{task_id}.json", record)
        print(f"[{index}/{len(pending)}] judged {task_id}", flush=True)
    judgments = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "judgments").glob("*.json"))
    }
    if len(judgments) == 6:
        analysis = _build_analysis(manifest, protocol, rows, judgments)
        _replace_json(experiment / "analysis.json", analysis)
        print(experiment / "analysis.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
