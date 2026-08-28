#!/usr/bin/env python3
"""Repeat three representative GameCraft judge runs without changing the frozen scorer."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from score_gamecraft_bench_subscription import (
    DEFAULT_AGENT,
    DEFAULT_BENCHMARK,
    DEFAULT_EXPERIMENT,
    _aggregate,
    _freeze_json,
    _judge_demo,
    _load_official,
    _select_frames,
    _sha256,
    _write_json,
)


TASK_IDS = (
    "idle-spell-tower",
    "roguelike-dice-throne",
    "simulation-space-station",
)
MODEL = "gpt-5.5"
REASONING_EFFORT = "low"


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> int:
    experiment = DEFAULT_EXPERIMENT.resolve(strict=True)
    benchmark = DEFAULT_BENCHMARK.resolve(strict=True)
    agent = DEFAULT_AGENT.resolve(strict=True)
    official = _load_official(benchmark)
    output = experiment / "subscription-judge-repeatability-gpt-5.5-run2"
    output.mkdir(parents=True, exist_ok=True)

    protocol = {
        "schema_version": 1,
        "runner": str(Path(__file__).resolve()),
        "runner_sha256": _sha256(Path(__file__).resolve()),
        "task_ids": list(TASK_IDS),
        "selection": "one high-score, one middle-score, and one low-score family15 task",
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "transport": "codex-subscription",
        "frame_selection": "same official replay frames and evenly capped-to-40 rule as run1",
        "strict_leaderboard_comparable": False,
    }
    _freeze_json(output / "protocol.json", protocol)

    receipts = {
        item["task_id"]: item
        for item in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((experiment / "receipts").glob("*.json"))
        )
    }
    task_results: list[dict[str, object]] = []
    for task_id in TASK_IDS:
        receipt = receipts[task_id]
        attempt_id = f"{int(receipt['ordinal']):02d}-{task_id}"
        attempt = experiment / "attempts" / attempt_id
        evidence = attempt / "official-evidence"
        rubric = json.loads(
            (benchmark / "tasks" / task_id / "tests/rubric.json").read_text(
                encoding="utf-8"
            )
        )
        evidence_breakdown = json.loads(
            (evidence / "breakdown.json").read_text(encoding="utf-8")
        )
        run1 = json.loads(
            (attempt / f"subscription-judge-{MODEL}" / "breakdown.json").read_text(
                encoding="utf-8"
            )
        )
        task_output = output / task_id
        task_output.mkdir(parents=True, exist_ok=True)
        per_demo: dict[str, dict[str, float]] = {}
        frame_sets: dict[str, list[str]] = {}
        for demo in evidence_breakdown["demos"]:
            demo_id = str(demo["demo_id"])
            frames = _select_frames(
                sorted((evidence / "demos" / demo_id / "frames").glob("frame_*.png"))
            )
            result = _judge_demo(
                agent=agent,
                model=MODEL,
                reasoning_effort=REASONING_EFFORT,
                workdir=evidence,
                frames=frames,
                requirements=rubric["requirements"],
                official=official,
                destination=task_output / "demos" / demo_id,
            )
            _write_json(task_output / "demos" / demo_id / "result.json", result)
            per_demo[demo_id] = result["scores"]
            frame_sets[demo_id] = result["frame_sha256"]

        variables: dict[str, float] = {"BUILD": 1.0}
        for requirement in rubric["requirements"]:
            identifier = str(requirement["id"])
            values = {demo_id: scores[identifier] for demo_id, scores in per_demo.items()}
            variables[identifier] = _aggregate(
                values, str(requirement.get("agg", "max"))
            )
        reward = max(
            0.0,
            min(
                1.0,
                float(official["safe_eval_formula"](rubric["score_formula"], variables)),
            ),
        )
        categories = {
            category["name"]: sum(variables[item] for item in category["items"])
            / len(category["items"])
            for category in rubric["categories"]
        }
        result = {
            "schema_version": 1,
            "task_id": task_id,
            "run1_reward": run1["reward"],
            "run2_reward": reward,
            "reward_delta": reward - run1["reward"],
            "absolute_reward_delta": abs(reward - run1["reward"]),
            "run1_categories": run1["categories"],
            "run2_categories": categories,
            "category_deltas": {
                name: categories[name] - run1["categories"][name] for name in categories
            },
            "variables": variables,
            "frame_set_sha256": _text_sha256(
                json.dumps(frame_sets, sort_keys=True, separators=(",", ":"))
            ),
            "completed_at": datetime.now(UTC).isoformat(),
        }
        _write_json(task_output / "comparison.json", result)
        task_results.append(result)
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "run1_percent": 100.0 * run1["reward"],
                    "run2_percent": 100.0 * reward,
                    "delta_points": 100.0 * (reward - run1["reward"]),
                },
                sort_keys=True,
            ),
            flush=True,
        )

    absolute_deltas = [float(item["absolute_reward_delta"]) for item in task_results]
    summary = {
        "schema_version": 1,
        "task_count": len(task_results),
        "mean_absolute_delta_points": 100.0 * sum(absolute_deltas) / len(absolute_deltas),
        "max_absolute_delta_points": 100.0 * max(absolute_deltas),
        "tasks": task_results,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _write_json(output / "summary.json", summary)
    print(json.dumps({key: summary[key] for key in (
        "task_count", "mean_absolute_delta_points", "max_absolute_delta_points"
    )}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
