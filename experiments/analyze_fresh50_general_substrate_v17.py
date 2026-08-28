#!/usr/bin/env python3
"""Compare the frozen v17 general substrate run with Official and v16."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from experiments.analyze_fresh50_official_v4 import SAMPLES, _percentile

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260817
FAILURE_GROUPS = {
    "visual_geometry": {
        "task_0177",
        "task_0233",
        "task_0241",
        "task_0185",
        "task_0178",
        "task_0131",
        "task_0226",
        "task_0087",
        "task_0081",
    },
    "behavior_exact_requirement": {
        "task_0132",
        "task_0057",
        "task_0048",
        "task_0089",
        "task_0095",
        "task_0055",
        "task_0098",
        "task_0054",
    },
    "resource_dependency": {"task_0031"},
}
CAPTURE_FIDELITY_IMPLICATED = {"task_0233", "task_0226"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--v17-experiment",
        type=Path,
        default=ROOT
        / "runs/experiments/"
        "gamedevbench-fresh50-programmable-open-general-substrate-v17-run1",
    )
    parser.add_argument(
        "--v16-experiment",
        type=Path,
        default=ROOT
        / "runs/experiments/"
        "gamedevbench-fresh50-programmable-open-general-workspace-v16-run1",
    )
    return parser.parse_args()


def _bootstrap(deltas: list[float], *, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    samples = [
        statistics.fmean(deltas[rng.randrange(len(deltas))] for _ in deltas)
        for _ in range(SAMPLES)
    ]
    return {
        "estimate": statistics.fmean(deltas),
        "ci95": [_percentile(samples, 0.025), _percentile(samples, 0.975)],
        "method": "paired task bootstrap percentile interval",
        "samples": SAMPLES,
        "seed": seed,
    }


def _mcnemar(first: list[bool], second: list[bool]) -> dict[str, Any]:
    first_only = sum(a and not b for a, b in zip(first, second, strict=True))
    second_only = sum(b and not a for a, b in zip(first, second, strict=True))
    discordant = first_only + second_only
    if discordant == 0:
        probability = 1.0
    else:
        tail = sum(
            math.comb(discordant, index)
            for index in range(min(first_only, second_only) + 1)
        ) / (2**discordant)
        probability = min(1.0, 2 * tail)
    return {
        "first_only": first_only,
        "second_only": second_only,
        "discordant": discordant,
        "two_sided_exact_p": probability,
    }


def _paired(
    first: dict[str, bool],
    second: dict[str, bool],
    order: list[str],
    *,
    seed: int,
) -> dict[str, Any]:
    first_values = [first[task_id] for task_id in order]
    second_values = [second[task_id] for task_id in order]
    deltas = [
        float(a) - float(b)
        for a, b in zip(first_values, second_values, strict=True)
    ]
    return {
        "difference": _bootstrap(deltas, seed=seed),
        "mcnemar": _mcnemar(first_values, second_values),
        "first_only_pass": [
            task_id
            for task_id in order
            if first[task_id] and not second[task_id]
        ],
        "second_only_pass": [
            task_id
            for task_id in order
            if second[task_id] and not first[task_id]
        ],
    }


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _workspace_timeout_audit(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    calls = 0
    likely_timeouts: list[dict[str, Any]] = []
    tasks_using_workspace: set[str] = set()
    for row in rows:
        task_id = str(row["task_id"])
        run_directory = Path(row["record"]["run_directory"])
        trace = json.loads(
            (run_directory / "agent-result.json").read_text(encoding="utf-8")
        )["trace"]
        declarations = [
            (
                int(item["turn"]),
                int(item["decision"].get("timeout_seconds") or 120),
            )
            for item in trace
            if item.get("event") == "model_program_decision"
            and item.get("decision", {}).get("kind") == "workspace"
        ]
        if declarations:
            tasks_using_workspace.add(task_id)
        events = [
            json.loads(line)
            for line in (run_directory / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        starts = [
            event
            for event in events
            if event["kind"] == "capability_started"
            and event["payload"].get("name") == "run_workspace_program"
        ]
        completed = [
            event
            for event in events
            if event["kind"] in {"capability_completed", "capability_failed"}
            and event["payload"].get("name") == "run_workspace_program"
        ]
        calls += len(starts)
        for index, (start, end) in enumerate(zip(starts, completed, strict=True)):
            elapsed = (
                _parse_time(end["occurred_at"])
                - _parse_time(start["occurred_at"])
            ).total_seconds()
            _, declared = declarations[index]
            if elapsed >= declared - 0.5:
                likely_timeouts.append(
                    {
                        "task_id": task_id,
                        "call_ordinal": index + 1,
                        "declared_timeout_seconds": declared,
                        "elapsed_seconds": round(elapsed, 3),
                    }
                )
    return {
        "calls": calls,
        "tasks_using_workspace": len(tasks_using_workspace),
        "timeout_calls": len(likely_timeouts),
        "timeout_tasks": sorted({item["task_id"] for item in likely_timeouts}),
        "timeout_detail": likely_timeouts,
        "classification_note": (
            "A call is classified as a timeout when Host elapsed time reaches the model's "
            "declared timeout. The structured observation itself is intentionally not copied "
            "into the public event payload."
        ),
    }


def _decision_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions: Counter[str] = Counter()
    capabilities: Counter[str] = Counter()
    transport_attempts: Counter[int] = Counter()
    tasks_with_transport_retry: set[str] = set()
    for row in rows:
        task_id = str(row["task_id"])
        run_directory = Path(row["record"]["run_directory"])
        trace = json.loads(
            (run_directory / "agent-result.json").read_text(encoding="utf-8")
        )["trace"]
        for item in trace:
            attempts = int(item.get("transport_attempts", 1))
            transport_attempts[attempts] += 1
            if attempts > 1:
                tasks_with_transport_retry.add(task_id)
            if item.get("event") != "model_program_decision":
                continue
            decision = item["decision"]
            kind = str(decision["kind"])
            decisions[kind] += 1
            if kind == "capability":
                capabilities[str(decision["capability"])] += 1
    action_decisions = sum(
        decisions[kind] for kind in ("capability", "workspace", "cell")
    )
    return {
        "decisions": dict(sorted(decisions.items())),
        "capabilities": dict(capabilities.most_common()),
        "first_class_capability_share_of_actions": (
            decisions["capability"] / action_decisions
        ),
        "transport_attempts": {
            str(key): value for key, value in sorted(transport_attempts.items())
        },
        "tasks_with_transport_retry": sorted(tasks_with_transport_retry),
    }


def _visual_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    contexts: Counter[str] = Counter()
    gate_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        record = row["record"]
        gate_outcomes[str(record["visual_gate"])][str(row["normalized_status"])] += 1
        manifest_path = Path(record["run_directory"]) / "public-asset-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("derived_context", []):
            contexts[str(item.get("kind", "unknown"))] += 1
    return {
        "derived_contexts": dict(sorted(contexts.items())),
        "visual_gate_by_official_outcome": {
            gate: dict(sorted(outcomes.items()))
            for gate, outcomes in sorted(gate_outcomes.items())
        },
        "capture_fidelity_implicated_failures": sorted(CAPTURE_FIDELITY_IMPLICATED),
    }


def _resource_delta(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_duration = first["duration_seconds"]
    second_duration = second["duration_seconds"]
    return {
        "duration_total_fraction": (
            first_duration["total"] / second_duration["total"] - 1
        ),
        "duration_median_fraction": (
            first_duration["median"] / second_duration["median"] - 1
        ),
        "duration_p95_fraction": first_duration["p95"] / second_duration["p95"] - 1,
        "model_turns_fraction": first["model_turns"] / second["model_turns"] - 1,
        "tool_calls_fraction": first["tool_calls"] / second["tool_calls"] - 1,
        "input_tokens_fraction": (
            first["tokens"]["input_tokens"] / second["tokens"]["input_tokens"] - 1
        ),
    }


def main() -> int:
    arguments = _arguments()
    v17_experiment = arguments.v17_experiment.resolve(strict=True)
    v16_experiment = arguments.v16_experiment.resolve(strict=True)
    v17_analysis = json.loads(
        (v17_experiment / "analysis.json").read_text(encoding="utf-8")
    )
    v16_analysis = json.loads(
        (v16_experiment / "retry-adjusted-analysis.json").read_text(encoding="utf-8")
    )
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((v17_experiment / "receipts").glob("*.json"))
    ]
    order = [str(row["task_id"]) for row in rows]
    v17 = {
        str(row["task_id"]): row["normalized_status"] == "PASS" for row in rows
    }
    official = {
        str(row["task_id"]): row["official"] == "PASS"
        for row in v16_analysis["tasks"]
    }
    v16_adjusted = {
        str(row["task_id"]): row["retry_adjusted"] == "PASS"
        for row in v16_analysis["tasks"]
    }
    v16_strict_rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((v16_experiment / "receipts").glob("*.json"))
    ]
    v16_strict = {
        str(row["task_id"]): row["normalized_status"] == "PASS"
        for row in v16_strict_rows
    }
    failure_ids = {task_id for task_id in order if not v17[task_id]}
    classified_ids = set().union(*FAILURE_GROUPS.values())
    if failure_ids != classified_ids:
        raise RuntimeError(
            f"failure classification drift: failures={sorted(failure_ids)} "
            f"classified={sorted(classified_ids)}"
        )

    condition = v17_analysis["inputs"]["condition"]
    v17_resources = v17_analysis["overall"][condition]["resources"]
    v16_resources = v16_analysis["retry_adjusted"]["summary"]["resources"]
    result = {
        "schema_version": 1,
        "validation": {
            "v17_analysis_valid": v17_analysis["validation"]["valid"],
            "v17_receipts": len(rows),
            "v17_blocked": sum(
                row["normalized_status"] == "BLOCKED" for row in rows
            ),
            "failure_classification_complete": failure_ids == classified_ids,
        },
        "headline": {
            "official": {
                "passes": sum(official.values()),
                "pass_rate": statistics.fmean(official.values()),
            },
            "v16_strict": {
                "passes": sum(v16_strict.values()),
                "pass_rate": statistics.fmean(v16_strict.values()),
            },
            "v16_retry_adjusted": {
                "passes": sum(v16_adjusted.values()),
                "pass_rate": statistics.fmean(v16_adjusted.values()),
            },
            "v17": {
                "passes": sum(v17.values()),
                "pass_rate": statistics.fmean(v17.values()),
            },
        },
        "paired": {
            "v17_vs_official": _paired(v17, official, order, seed=SEED),
            "v17_vs_v16_retry_adjusted": _paired(
                v17, v16_adjusted, order, seed=SEED + 1
            ),
            "v17_vs_v16_strict": _paired(v17, v16_strict, order, seed=SEED + 2),
        },
        "by_stratum": {
            "official": v17_analysis["overall"]["official-default"]["by_stratum"],
            "v16_retry_adjusted": v16_analysis["retry_adjusted"]["summary"]["by_stratum"],
            "v17": v17_analysis["overall"][condition]["by_stratum"],
        },
        "resources": {
            "v16_retry_adjusted_effective": v16_resources,
            "v17": v17_resources,
            "v17_fractional_change_vs_v16_retry_adjusted": _resource_delta(
                v17_resources, v16_resources
            ),
        },
        "decision_substrate": _decision_audit(rows),
        "workspace": _workspace_timeout_audit(rows),
        "visual": _visual_audit(rows),
        "failure_audit": {
            "groups": {
                name: {"count": len(task_ids), "task_ids": sorted(task_ids)}
                for name, task_ids in FAILURE_GROUPS.items()
            },
            "preservation_failures": [
                str(row["task_id"])
                for row in rows
                if row["record"]["preservation_gate"] == "FAIL"
            ],
        },
    }
    output = v17_experiment / "comparative-analysis.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result["headline"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
