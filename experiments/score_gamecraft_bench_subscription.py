#!/usr/bin/env python3
"""Score preserved GameCraft replay frames with the Codex subscription judge.

This is intentionally a disclosed judge-transport approximation: replay, frame
sampling, rubric, aggregation, and score formula are official GameCraft-Bench;
the multimodal GPT-5.5 call uses Codex subscription auth rather than the public
OpenAI chat-completions API used by the leaderboard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/gamecraft-bench-family15-unified-native-open-run1"
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
    parser.add_argument("--max-new-demos", type=int)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _freeze_json(path: Path, payload: object) -> None:
    canonical = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != canonical:
        raise RuntimeError(f"frozen subscription-judge protocol differs: {path}")
    if not path.exists():
        path.write_text(canonical, encoding="utf-8")


def _load_official(benchmark: Path) -> dict[str, Any]:
    sys.path.insert(0, str(benchmark))
    from gamecraft_bench.verifier.judges import _common  # type: ignore[import-not-found]
    from gamecraft_bench.verifier.judges.base import RequirementSpec  # type: ignore[import-not-found]
    from gamecraft_bench.verifier.score import _safe_eval_formula  # type: ignore[import-not-found]

    return {
        "common": _common,
        "RequirementSpec": RequirementSpec,
        "safe_eval_formula": _safe_eval_formula,
    }


def _schema(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    identifiers = [str(item["id"]) for item in requirements]
    score_properties = {
        identifier: {"type": "number", "minimum": 0.0, "maximum": 1.0}
        for identifier in identifiers
    }
    rationale_properties = {identifier: {"type": "string"} for identifier in identifiers}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["scores", "rationales"],
        "properties": {
            "scores": {
                "type": "object",
                "additionalProperties": False,
                "required": identifiers,
                "properties": score_properties,
            },
            "rationales": {
                "type": "object",
                "additionalProperties": False,
                "required": identifiers,
                "properties": rationale_properties,
            },
        },
    }


def _prompt(official: dict[str, Any], requirements: list[dict[str, Any]], count: int) -> str:
    RequirementSpec = official["RequirementSpec"]
    specs = [
        RequirementSpec(id=str(item["id"]), description=str(item["description"]))
        for item in requirements
    ]
    common = official["common"]
    return "\n\n".join(
        (
            common.SYSTEM_INSTRUCTION,
            (
                f"The attached {count} images are PNG frames sampled in temporal order "
                "from one playthrough of a Godot 2D game. Evaluate only the visible "
                "recording evidence. Do not inspect files or use tools."
            ),
            common.build_user_prompt(specs),
        )
    )


def _select_frames(paths: list[Path]) -> list[Path]:
    if len(paths) <= 40:
        return paths
    step = len(paths) / 40.0
    return [paths[min(int(index * step), len(paths) - 1)] for index in range(40)]


def _judge_demo(
    *, agent: Path, model: str, reasoning_effort: str, workdir: Path,
    frames: list[Path], requirements: list[dict[str, Any]], official: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    schema_path = destination / "output-schema.json"
    response_path = destination / "response.json"
    prompt = _prompt(official, requirements, len(frames))
    _write_json(schema_path, _schema(requirements))
    command = [
        str(agent), "-a", "never", "exec", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--skip-git-repo-check", "--json", "--color", "never",
        "-s", "read-only", "-C", str(workdir), "-m", model,
        "-c", f'model_reasoning_effort="{reasoning_effort}"',
        "-i", *[str(path) for path in frames],
        "--output-schema", str(schema_path), "-o", str(response_path), prompt,
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=1800, check=False
    )
    (destination / "events.jsonl").write_text(completed.stdout, encoding="utf-8")
    (destination / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not response_path.is_file():
        raise RuntimeError(
            f"subscription judge failed (rc={completed.returncode}): {completed.stderr[-1000:]}"
        )
    response = json.loads(response_path.read_text(encoding="utf-8"))
    identifiers = [str(item["id"]) for item in requirements]
    scores = {
        identifier: max(0.0, min(1.0, float(response["scores"][identifier])))
        for identifier in identifiers
    }
    rationales = {
        identifier: str(response["rationales"][identifier]) for identifier in identifiers
    }
    return {
        "scores": scores,
        "rationales": rationales,
        "frame_count": len(frames),
        "frame_sha256": [_sha256(path) for path in frames],
        "prompt_sha256": _text_sha256(prompt),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "return_code": completed.returncode,
    }


def _aggregate(values: dict[str, float], method: str) -> float:
    if not values:
        return 0.0
    if method == "mean":
        return sum(values.values()) / len(values)
    return max(values.values())


def _score_task(
    *, receipt: dict[str, Any], experiment: Path, benchmark: Path, agent: Path,
    model: str, reasoning_effort: str, max_new_demos: int | None,
    official: dict[str, Any],
) -> dict[str, Any]:
    task_id = str(receipt["task_id"])
    attempt_id = f"{int(receipt['ordinal']):02d}-{task_id}"
    attempt = experiment / "attempts" / attempt_id
    evidence = attempt / "official-evidence"
    rubric = json.loads(
        (benchmark / "tasks" / task_id / "tests/rubric.json").read_text(encoding="utf-8")
    )
    breakdown = json.loads((evidence / "breakdown.json").read_text(encoding="utf-8"))
    if not breakdown["build_ok"] or breakdown["errors"]:
        raise RuntimeError(f"official replay evidence is not clean for {task_id}")
    output = attempt / f"subscription-judge-{model}"
    output.mkdir(parents=True, exist_ok=True)
    pending_budget = max_new_demos
    newly_judged = 0
    per_demo: dict[str, dict[str, float]] = {}
    rationales: dict[str, dict[str, str]] = {}
    demo_metadata: dict[str, dict[str, Any]] = {}
    for demo in breakdown["demos"]:
        demo_id = str(demo["demo_id"])
        demo_output = output / "demos" / demo_id
        result_path = demo_output / "result.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            if pending_budget is not None and pending_budget <= 0:
                continue
            frames = _select_frames(
                sorted((evidence / "demos" / demo_id / "frames").glob("frame_*.png"))
            )
            if not frames:
                raise RuntimeError(f"no official replay frames for {task_id}/{demo_id}")
            result = _judge_demo(
                agent=agent,
                model=model,
                reasoning_effort=reasoning_effort,
                workdir=evidence,
                frames=frames,
                requirements=rubric["requirements"],
                official=official,
                destination=demo_output,
            )
            _write_json(result_path, result)
            newly_judged += 1
            if pending_budget is not None:
                pending_budget -= 1
        per_demo[demo_id] = result["scores"]
        rationales[demo_id] = result["rationales"]
        demo_metadata[demo_id] = {
            key: result[key]
            for key in ("frame_count", "prompt_sha256", "elapsed_seconds", "return_code")
        }
    if len(per_demo) != len(breakdown["demos"]):
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
        aggregated = _aggregate(values, str(requirement.get("agg", "max")))
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
    _write_json(output / "breakdown.json", result)
    return result


def _write_summary(experiment: Path, model: str) -> None:
    breakdowns = sorted(
        (experiment / "attempts").glob(f"*/subscription-judge-{model}/breakdown.json")
    )
    results = [json.loads(path.read_text(encoding="utf-8")) for path in breakdowns]
    complete = [item for item in results if item.get("complete") is True]
    category_names = sorted({name for item in complete for name in item["categories"]})
    payload = {
        "schema_version": 1,
        "strict_leaderboard_comparable": False,
        "completed_tasks": len(complete),
        "overall_percent": (
            100.0 * sum(item["reward"] for item in complete) / len(complete) if complete else None
        ),
        "category_percent": {
            name: 100.0 * sum(item["categories"][name] for item in complete) / len(complete)
            for name in category_names
        },
        "tasks": [
            {
                "task_id": item["task_id"],
                "reward_percent": 100.0 * item["reward"],
                "categories_percent": {
                    name: 100.0 * value for name, value in item["categories"].items()
                },
            }
            for item in complete
        ],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _write_json(experiment / f"subscription-judge-{model}-summary.json", payload)


def main() -> int:
    arguments = _arguments()
    if arguments.max_new_demos is not None and arguments.max_new_demos < 0:
        raise ValueError("max-new-demos must be nonnegative")
    experiment = arguments.experiment.resolve(strict=True)
    benchmark = arguments.benchmark_root.resolve(strict=True)
    agent = arguments.agent.resolve(strict=True)
    official = _load_official(benchmark)
    protocol = {
        "schema_version": 1,
        "runner": str(Path(__file__).resolve()),
        "runner_sha256": _sha256(Path(__file__).resolve()),
        "benchmark_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=benchmark,
            capture_output=True, text=True, check=True,
        ).stdout.strip(),
        "agent": str(agent),
        "agent_sha256": _sha256(agent),
        "model": arguments.model,
        "reasoning_effort": arguments.reasoning_effort,
        "frame_selection": "official samples in temporal order, evenly cap to 40",
        "judge_prompt": "official system instruction and requirement prompt",
        "strict_leaderboard_comparable": False,
    }
    _freeze_json(experiment / "subscription-judge-protocol.json", protocol)
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((experiment / "receipts").glob("*.json"))
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
    _write_summary(experiment, arguments.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
