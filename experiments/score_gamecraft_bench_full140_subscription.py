#!/usr/bin/env python3
"""Score a full140 GameCraft experiment with the disclosed subscription judge.

Replay, frame capture, rubric aggregation, and the score formula are the pinned
official GameCraft-Bench implementation.  Only the judge transport differs from
the public leaderboard: this runner invokes GPT-5.5 through Codex subscription
authentication.  Receipts can be deterministically sharded across workers.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import score_gamecraft_bench_subscription as shared


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/gamecraft-bench-full140-unified-native-open-run1"
DEFAULT_BENCHMARK = ROOT / "third_party/gamecraft-bench"
DEFAULT_AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--agent", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--task-id")
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--max-new-demos", type=int)
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args()


def _score_task(
    *, receipt: dict[str, Any], experiment: Path, benchmark: Path, agent: Path,
    model: str, reasoning_effort: str, max_new_demos: int | None,
    official: dict[str, Any],
) -> dict[str, Any]:
    task_id = str(receipt["task_id"])
    attempt_id = str(receipt["attempt_id"])
    attempt = experiment / "attempts" / attempt_id
    evidence = attempt / "official-evidence"
    rubric = json.loads(
        (benchmark / "tasks" / task_id / "tests/rubric.json").read_text(encoding="utf-8")
    )
    replay = json.loads((evidence / "breakdown.json").read_text(encoding="utf-8"))
    if not replay["build_ok"] or replay["errors"]:
        raise RuntimeError(f"official replay evidence is not clean for {task_id}")
    output = attempt / f"subscription-judge-{model}"
    output.mkdir(parents=True, exist_ok=True)
    pending_budget = max_new_demos
    newly_judged = 0
    per_demo: dict[str, dict[str, float]] = {}
    rationales: dict[str, dict[str, str]] = {}
    demo_metadata: dict[str, dict[str, Any]] = {}
    for demo in replay["demos"]:
        demo_id = str(demo["demo_id"])
        demo_output = output / "demos" / demo_id
        result_path = demo_output / "result.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            if pending_budget is not None and pending_budget <= 0:
                continue
            frames = shared._select_frames(
                sorted((evidence / "demos" / demo_id / "frames").glob("frame_*.png"))
            )
            if not frames:
                raise RuntimeError(f"no official replay frames for {task_id}/{demo_id}")
            result = shared._judge_demo(
                agent=agent,
                model=model,
                reasoning_effort=reasoning_effort,
                workdir=evidence,
                frames=frames,
                requirements=rubric["requirements"],
                official=official,
                destination=demo_output,
            )
            shared._write_json(result_path, result)
            newly_judged += 1
            if pending_budget is not None:
                pending_budget -= 1
        per_demo[demo_id] = result["scores"]
        rationales[demo_id] = result["rationales"]
        demo_metadata[demo_id] = {
            key: result[key]
            for key in ("frame_count", "prompt_sha256", "elapsed_seconds", "return_code")
        }
    if len(per_demo) != len(replay["demos"]):
        return {
            "complete": False,
            "task_id": task_id,
            "judged_demos": len(per_demo),
            "newly_judged_demos": newly_judged,
        }

    variables: dict[str, float] = {"BUILD": 1.0}
    requirements_output: list[dict[str, Any]] = []
    for requirement in rubric["requirements"]:
        identifier = str(requirement["id"])
        values = {demo_id: scores[identifier] for demo_id, scores in per_demo.items()}
        aggregated = shared._aggregate(values, str(requirement.get("agg", "max")))
        variables[identifier] = aggregated
        requirements_output.append(
            {
                "id": identifier,
                "description": requirement["description"],
                "agg": requirement.get("agg", "max"),
                "per_demo": values,
                "aggregated": aggregated,
            }
        )
    reward = max(
        0.0,
        min(1.0, float(official["safe_eval_formula"](rubric["score_formula"], variables))),
    )
    categories = {
        category["name"]: sum(variables[item] for item in category["items"])
        / len(category["items"])
        for category in rubric["categories"]
    }
    result = {
        "schema_version": 1,
        "complete": True,
        "strict_leaderboard_comparable": False,
        "non_comparability_reason": (
            "GPT-5.5 used through Codex subscription rather than the official OpenAI "
            "chat-completions API; official replay, frames, rubric, aggregation, and formula retained."
        ),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "build_ok": True,
        "judge": {
            "transport": "codex-subscription",
            "model": model,
            "reasoning_effort": reasoning_effort,
        },
        "reward": reward,
        "formula": rubric["score_formula"],
        "variables": variables,
        "categories": categories,
        "requirements": requirements_output,
        "demos": demo_metadata,
        "newly_judged_demos": newly_judged,
        "rationales": rationales,
        "scored_at": datetime.now(UTC).isoformat(),
    }
    shared._write_json(output / "breakdown.json", result)
    return result


def main() -> int:
    arguments = _arguments()
    if arguments.worker_count < 1 or not 0 <= arguments.worker_index < arguments.worker_count:
        raise ValueError("worker-index must be in [0, worker-count)")
    if arguments.max_new_demos is not None and arguments.max_new_demos < 0:
        raise ValueError("max-new-demos must be nonnegative")
    experiment = arguments.experiment.resolve(strict=True)
    benchmark = arguments.benchmark_root.resolve(strict=True)
    agent = arguments.agent.resolve(strict=True)
    official = shared._load_official(benchmark)
    protocol = {
        "schema_version": 1,
        "runner": str(Path(__file__).resolve()),
        "runner_sha256": shared._sha256(Path(__file__).resolve()),
        "benchmark_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=benchmark,
            capture_output=True, text=True, check=True,
        ).stdout.strip(),
        "agent": str(agent),
        "agent_sha256": shared._sha256(agent),
        "model": arguments.model,
        "reasoning_effort": arguments.reasoning_effort,
        "frame_selection": "official samples in temporal order, evenly cap to 40",
        "judge_prompt": "official system instruction and requirement prompt",
        "strict_leaderboard_comparable": False,
        "sharding": "(ordinal - 1) modulo worker_count; worker count does not affect task evidence",
    }
    shared._freeze_json(experiment / "subscription-judge-protocol.json", protocol)
    if arguments.summary_only:
        shared._write_summary(experiment, arguments.model)
        return 0

    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
    ]
    receipts = [
        item for item in receipts
        if (int(item["ordinal"]) - 1) % arguments.worker_count == arguments.worker_index
    ]
    if arguments.task_id:
        receipts = [item for item in receipts if item["task_id"] == arguments.task_id]
    remaining = arguments.max_new_demos
    for receipt in receipts:
        result = _score_task(
            receipt=receipt,
            experiment=experiment,
            benchmark=benchmark,
            agent=agent,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            max_new_demos=remaining,
            official=official,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        if remaining is not None:
            remaining = max(0, remaining - int(result.get("newly_judged_demos", 0)))
    shared._write_summary(experiment, arguments.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
