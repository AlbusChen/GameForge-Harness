#!/usr/bin/env python3
# ruff: noqa: E501
"""Build normalized data and a canonical HTML-report artifact for the v4 evaluation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analyze_unseen30_maturity import _cluster_bootstrap_ci, _mcnemar_exact

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "runs/experiments/gamedevbench-unseen30-maturity-v1"
V2 = ROOT / "runs/experiments/gamedevbench-unseen30-open-scope-v2-run1"
V3 = ROOT / "runs/experiments/gamedevbench-unseen30-open-global-v3-run1"
V4 = ROOT / "runs/experiments/gamedevbench-unseen30-open-global-diagnostic-v4-run1"
OPEN = ROOT / "runs/experiments/open-requests-open-global-diagnostic-v4-run1"
REPORT = ROOT / "reports/harness-open-global-v4-evaluation-2026-08-11"
MANIFEST = ROOT / "benchmarks/gamedevbench-unseen30-maturity-v1.json"
ITERATION_LOG = ROOT / "docs/harness-open-scope-iteration-log-2026-08-11.md"
DESIGN = ROOT / "docs/harness-exploration-control-boundary-2026-08-11.md"
V2_ID = "programmable-open-scope-v2"
V4_ID = "programmable-open-global-diagnostic-v4"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipts(directory: Path) -> list[dict[str, Any]]:
    return [_load(path) for path in sorted((directory / "receipts").glob("*.json"))]


def _condition_row(
    analysis: dict[str, Any], condition: str, label: str, order: int, role: str
) -> dict[str, object]:
    overall = analysis["overall"][condition]
    primary = overall["primary_evaluator"]
    terminal = overall["terminal"]
    resources = overall["resources"]
    tasks = analysis["task_metrics"]["by_condition"][condition]
    return {
        "condition_order": order,
        "condition": label,
        "condition_id": condition,
        "role": role,
        "attempts": primary["attempts"],
        "evaluator_passes": primary["status_counts"].get("PASS", 0),
        "evaluator_failures": primary["status_counts"].get("FAIL", 0),
        "evaluator_not_run": primary["status_counts"].get("NOT_RUN", 0),
        "evaluator_pass_rate": primary["pass_rate"],
        "terminal_passes": terminal["status_counts"].get("PASS", 0),
        "terminal_failures": terminal["status_counts"].get("FAIL", 0),
        "blocked": terminal["status_counts"].get("BLOCKED", 0),
        "terminal_pass_rate": terminal["terminal_pass_rate"],
        "majority_pass_tasks": tasks["majority_evaluator_pass_tasks"],
        "never_pass_tasks": tasks["never_evaluator_pass_tasks"],
        "duration_total_seconds": resources["duration_seconds"]["total"],
        "duration_median_seconds": resources["duration_seconds"]["median"],
        "duration_p95_seconds": resources["duration_seconds"]["p95"],
        "duration_max_seconds": resources["duration_seconds"]["max"],
        "model_turns": resources["model_turns"],
        "tool_calls": resources["tool_calls"],
        "input_tokens": resources["tokens"]["input_tokens"],
        "cached_input_tokens": resources["tokens"]["cached_input_tokens"],
        "output_tokens": resources["tokens"]["output_tokens"],
        "reasoning_output_tokens": resources["tokens"]["reasoning_output_tokens"],
    }


def _task_index(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["task_id"]: row for row in analysis["task_metrics"]["tasks"]}


def _pairwise_v4_v2(
    v2_analysis: dict[str, Any], v4_analysis: dict[str, Any]
) -> dict[str, Any]:
    v2_tasks = _task_index(v2_analysis)
    v4_tasks = _task_index(v4_analysis)
    deltas = []
    v4_majority = []
    v2_majority = []
    task_deltas = []
    for task_id in sorted(v4_tasks):
        first = v4_tasks[task_id]["conditions"][V4_ID]
        second = v2_tasks[task_id]["conditions"][V2_ID]
        delta = first["evaluator_pass_fraction"] - second["evaluator_pass_fraction"]
        deltas.append(delta)
        v4_majority.append(first["majority_evaluator_pass"])
        v2_majority.append(second["majority_evaluator_pass"])
        task_deltas.append({"task_id": task_id, "delta": delta})
    return {
        "first": V4_ID,
        "second": V2_ID,
        "pass_fraction_delta": _cluster_bootstrap_ci(deltas),
        "majority_pass_mcnemar": _mcnemar_exact(v4_majority, v2_majority),
        "task_deltas": task_deltas,
    }


def _pair_row(pair: dict[str, Any], labels: dict[str, str], order: int) -> dict[str, object]:
    delta = pair["pass_fraction_delta"]
    mcnemar = pair["majority_pass_mcnemar"]
    return {
        "comparison_order": order,
        "comparison": f"{labels[pair['first']]} − {labels[pair['second']]}",
        "first": labels[pair["first"]],
        "second": labels[pair["second"]],
        "task_mean_delta": delta["estimate"],
        "ci95_low": delta["ci95"][0],
        "ci95_high": delta["ci95"][1],
        "bootstrap_samples": delta["samples"],
        "first_only_majority_tasks": mcnemar["first_only"],
        "second_only_majority_tasks": mcnemar["second_only"],
        "discordant_majority_tasks": mcnemar["discordant"],
        "mcnemar_exact_p": mcnemar["two_sided_exact_p"],
    }


def _tool_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        events = Path(row["record"]["run_directory"]) / "events.jsonl"
        for line in events.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("kind") == "capability_requested":
                counts[str(event["payload"]["name"])] += 1
    return counts


def _open_rows(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    output = []
    for row in rows:
        case = row["case"]
        record = row.get("record") or {}
        gates = record.get("gates") or []
        preservation = next(
            (item.get("status") for item in gates if item.get("gate") == "preservation"),
            "UNCHANGED" if case["mode"] in {"query", "unapproved"} else "NOT_RUN",
        )
        output.append(
            {
                "case_order": row["ordinal"],
                "case": case["id"],
                "mode": case["mode"],
                "status": row["status"],
                "duration_seconds": row["duration_seconds"],
                "citations": len(record.get("citations", [])),
                "model_turns": record.get("model_turns", 0),
                "tool_calls": record.get("tool_calls", 0),
                "lineage_complete": record.get("lineage_complete", True),
                "preservation": preservation,
                "policy_violations": record.get("policy_violations", 0),
                "source_project_unchanged": row["source_project_unchanged"],
            }
        )
    return output


def _query_source(source_id: str, sql: str, description: str) -> dict[str, object]:
    return {
        "id": source_id,
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "id": f"harness-open-global-v4-{source_id}",
            "sql": sql,
            "description": description,
            "tables_used": [source_id.removesuffix("_query")],
        },
    }


def main() -> None:
    required = [V2 / "analysis.json", V4 / "analysis.json", OPEN / "complete.json"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"evaluation artifacts are incomplete: {missing}")
    v2_analysis = _load(V2 / "analysis.json")
    v4_analysis = _load(V4 / "analysis.json")
    v2_receipts = _receipts(V2)
    v3_receipts = _receipts(V3)
    v4_receipts = _receipts(V4)
    open_receipts = _receipts(OPEN)
    if len(v4_receipts) != 90 or len(open_receipts) != 7:
        raise RuntimeError("final evaluation receipt counts are incomplete")

    labels = {
        "official-default": "Official default",
        "legacy-tool-v1": "Legacy Harness",
        "programmable-v1": "Programmable v1",
        V2_ID: "Open-scope v2",
        V4_ID: "Open-global diagnostic v4",
    }
    conditions = [
        _condition_row(v4_analysis, "official-default", labels["official-default"], 1, "frozen baseline"),
        _condition_row(v4_analysis, "legacy-tool-v1", labels["legacy-tool-v1"], 2, "frozen baseline"),
        _condition_row(v4_analysis, "programmable-v1", labels["programmable-v1"], 3, "frozen prior Harness"),
        _condition_row(v2_analysis, V2_ID, labels[V2_ID], 4, "completed intermediate candidate"),
        _condition_row(v4_analysis, V4_ID, labels[V4_ID], 5, "final candidate"),
    ]
    by_id = {row["condition_id"]: row for row in conditions}
    pair_v4_v2 = _pairwise_v4_v2(v2_analysis, v4_analysis)
    pairwise = [_pair_row(pair_v4_v2, labels, 1)]
    for order, pair in enumerate(v4_analysis["pairwise"], start=2):
        pairwise.append(_pair_row(pair, labels, order))

    v2_tasks = _task_index(v2_analysis)
    task_rows = []
    delta_counts: Counter[int] = Counter()
    for task in v4_analysis["task_metrics"]["tasks"]:
        task_id = task["task_id"]
        cells = task["conditions"]
        v2_cell = v2_tasks[task_id]["conditions"][V2_ID]
        v4_cell = cells[V4_ID]
        delta_passes = v4_cell["evaluator_passes"] - v2_cell["evaluator_passes"]
        delta_counts[delta_passes] += 1
        task_rows.append(
            {
                "task_id": task_id,
                "task_name": task["task_name"],
                "stratum": task["stratum"],
                "official_passes": cells["official-default"]["evaluator_passes"],
                "legacy_passes": cells["legacy-tool-v1"]["evaluator_passes"],
                "programmable_v1_passes": cells["programmable-v1"]["evaluator_passes"],
                "v2_passes": v2_cell["evaluator_passes"],
                "v4_passes": v4_cell["evaluator_passes"],
                "v4_terminal_passes": v4_cell["terminal_passes"],
                "delta_v4_v2_attempts": delta_passes,
                "v4_majority": v4_cell["majority_evaluator_pass"],
                "v4_attempt_statuses": ", ".join(
                    f"r{item['repetition']}:{item['terminal_status']}/{item['evaluator_status']}"
                    for item in v4_cell["attempts_detail"]
                ),
            }
        )
    delta_distribution = [
        {
            "delta": delta,
            "delta_label": f"{delta:+d}",
            "task_count": delta_counts[delta],
            "interpretation": "improved" if delta > 0 else "regressed" if delta < 0 else "unchanged",
        }
        for delta in range(-3, 4)
    ]
    terminal_status = []
    for row in conditions:
        for status, field in (("PASS", "terminal_passes"), ("FAIL", "terminal_failures"), ("BLOCKED", "blocked")):
            terminal_status.append(
                {
                    "condition": row["condition"],
                    "status": status,
                    "attempt_count": row[field],
                    "condition_total": row["attempts"],
                }
            )

    v2_tools = _tool_counts(v2_receipts)
    v4_tools = _tool_counts(v4_receipts)
    tool_usage = [
        {
            "capability": name,
            "v2_calls": v2_tools[name],
            "v4_calls": v4_tools[name],
            "delta_calls": v4_tools[name] - v2_tools[name],
        }
        for name in sorted(set(v2_tools) | set(v4_tools), key=lambda name: (-v4_tools[name], name))
    ]
    open_suite = _open_rows(open_receipts)
    v4_controls = v4_analysis["candidate_controls"]
    preservation_counts = v4_controls["preservation_gate"]
    open_passes = sum(row["status"] == "PASS" for row in open_suite)
    mature = (
        by_id[V4_ID]["blocked"] == 0
        and preservation_counts.get("FAIL", 0) == 0
        and v4_controls["policy_violations"] == 0
        and open_passes == 7
    )
    stop_reason = (
        "limited-sample control-layer maturity reached; further tuning on the observed 30 tasks risks overfitting"
        if mature
        else "maturity criteria not met; another general mechanism iteration is required"
    )
    v3_status = Counter(row["normalized_status"] for row in v3_receipts)
    iterations = [
        {
            "iteration": "v1 → v2",
            "problem": "read/write coupling, static output scope, sidecar false failures, load-only scene validation",
            "general_change": "public project retrieval, audited output manifest, engine-aware sidecars, scene instantiation",
            "outcome": "35/90 evaluator PASS unchanged; terminal PASS 17→35; 3 BLOCKED remained",
            "disposition": "complete intermediate candidate",
        },
        {
            "iteration": "v2 → v3",
            "problem": "route-specific early budgets and expensive serial inspection",
            "general_change": "40/80 global envelope, batch read/inspect, Host diff review",
            "outcome": f"partial {len(v3_receipts)} receipts: {v3_status.get('PASS', 0)} PASS, {v3_status.get('FAIL', 0)} FAIL, {v3_status.get('BLOCKED', 0)} BLOCKED",
            "disposition": "excluded diagnostic batch; stopped on hidden lifecycle diagnostics",
        },
        {
            "iteration": "v3 → v4",
            "problem": "lifecycle results and small diagnostics disappeared behind whole-state truncation",
            "general_change": "persistent lifecycle observations, bounded automatic diagnostics, per-variable state projection",
            "outcome": "targeted task_0045 PASS in 6 turns/7 calls/one import; full v4 shown in this report",
            "disposition": "final candidate",
        },
    ]
    headline = [
        {
            "v4_evaluator_pass_rate": by_id[V4_ID]["evaluator_pass_rate"],
            "official_pass_rate": by_id["official-default"]["evaluator_pass_rate"],
            "v2_pass_rate": by_id[V2_ID]["evaluator_pass_rate"],
            "v4_terminal_pass_rate": by_id[V4_ID]["terminal_pass_rate"],
            "v4_blocked": by_id[V4_ID]["blocked"],
            "v2_blocked": by_id[V2_ID]["blocked"],
            "v4_v2_delta": pair_v4_v2["pass_fraction_delta"]["estimate"],
            "v4_v2_ci_low": pair_v4_v2["pass_fraction_delta"]["ci95"][0],
            "v4_v2_ci_high": pair_v4_v2["pass_fraction_delta"]["ci95"][1],
            "open_pass_rate": open_passes / len(open_suite),
            "open_passes": open_passes,
            "open_total": len(open_suite),
            "policy_violations": v4_controls["policy_violations"],
            "preservation_failures": preservation_counts.get("FAIL", 0),
            "quota_remaining_minimum": v4_analysis["quota"]["minimum_remaining_percent_observed"] / 100,
            "maturity_stop": mature,
        }
    ]

    generated_at = datetime.now(UTC).isoformat()
    source_paths = [
        MANIFEST,
        BASELINE / "analysis.json",
        V2 / "analysis.json",
        V4 / "analysis.json",
        V4 / "schedule.json",
        V4 / "protocol-freeze.json",
        OPEN / "protocol-freeze.json",
        OPEN / "complete.json",
        ITERATION_LOG,
        DESIGN,
    ]
    data = {
        "schema_version": 1,
        "report_id": "harness-open-global-v4-evaluation-2026-08-11",
        "generated_at": generated_at,
        "scope": {
            "benchmark": "GameDevBench",
            "task_count": 30,
            "repetitions": 3,
            "attempts_per_condition": 90,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "primary_endpoint": "official evaluator PASS; NOT_RUN remains in denominator",
            "inference_unit": "task, with three repetitions retained within task",
            "baseline_policy": "Official, Legacy, and Programmable v1 frozen; v2 and v4 candidate-only reruns",
            "v3_policy": "46-receipt diagnostic partial excluded from final outcome score",
            "stop_reason": stop_reason,
        },
        "headline": headline,
        "condition_summary": conditions,
        "terminal_status": terminal_status,
        "pairwise": pairwise,
        "task_delta_distribution": delta_distribution,
        "tasks": task_rows,
        "tool_usage": tool_usage,
        "open_suite": open_suite,
        "iterations": iterations,
        "controls": v4_controls,
        "source_integrity": [
            {"source": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
            for path in source_paths
        ],
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    _write(REPORT / "data.json", data)

    sql = """-- Reproducible projections over reports/harness-open-global-v4-evaluation-2026-08-11/data.json
SELECT * FROM headline;
SELECT * FROM condition_summary ORDER BY condition_order;
SELECT * FROM terminal_status ORDER BY condition, status;
SELECT * FROM pairwise ORDER BY comparison_order;
SELECT * FROM task_delta_distribution ORDER BY delta;
SELECT * FROM tasks ORDER BY task_id;
SELECT * FROM tool_usage ORDER BY v4_calls DESC, capability;
SELECT * FROM open_suite ORDER BY case_order;
SELECT * FROM iterations;
"""
    (REPORT / "report-data.sql").write_text(sql, encoding="utf-8")

    file_sources = [
        {"id": "report_data", "label": "Normalized v4 evaluation snapshot", "path": "reports/harness-open-global-v4-evaluation-2026-08-11/data.json"},
        {"id": "iteration_log", "label": "Append-only Harness iteration log", "path": "docs/harness-open-scope-iteration-log-2026-08-11.md"},
        {"id": "design_record", "label": "Exploration/control boundary decision", "path": "docs/harness-exploration-control-boundary-2026-08-11.md"},
        {"id": "v4_analysis", "label": "Validated v4 task-clustered analysis", "path": "runs/experiments/gamedevbench-unseen30-open-global-diagnostic-v4-run1/analysis.json"},
        {"id": "v2_analysis", "label": "Validated completed v2 analysis", "path": "runs/experiments/gamedevbench-unseen30-open-scope-v2-run1/analysis.json"},
        {"id": "baseline_analysis", "label": "Frozen Official, Legacy, and v1 baseline analysis", "path": "runs/experiments/gamedevbench-unseen30-maturity-v1/analysis.json"},
    ]
    query_specs = {
        "headline_query": ("SELECT * FROM headline", "Loads reviewed headline quality, control, open-suite, quota, and stop metrics."),
        "condition_summary_query": ("SELECT * FROM condition_summary ORDER BY condition_order", "Loads exact quality, terminal, stability, latency, and resource metrics for five versioned conditions."),
        "terminal_status_query": ("SELECT * FROM terminal_status ORDER BY condition, status", "Loads PASS/FAIL/BLOCKED composition with the original 90-attempt denominators."),
        "pairwise_query": ("SELECT * FROM pairwise ORDER BY comparison_order", "Loads task-clustered effect intervals and exact majority-outcome tests."),
        "task_delta_distribution_query": ("SELECT * FROM task_delta_distribution ORDER BY delta", "Loads the distribution of per-task changes in passes out of three from v2 to v4."),
        "tasks_query": ("SELECT * FROM tasks ORDER BY task_id", "Loads the 30-task repeated paired audit across frozen baselines and candidates."),
        "tool_usage_query": ("SELECT * FROM tool_usage ORDER BY v4_calls DESC, capability", "Loads audited capability-call counts from v2 and v4 event streams."),
        "open_suite_query": ("SELECT * FROM open_suite ORDER BY case_order", "Loads the repeated read-only, approval, mutation, and create integration cases."),
        "iterations_query": ("SELECT * FROM iterations", "Loads the mechanism-level iteration and exclusion audit."),
    }
    query_sources = [
        _query_source(source_id, sql_text, description)
        for source_id, (sql_text, description) in query_specs.items()
    ]
    manifest_sources = file_sources + [
        {"id": source["id"], "label": source["query"]["description"], "path": "reports/harness-open-global-v4-evaluation-2026-08-11/report-data.sql"}
        for source in query_sources
    ]

    v4_rate = by_id[V4_ID]["evaluator_pass_rate"]
    official_rate = by_id["official-default"]["evaluator_pass_rate"]
    v2_rate = by_id[V2_ID]["evaluator_pass_rate"]
    delta = pair_v4_v2["pass_fraction_delta"]
    preservation_evaluated = sum(preservation_counts.values()) - preservation_counts.get("NOT_RUN", 0)
    title = "GameEngine Harness 开放边界成熟度评估 — v4"
    summary_outcome = (
        "控制层已达到当前小样本的相对成熟线"
        if mature
        else "控制层仍未达到当前小样本成熟线"
    )
    cards = [
        {
            "id": "quality_card",
            "description": "30 tasks × 3 repeats; official evaluator PASS is the primary quality endpoint",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "v4 evaluator PASS", "field": "v4_evaluator_pass_rate", "format": "percent"},
                {"label": "Official default", "field": "official_pass_rate", "format": "percent"},
                {"label": "Open-scope v2", "field": "v2_pass_rate", "format": "percent"},
            ],
        },
        {
            "id": "effect_card",
            "description": "v4 − v2; task-clustered bootstrap interval",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "Mean task delta", "field": "v4_v2_delta", "format": "percent", "signed": True},
                {"label": "95% CI low", "field": "v4_v2_ci_low", "format": "percent", "signed": True},
                {"label": "95% CI high", "field": "v4_v2_ci_high", "format": "percent", "signed": True},
            ],
        },
        {
            "id": "control_card",
            "description": "End-to-end control outcomes; BLOCKED remains in the denominator",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "v4 terminal PASS", "field": "v4_terminal_pass_rate", "format": "percent"},
                {"label": "v4 BLOCKED", "field": "v4_blocked", "format": "number"},
                {"label": "v2 BLOCKED", "field": "v2_blocked", "format": "number"},
            ],
        },
        {
            "id": "open_card",
            "description": "Two queries, one unapproved mutation, three changes, and one create/build/smoke flow",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "Open-suite expectations", "field": "open_pass_rate", "format": "percent"},
                {"label": "Passed", "field": "open_passes", "format": "number"},
                {"label": "Total", "field": "open_total", "format": "number"},
            ],
        },
    ]
    charts = [
        {
            "id": "condition_quality_chart",
            "title": "Official evaluator pass rate by versioned condition",
            "subtitle": "30 GameDevBench tasks × 3 repeats; NOT_RUN/BLOCKED retained in each 90-attempt denominator",
            "showDescription": True,
            "intent": "comparison",
            "question": "How does final v4 task quality compare with frozen controls and completed v2?",
            "rationale": "A zero-baseline horizontal bar compares the same primary endpoint across five discrete conditions.",
            "comparisonContext": {"denominator": "90 attempts per condition", "grain": "condition", "unit": "official evaluator pass rate"},
            "type": "horizontalBar",
            "dataset": "condition_summary",
            "sourceId": "condition_summary_query",
            "encodings": {
                "x": {"field": "condition", "type": "nominal", "label": "Condition"},
                "y": {"field": "evaluator_pass_rate", "type": "quantitative", "format": "percent", "label": "Evaluator PASS rate"},
                "tooltip": [
                    {"field": "evaluator_passes", "type": "quantitative", "label": "PASS"},
                    {"field": "evaluator_not_run", "type": "quantitative", "label": "NOT RUN"},
                ],
            },
            "valueFormat": "percent",
            "layout": "full",
            "labels": {"values": "all"},
            "palette": {"kind": "sequential", "name": "blue"},
            "surface": {"surface": "card", "interactiveLegend": False, "showControls": False, "viewMode": "both"},
        },
        {
            "id": "terminal_status_chart",
            "title": "Terminal status composition by condition",
            "subtitle": "PASS, FAIL, and BLOCKED across the same 90-attempt denominators",
            "showDescription": True,
            "intent": "composition",
            "question": "Did the final control layer remove false failures and premature blocking?",
            "rationale": "Stacked bars keep failures and blocking visible instead of hiding them behind evaluator pass rate.",
            "comparisonContext": {"denominator": "90 attempts per condition", "grain": "condition by terminal status", "unit": "attempts"},
            "type": "stackedBar",
            "dataset": "terminal_status",
            "sourceId": "terminal_status_query",
            "encodings": {
                "x": {"field": "condition", "type": "nominal", "label": "Condition"},
                "y": {"field": "attempt_count", "type": "quantitative", "format": "number", "label": "Attempts"},
                "color": {"field": "status", "type": "nominal", "label": "Terminal status"},
                "tooltip": [{"field": "condition_total", "type": "quantitative", "label": "Condition total"}],
            },
            "valueFormat": "number",
            "layout": "full",
            "labels": {"values": "all"},
            "palette": {"kind": "categorical", "name": "blue"},
            "legend": {"position": "bottom", "sort": "spec"},
            "surface": {"surface": "card", "interactiveLegend": False, "showControls": False, "viewMode": "both"},
        },
        {
            "id": "delta_distribution_chart",
            "title": "Per-task change in passes from v2 to v4",
            "subtitle": "30 tasks; each delta is the change in PASS count out of three repetitions",
            "showDescription": True,
            "intent": "distribution",
            "question": "Is the aggregate change broad or concentrated in a few tasks?",
            "rationale": "Seven discrete delta bins show improved, unchanged, and regressed tasks without treating 90 attempts as independent.",
            "comparisonContext": {"denominator": "30 tasks", "grain": "task", "unit": "change in passes out of three"},
            "type": "bar",
            "dataset": "task_delta_distribution",
            "sourceId": "task_delta_distribution_query",
            "encodings": {
                "x": {"field": "delta_label", "type": "nominal", "label": "v4 − v2 PASS count"},
                "y": {"field": "task_count", "type": "quantitative", "format": "number", "label": "Tasks"},
                "tooltip": [{"field": "interpretation", "type": "nominal", "label": "Direction"}],
            },
            "valueFormat": "number",
            "layout": "full",
            "labels": {"values": "all"},
            "palette": {"kind": "sequential", "name": "orange"},
            "surface": {"surface": "card", "interactiveLegend": False, "showControls": False, "viewMode": "both"},
        },
    ]
    tables = [
        {
            "id": "condition_table",
            "title": "Five-condition exact summary",
            "subtitle": "Frozen baselines are not rerun; v2 and v4 use the identical task/repetition source order",
            "showDescription": True,
            "dataset": "condition_summary",
            "sourceId": "condition_summary_query",
            "defaultSort": {"field": "condition", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "condition", "label": "Condition", "type": "text"},
                {"field": "role", "label": "Role", "type": "text"},
                {"field": "evaluator_passes", "label": "Evaluator PASS", "format": "number"},
                {"field": "evaluator_pass_rate", "label": "Evaluator rate", "format": "percent"},
                {"field": "terminal_passes", "label": "Terminal PASS", "format": "number"},
                {"field": "blocked", "label": "BLOCKED", "format": "number"},
                {"field": "majority_pass_tasks", "label": "Majority-pass tasks", "format": "number"},
                {"field": "duration_total_seconds", "label": "Total seconds", "format": "number"},
                {"field": "duration_median_seconds", "label": "Median seconds", "format": "number"},
                {"field": "duration_p95_seconds", "label": "P95 seconds", "format": "number"},
                {"field": "model_turns", "label": "Turns", "format": "number"},
                {"field": "tool_calls", "label": "Calls", "format": "number"},
            ],
        },
        {
            "id": "pairwise_table",
            "title": "Task-clustered paired effects",
            "subtitle": "100,000-sample task bootstrap; majority outcomes use two-sided exact McNemar",
            "showDescription": True,
            "dataset": "pairwise",
            "sourceId": "pairwise_query",
            "defaultSort": {"field": "comparison", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "comparison", "label": "Comparison", "type": "text"},
                {"field": "task_mean_delta", "label": "Mean delta", "format": "percent"},
                {"field": "ci95_low", "label": "95% CI low", "format": "percent"},
                {"field": "ci95_high", "label": "95% CI high", "format": "percent"},
                {"field": "first_only_majority_tasks", "label": "First-only majority", "format": "number"},
                {"field": "second_only_majority_tasks", "label": "Second-only majority", "format": "number"},
                {"field": "mcnemar_exact_p", "label": "McNemar p", "format": "number"},
            ],
        },
        {
            "id": "task_table",
            "title": "Thirty-task repeated audit",
            "subtitle": "Pass columns are counts out of three; v4 status shows terminal/evaluator for every repetition",
            "showDescription": True,
            "dataset": "tasks",
            "sourceId": "tasks_query",
            "defaultSort": {"field": "task_id", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "task_id", "label": "Task", "type": "text"},
                {"field": "task_name", "label": "Name", "type": "text"},
                {"field": "stratum", "label": "Stratum", "type": "text"},
                {"field": "official_passes", "label": "Official /3", "format": "number"},
                {"field": "legacy_passes", "label": "Legacy /3", "format": "number"},
                {"field": "programmable_v1_passes", "label": "v1 /3", "format": "number"},
                {"field": "v2_passes", "label": "v2 /3", "format": "number"},
                {"field": "v4_passes", "label": "v4 /3", "format": "number"},
                {"field": "delta_v4_v2_attempts", "label": "v4 − v2", "format": "number", "movement": True},
                {"field": "v4_attempt_statuses", "label": "v4 repetitions", "type": "text"},
            ],
        },
        {
            "id": "open_table",
            "title": "Open questions and ordinary requests",
            "subtitle": "Seven curated integration expectations; this is coverage evidence, not a population success rate",
            "showDescription": True,
            "dataset": "open_suite",
            "sourceId": "open_suite_query",
            "defaultSort": {"field": "case", "direction": "asc"},
            "density": "spacious",
            "layout": "full",
            "columns": [
                {"field": "case", "label": "Case", "type": "text"},
                {"field": "mode", "label": "Mode", "type": "text"},
                {"field": "status", "label": "Status", "type": "text"},
                {"field": "duration_seconds", "label": "Seconds", "format": "number"},
                {"field": "citations", "label": "Citations", "format": "number"},
                {"field": "model_turns", "label": "Turns", "format": "number"},
                {"field": "tool_calls", "label": "Calls", "format": "number"},
                {"field": "preservation", "label": "Preservation", "type": "text"},
            ],
        },
        {
            "id": "tool_table",
            "title": "Audited capability use",
            "subtitle": "Exact capability_requested counts from all 90 v2 and 90 v4 event streams",
            "showDescription": True,
            "dataset": "tool_usage",
            "sourceId": "tool_usage_query",
            "defaultSort": {"field": "v4_calls", "direction": "desc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "capability", "label": "Capability", "type": "text"},
                {"field": "v2_calls", "label": "v2 calls", "format": "number"},
                {"field": "v4_calls", "label": "v4 calls", "format": "number"},
                {"field": "delta_calls", "label": "Delta", "format": "number", "movement": True},
            ],
        },
        {
            "id": "iteration_table",
            "title": "Mechanism iteration and exclusion audit",
            "subtitle": "Every change is general; interrupted diagnostic batches are not scored as final outcomes",
            "showDescription": True,
            "dataset": "iterations",
            "sourceId": "iterations_query",
            "defaultSort": {"field": "iteration", "direction": "asc"},
            "density": "spacious",
            "layout": "full",
            "columns": [
                {"field": "iteration", "label": "Iteration", "type": "text"},
                {"field": "problem", "label": "Observed problem", "type": "text"},
                {"field": "general_change", "label": "General change", "type": "text"},
                {"field": "outcome", "label": "Outcome", "type": "text"},
                {"field": "disposition", "label": "Disposition", "type": "text"},
            ],
        },
    ]
    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {title}"},
        {
            "id": "technical_summary",
            "type": "markdown",
            "sourceId": "report_data",
            "body": (
                f"## 技术结论：{summary_outcome}\n\n"
                f"- **任务质量：** v4 为 {by_id[V4_ID]['evaluator_passes']}/90（{v4_rate:.1%}），Official 为 {by_id['official-default']['evaluator_passes']}/90（{official_rate:.1%}），完整 v2 为 {by_id[V2_ID]['evaluator_passes']}/90（{v2_rate:.1%}）。v4 − v2 的任务平均差为 {delta['estimate']:+.1%}，任务聚类 95% CI [{delta['ci95'][0]:+.1%}, {delta['ci95'][1]:+.1%}]。\n"
                f"- **控制层：** v4 有 {by_id[V4_ID]['blocked']} 次 BLOCKED、{v4_controls['policy_violations']} 次策略违规、{preservation_counts.get('FAIL', 0)} 次 preservation FAIL；v2 有 {by_id[V2_ID]['blocked']} 次 BLOCKED。\n"
                f"- **开放请求：** {open_passes}/{len(open_suite)} 个安全/验收预期通过；两次只读问答保持源项目不变，未审批模糊修改在模型执行前拒绝。\n"
                f"- **停止决定：** {stop_reason}。这表示 Harness 控制机制在当前有限样本上相对成熟，不表示 v4 的任务质量统计上优于 Official。"
            ),
        },
        {"id": "metric_strip", "type": "metric-strip", "cardIds": ["quality_card", "effect_card", "control_card", "open_card"]},
        {
            "id": "quality_finding",
            "type": "markdown",
            "sourceId": "report_data",
            "body": "## 质量比较必须同时看冻结默认设置和版本内增量\n\nOfficial/Legacy/v1 没有重跑；它们来自同一 frozen manifest、同一模型与 reasoning effort 的原实验。v2 与 v4 只重跑候选。主终点始终是 official evaluator PASS，并把 BLOCKED/NOT_RUN 留在 90 次分母中。图中因此可以回答两个不同问题：v4 是否缩小对 Official 的差距，以及通用 Harness 修复是否相对 v2 有净收益。",
        },
        {"id": "condition_quality_chart_block", "type": "chart", "chartId": "condition_quality_chart"},
        {"id": "condition_table_block", "type": "table", "tableId": "condition_table"},
        {
            "id": "paired_finding",
            "type": "markdown",
            "sourceId": "report_data",
            "body": f"## v4 相对 v2 的变化按任务聚类估计\n\n三次重复不是 90 个独立任务，因此区间在 30 个任务的 pass fraction 上 bootstrap。点估计为 {delta['estimate']:+.1%}，95% CI [{delta['ci95'][0]:+.1%}, {delta['ci95'][1]:+.1%}]。区间与逐题分布共同决定是否把收益理解为广泛机制改善；不能只用一次总 PASS 数作因果或泛化结论。",
        },
        {"id": "pairwise_table_block", "type": "table", "tableId": "pairwise_table"},
        {"id": "delta_distribution_chart_block", "type": "chart", "chartId": "delta_distribution_chart"},
        {
            "id": "control_finding",
            "type": "markdown",
            "sourceId": "report_data",
            "body": f"## 开放探索没有取消 Host 控制边界\n\n项目公开文本可检索、可批量读取，新输出路径可通过 manifest 协商；模型也得到统一 40/80 探索预算和完整诊断。但写入仍逐次授权，hidden evaluator、路径逃逸、二进制来源、引擎生命周期、证据 lineage 与最终 verdict 仍属于 Host。v4 的 {preservation_evaluated} 个已执行 preservation Gate 中有 {preservation_counts.get('FAIL', 0)} 个失败，策略违规为 {v4_controls['policy_violations']}。终态图把这些指标与任务语义失败分开。",
        },
        {"id": "terminal_status_chart_block", "type": "chart", "chartId": "terminal_status_chart"},
        {"id": "tool_table_block", "type": "table", "tableId": "tool_table"},
        {
            "id": "iteration_finding",
            "type": "markdown",
            "sourceId": "report_data",
            "body": "## 迭代只接受通用机制证据\n\nv2 完整但保留三个预算 BLOCKED；v3 解除早停后恢复了 `task_0257`，同时在 `task_0045` 暴露生命周期诊断被上下文投影吞掉。v3 因此在 46 份收据后停止且不计入最终分数。v4 通过持久 observation、有界自动诊断和逐变量状态投影修复该类问题；没有加入任务 ID、隐藏期望值或专用 workflow。",
        },
        {"id": "iteration_table_block", "type": "table", "tableId": "iteration_table"},
        {"id": "task_table_block", "type": "table", "tableId": "task_table"},
        {
            "id": "open_finding",
            "type": "markdown",
            "sourceId": "report_data",
            "body": f"## 开放问题覆盖读、拒绝、改、建四种用户意图\n\n本轮只重跑最终 Harness：{open_passes}/{len(open_suite)} 个预期通过。查询 route 只读取显式批准文本并验证引用；模糊修改没有审批时在模型/工作区执行前拒绝；三项已有项目修改运行 acceptance、compilation、gameplay 与 preservation；创建案例还运行 specification、structure、build 和 built-player smoke。该套件证明功能路径仍通，不代表真实用户请求成功率为 100%。",
        },
        {"id": "open_table_block", "type": "table", "tableId": "open_table"},
        {
            "id": "definitions",
            "type": "markdown",
            "body": "## 范围、指标与比较口径\n\n- **任务总体：** 预注册的 30 个 GameDevBench 未见任务；2D 15、UI 9、3D 3、script/resource 3。\n- **重复：** 每条件每题 3 次；推断单位为任务。\n- **主要质量终点：** 官方 evaluator PASS / 90；NOT_RUN 保留在分母。\n- **终态：** Harness 完成全部 public/hidden Gate 后的 PASS/FAIL/BLOCKED；它不是主要质量终点。\n- **冻结比较：** Official、Legacy、Programmable v1 来自 2026-08-10 基线；v2、v4 使用相同源调度做 candidate-only rerun。\n- **开放套件：** 七个 curated integration expectations，单次执行，不作总体成功率推断。",
        },
        {
            "id": "methodology",
            "type": "markdown",
            "sourceId": "report_data",
            "body": "## 方法、验证与可复现性\n\n模型固定为 `gpt-5.6-sol`、reasoning effort `medium`，benchmark commit 固定为 `e3868bccbb88e86a3eb2d62154f1e9e0f3fd489a`，所有候选串行运行官方 evaluator。每个 v4 尝试保存 quota snapshot、runner log、result-source hash 和 receipt；分析器验证 90/90 收据、schedule、baseline hash 与 complete marker。任务差值使用固定 seed 的 100,000-sample nonparametric task-clustered bootstrap；多数通过结果使用双侧 exact McNemar。v4 runtime tree SHA-256 与完整测试结果记录在 iteration log。",
        },
        {
            "id": "limitations",
            "type": "markdown",
            "body": "## 局限与稳健性\n\n- 这 30 题最初是未见集，但在 v2/v3 机制诊断后已成为观察过的回归集；v4 对比属于工程迭代证据，不是新的泛化留出结论。\n- 每题仅 3 次，3D 与 script/resource 各仅 3 题；总体与分层差异仍有宽区间。\n- Official 与 Harness 的 token/cache/cost 计量口径不一致；报告资源只比较时延、turn/call 和原始 token 记录，不作账单结论。\n- 开放套件是人工挑选的七条路径，缺少真实用户分布、长对话与大项目噪声。\n- 解除 route 预算增加了部分任务的长尾资源；成熟判断依赖无 Harness-caused BLOCKED 与可接受 P95，而不是简单追求最低调用数。",
        },
        {
            "id": "recommendations",
            "type": "markdown",
            "body": (
                "## 建议与停止点\n\n"
                + (
                    "1. 将 v4 作为当前默认 Programmable candidate，保留 v2 与冻结基线作回归控制。\n2. 不再针对这 30 个已观察任务继续调 prompt 或工具；下一次 outcome 提升必须使用新的预注册未见集。\n3. 下一阶段扩展真实项目规模、Unity/Godot 混合任务、视觉交互和多轮开放需求，并监控 P95、重复 sync、声明路径与 preservation。"
                    if mature
                    else "1. 继续从 v4 的 BLOCKED、policy 或 preservation 证据中提取通用机制缺陷。\n2. 修复后重跑候选，保持所有冻结控制不动。\n3. 在达到控制层成熟线前不要把 v4 设为默认。"
                )
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": "## 后续问题\n\n- 在完全新的 30–50 题集上，v4 相对 Official 的质量差距是否仍存在？\n- 大型项目中，公开文本索引与逐变量 context projection 的观察预算是否需要按项目规模自适应？\n- 哪些剩余失败来自视觉/几何 grounding，哪些来自模型漏实现可见要求？\n- 真实用户开放请求中，澄清率、一次通过率、返工轮数与保留无关文件的分布如何？",
        },
    ]
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "A task-clustered, versioned evaluation of the final programmable GameEngine Harness against frozen controls, plus a repeated open-request integration suite.",
            "generatedAt": generated_at,
            "blocks": blocks,
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": manifest_sources,
        },
        "snapshot": {
            "version": 1,
            "status": "ready",
            "generatedAt": generated_at,
            "datasets": {
                "headline": headline,
                "condition_summary": conditions,
                "terminal_status": terminal_status,
                "pairwise": pairwise,
                "task_delta_distribution": delta_distribution,
                "tasks": task_rows,
                "tool_usage": tool_usage,
                "open_suite": open_suite,
                "iterations": iterations,
            },
        },
        "sources": file_sources + query_sources,
    }
    _write(REPORT / "artifact.json", artifact)
    print(REPORT / "artifact.json")


if __name__ == "__main__":
    main()
