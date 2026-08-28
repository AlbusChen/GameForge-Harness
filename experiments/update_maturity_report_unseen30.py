#!/usr/bin/env python3
# ruff: noqa: E501
"""Merge the frozen unseen-30 analysis into the maturity report artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports/harness-maturity-evaluation-2026-08-10"
DATA_PATH = REPORT_DIR / "data.json"
ARTIFACT_PATH = REPORT_DIR / "artifact.json"
ANALYSIS_PATH = ROOT / "runs/experiments/gamedevbench-unseen30-maturity-v1/analysis.json"
MANIFEST_PATH = ROOT / "benchmarks/gamedevbench-unseen30-maturity-v1.json"
EXPERIMENT_DIR = ROOT / "runs/experiments/gamedevbench-unseen30-maturity-v1"
ITERATION_LOG = ROOT / "docs/harness-iteration-log-2026-08-10.md"

CONDITIONS = ("official-default", "legacy-tool-v1", "programmable-v1")
LABELS = {
    "official-default": "Official default",
    "legacy-tool-v1": "Legacy Harness",
    "programmable-v1": "Programmable Harness",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replace(items: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incoming_ids = {item["id"] for item in incoming}
    return [item for item in items if item.get("id") not in incoming_ids] + incoming


def _pair(analysis: dict[str, Any], first: str, second: str) -> dict[str, Any]:
    return next(
        row
        for row in analysis["pairwise"]
        if row["first"] == first and row["second"] == second
    )


def _condition_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for order, condition in enumerate(CONDITIONS, start=1):
        overall = analysis["overall"][condition]
        primary = overall["primary_evaluator"]
        terminal = overall["terminal"]
        tasks = analysis["task_metrics"]["by_condition"][condition]
        resources = overall["resources"]
        duration = resources["duration_seconds"]
        rows.append(
            {
                "condition_order": order,
                "condition": LABELS[condition],
                "condition_id": condition,
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
                "majority_pass_rate": tasks["majority_evaluator_pass_rate"],
                "never_evaluator_pass_tasks": tasks["never_evaluator_pass_tasks"],
                "duration_total_seconds": duration["total"],
                "duration_median_seconds": duration["median"],
                "duration_p95_seconds": duration["p95"],
                "duration_max_seconds": duration["max"],
                "input_tokens": resources["tokens"]["input_tokens"],
                "output_tokens": resources["tokens"]["output_tokens"],
                "tool_calls": resources["tool_calls"],
                "reported_cost_usd": resources["reported_cost_usd"],
                "cost_interpretation": (
                    "runner-reported"
                    if condition == "official-default"
                    else "not instrumented (runner recorded 0)"
                ),
            }
        )
    return rows


def _terminal_rows(condition_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in condition_rows:
        for status, field in (
            ("PASS", "terminal_passes"),
            ("FAIL", "terminal_failures"),
            ("BLOCKED", "blocked"),
        ):
            output.append(
                {
                    "condition": row["condition"],
                    "status": status,
                    "attempt_count": row[field],
                    "condition_total": row["attempts"],
                }
            )
    return output


def _repeat_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for condition in CONDITIONS:
        for repetition, summary in analysis["overall"][condition][
            "primary_by_repetition"
        ].items():
            output.append(
                {
                    "condition": LABELS[condition],
                    "condition_id": condition,
                    "repetition": int(repetition),
                    "attempts": summary["attempts"],
                    "passes": summary["status_counts"].get("PASS", 0),
                    "failures": summary["status_counts"].get("FAIL", 0),
                    "not_run": summary["status_counts"].get("NOT_RUN", 0),
                    "pass_rate": summary["pass_rate"],
                }
            )
    return output


def _stratum_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for stratum, stratum_data in analysis["task_metrics"]["by_stratum"].items():
        for condition in CONDITIONS:
            metrics = stratum_data["conditions"][condition]
            attempt_summary = analysis["overall"][condition]["primary_by_stratum"][stratum]
            output.append(
                {
                    "stratum": stratum,
                    "task_count": stratum_data["tasks"],
                    "condition": LABELS[condition],
                    "condition_id": condition,
                    "attempts": attempt_summary["attempts"],
                    "passes": attempt_summary["status_counts"].get("PASS", 0),
                    "pass_rate": attempt_summary["pass_rate"],
                    "majority_pass_tasks": metrics["majority_evaluator_pass_tasks"],
                    "majority_pass_rate": metrics["majority_evaluator_pass_rate"],
                }
            )
    return output


def _position_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for condition in CONDITIONS:
        for position, summary in analysis["overall"][condition][
            "primary_by_within_task_position"
        ].items():
            output.append(
                {
                    "condition": LABELS[condition],
                    "condition_id": condition,
                    "position": int(position),
                    "attempts": summary["attempts"],
                    "passes": summary["status_counts"].get("PASS", 0),
                    "pass_rate": summary["pass_rate"],
                }
            )
    return output


def _pairwise_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for order, pair in enumerate(analysis["pairwise"], start=1):
        delta = pair["pass_fraction_delta"]
        mcnemar = pair["majority_pass_mcnemar"]
        output.append(
            {
                "comparison_order": order,
                "comparison": f"{LABELS[pair['first']]} − {LABELS[pair['second']]}",
                "first": LABELS[pair["first"]],
                "second": LABELS[pair["second"]],
                "task_mean_delta": delta["estimate"],
                "ci95_low": delta["ci95"][0],
                "ci95_high": delta["ci95"][1],
                "bootstrap_samples": delta["samples"],
                "first_only_majority_tasks": mcnemar["first_only"],
                "second_only_majority_tasks": mcnemar["second_only"],
                "discordant_tasks": mcnemar["discordant"],
                "mcnemar_exact_p": mcnemar["two_sided_exact_p"],
            }
        )
    return output


def _task_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    diagnoses = {
        "task_0042": "required script path outside approved scope",
        "task_0083": "collider offset precision (about 7, 44)",
        "task_0108": "programmable 3/3 advantage on resource-driven particles",
        "task_0109": "two invalid scene serializations; one evaluator PASS",
        "task_0201": "relative spawn geometry Vector2(0, 15)",
        "task_0094": "new scene/script paths outside fixed approval scope",
        "task_0262": "3/3 evaluator PASS; generated .uid preservation false positive",
        "task_0149": "3/3 evaluator PASS; asset .import preservation false positive",
    }
    output = []
    for task in analysis["task_metrics"]["tasks"]:
        cells = task["conditions"]
        official = cells["official-default"]
        legacy = cells["legacy-tool-v1"]
        programmable = cells["programmable-v1"]
        output.append(
            {
                "task_id": task["task_id"],
                "task_name": task["task_name"],
                "stratum": task["stratum"],
                "official_evaluator": f"{official['evaluator_passes']}/3",
                "legacy_evaluator": f"{legacy['evaluator_passes']}/3",
                "programmable_evaluator": f"{programmable['evaluator_passes']}/3",
                "programmable_terminal": f"{programmable['terminal_passes']}/3",
                "official_majority": official["majority_evaluator_pass"],
                "legacy_majority": legacy["majority_evaluator_pass"],
                "programmable_majority": programmable["majority_evaluator_pass"],
                "programmable_delta_vs_official": (
                    programmable["evaluator_pass_fraction"]
                    - official["evaluator_pass_fraction"]
                ),
                "diagnosis": diagnoses.get(task["task_id"], "—"),
            }
        )
    return output


def _preservation_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for order, shape in enumerate(
        analysis["programmable_safety_diagnostics"]["failure_shapes"], start=1
    ):
        output.append(
            {
                "pattern_order": order,
                "changed_protected_paths": ", ".join(shape["changed_protected_paths"]),
                "attempts": shape["attempts"],
                "missing_identity_count": shape["missing_identity_count"],
                "broken_reference_count": shape["broken_reference_count"],
                "sidecar_only": all(
                    path.endswith((".import", ".uid"))
                    for path in shape["changed_protected_paths"]
                ),
            }
        )
    return output


def _headline(
    analysis: dict[str, Any], condition_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {row["condition_id"]: row for row in condition_rows}
    vs_official = _pair(analysis, "programmable-v1", "official-default")
    vs_legacy = _pair(analysis, "programmable-v1", "legacy-tool-v1")
    safety = analysis["programmable_safety_diagnostics"]
    return [
        {
            "official_pass_rate": by_id["official-default"]["evaluator_pass_rate"],
            "legacy_pass_rate": by_id["legacy-tool-v1"]["evaluator_pass_rate"],
            "programmable_pass_rate": by_id["programmable-v1"]["evaluator_pass_rate"],
            "programmable_terminal_pass_rate": by_id["programmable-v1"][
                "terminal_pass_rate"
            ],
            "programmable_delta_vs_official": vs_official["pass_fraction_delta"][
                "estimate"
            ],
            "programmable_vs_official_ci_low": vs_official["pass_fraction_delta"][
                "ci95"
            ][0],
            "programmable_vs_official_ci_high": vs_official["pass_fraction_delta"][
                "ci95"
            ][1],
            "programmable_delta_vs_legacy": vs_legacy["pass_fraction_delta"][
                "estimate"
            ],
            "programmable_blocked": by_id["programmable-v1"]["blocked"],
            "legacy_blocked": by_id["legacy-tool-v1"]["blocked"],
            "preservation_failures": safety["preservation_failures"],
            "evaluator_pass_downgraded": safety[
                "evaluator_pass_downgraded_by_terminal_gates"
            ],
            "policy_guard_events": safety["policy_guard"]["violation_events"],
            "infra_errors": 0,
            "quota_remaining": analysis["quota"]["minimum_remaining_percent_observed"]
            / 100,
        }
    ]


def _query_source(source_id: str, sql: str, description: str) -> dict[str, Any]:
    return {
        "id": source_id,
        "query": {
            "engine": "sqlite",
            "sql": sql,
            "description": description,
            "id": f"harness-maturity-{source_id}-v2",
            "language": "sql",
        },
    }


def main() -> None:
    analysis = _load(ANALYSIS_PATH)
    data = _load(DATA_PATH)
    artifact = _load(ARTIFACT_PATH)
    generated_at = analysis["generated_at"]

    conditions = _condition_rows(analysis)
    terminal = _terminal_rows(conditions)
    repeats = _repeat_rows(analysis)
    strata = _stratum_rows(analysis)
    positions = _position_rows(analysis)
    pairwise = _pairwise_rows(analysis)
    tasks = _task_rows(analysis)
    preservation = _preservation_rows(analysis)
    headline = _headline(analysis, conditions)

    if "historical_10_task_scope" not in data:
        data["historical_10_task_scope"] = data["scope"]
    data["generated_at"] = generated_at
    data["scope"] = {
        "question": "Does the frozen programmable Harness generalize on a preregistered unseen set relative to Official default and Legacy?",
        "benchmark": "GameDevBench",
        "benchmark_commit": "e3868bccbb88e86a3eb2d62154f1e9e0f3fd489a",
        "manifest": "benchmarks/gamedevbench-unseen30-maturity-v1.json",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "parallelism": 1,
        "task_count": 30,
        "repetitions_per_task_condition": 3,
        "attempt_count": 270,
        "execution_date": "2026-08-10",
        "stop_reason": "all preregistered attempts complete; holdout does not support the prior maturity conclusion",
        "weekly_subscription_used_percent": 13,
        "weekly_subscription_remaining_percent": 87,
    }
    data["holdout_headline"] = headline
    data["holdout_condition_summary"] = conditions
    data["holdout_terminal_status"] = terminal
    data["holdout_repetition_summary"] = repeats
    data["holdout_stratum_summary"] = strata
    data["holdout_position_summary"] = positions
    data["holdout_pairwise"] = pairwise
    data["holdout_tasks"] = tasks
    data["holdout_preservation_patterns"] = preservation
    data["holdout_analysis"] = analysis

    iteration = {
        "iteration": 8,
        "stage": "Preregistered unseen-30 holdout",
        "observed_problem": "Candidate trails Official on evaluator quality and byte-level preservation misclassifies Godot-generated sidecars.",
        "general_change": "No candidate change inside holdout; register engine-aware preservation, output-manifest approval, and semantic scene assertions for the next cycle.",
        "validation": "270/270 attempts; 30 tasks × 3 conditions × 3 repeats; complete receipt/hash audit.",
    }
    data["iteration_summary"] = [
        row for row in data["iteration_summary"] if row.get("iteration") != 8
    ] + [iteration]

    new_integrity = [
        {"source": str(path.relative_to(ROOT)), "sha256": _sha256(path)}
        for path in (
            MANIFEST_PATH,
            EXPERIMENT_DIR / "schedule.json",
            EXPERIMENT_DIR / "protocol-freeze.json",
            EXPERIMENT_DIR / "complete.json",
            ANALYSIS_PATH,
            ITERATION_LOG,
        )
    ]
    new_sources = {row["source"] for row in new_integrity}
    data["source_integrity"] = [
        row for row in data["source_integrity"] if row["source"] not in new_sources
    ] + new_integrity
    _write(DATA_PATH, data)

    file_sources = [
        {
            "id": "unseen_manifest",
            "label": "Frozen GameDevBench unseen 30-task manifest",
            "path": "benchmarks/gamedevbench-unseen30-maturity-v1.json",
        },
        {
            "id": "holdout_analysis",
            "label": "Validated unseen-30 task-clustered analysis",
            "path": "runs/experiments/gamedevbench-unseen30-maturity-v1/analysis.json",
        },
        {
            "id": "holdout_schedule",
            "label": "Materialized interleaved 270-attempt schedule",
            "path": "runs/experiments/gamedevbench-unseen30-maturity-v1/schedule.json",
        },
        {
            "id": "holdout_protocol",
            "label": "Candidate runtime and protocol freeze",
            "path": "runs/experiments/gamedevbench-unseen30-maturity-v1/protocol-freeze.json",
        },
    ]
    query_sources = [
        _query_source(
            "holdout_headline_query",
            "SELECT * FROM holdout_headline",
            "Loads the reviewed unseen-holdout headline metrics.",
        ),
        _query_source(
            "holdout_condition_query",
            "SELECT * FROM holdout_condition_summary ORDER BY condition_order",
            "Loads quality, terminal, task-stability, and resource metrics by condition.",
        ),
        _query_source(
            "holdout_terminal_query",
            "SELECT * FROM holdout_terminal_status ORDER BY condition, status",
            "Loads aggregate Harness terminal-state composition.",
        ),
        _query_source(
            "holdout_pairwise_query",
            "SELECT * FROM holdout_pairwise ORDER BY comparison_order",
            "Loads paired task-clustered effects and majority-outcome exact tests.",
        ),
        _query_source(
            "holdout_stratum_query",
            "SELECT * FROM holdout_stratum_summary ORDER BY stratum, condition_id",
            "Loads descriptive evaluator outcomes by preregistered task stratum.",
        ),
        _query_source(
            "holdout_task_query",
            "SELECT * FROM holdout_tasks ORDER BY task_id",
            "Loads the 30-task, three-repeat paired audit.",
        ),
        _query_source(
            "holdout_preservation_query",
            "SELECT * FROM holdout_preservation_patterns ORDER BY pattern_order",
            "Loads repeated preservation-failure sidecar patterns.",
        ),
    ]
    artifact["sources"] = _replace(artifact["sources"], file_sources + query_sources)
    artifact["manifest"]["sources"] = _replace(
        artifact["manifest"]["sources"],
        file_sources
        + [
            {
                "id": source["id"],
                "label": source["query"]["description"],
                "path": "reports/harness-maturity-evaluation-2026-08-10/report-data.sql",
            }
            for source in query_sources
        ],
    )

    cards = [
        {
            "id": "holdout_quality",
            "description": "30 unseen tasks × 3 repeats; official evaluator PASS is the primary endpoint",
            "dataset": "holdout_headline",
            "sourceId": "holdout_headline_query",
            "metrics": [
                {"label": "Programmable", "field": "programmable_pass_rate", "format": "percent"},
                {"label": "Official default", "field": "official_pass_rate", "format": "percent"},
                {"label": "Legacy", "field": "legacy_pass_rate", "format": "percent"},
            ],
        },
        {
            "id": "holdout_effect",
            "description": "Programmable − Official; task-clustered bootstrap 95% CI is −28.9 to +2.2 points",
            "dataset": "holdout_headline",
            "sourceId": "holdout_headline_query",
            "metrics": [
                {"label": "对 Official 差值", "field": "programmable_delta_vs_official", "format": "percent"},
                {"label": "CI 下界", "field": "programmable_vs_official_ci_low", "format": "percent"},
                {"label": "CI 上界", "field": "programmable_vs_official_ci_high", "format": "percent"},
            ],
        },
        {
            "id": "holdout_end_to_end",
            "description": "官方 evaluator 成功与全部 Harness Gate 通过必须分开报告",
            "dataset": "holdout_headline",
            "sourceId": "holdout_headline_query",
            "metrics": [
                {"label": "端到端终态 PASS", "field": "programmable_terminal_pass_rate", "format": "percent"},
                {"label": "Preservation FAIL", "field": "preservation_failures", "format": "number"},
                {"label": "Evaluator PASS 被降级", "field": "evaluator_pass_downgraded", "format": "number"},
            ],
        },
        {
            "id": "holdout_reliability",
            "description": "270/270 complete; zero runner errors or rate-limit markers",
            "dataset": "holdout_headline",
            "sourceId": "holdout_headline_query",
            "metrics": [
                {"label": "Programmable BLOCKED", "field": "programmable_blocked", "format": "number"},
                {"label": "Legacy BLOCKED", "field": "legacy_blocked", "format": "number"},
                {"label": "额度剩余", "field": "quota_remaining", "format": "percent"},
            ],
        },
    ]
    artifact["manifest"]["cards"] = _replace(artifact["manifest"]["cards"], cards)

    charts = [
        {
            "id": "holdout_primary_chart",
            "title": "30 个未见任务上的官方 evaluator 通过率",
            "subtitle": "每条件 90 次；BLOCKED/NOT_RUN 保留在分母",
            "showDescription": True,
            "intent": "comparison",
            "question": "冻结留出集上三种条件的主要质量终点如何？",
            "rationale": "零基线水平柱直接比较相同分母下的 evaluator PASS 率。",
            "comparisonContext": {
                "denominator": "90 attempts per condition",
                "grain": "condition",
                "unit": "official evaluator pass rate",
            },
            "type": "horizontalBar",
            "dataset": "holdout_condition_summary",
            "sourceId": "holdout_condition_query",
            "encodings": {
                "x": {"field": "condition", "type": "nominal", "label": "条件"},
                "y": {"field": "evaluator_pass_rate", "type": "quantitative", "format": "percent", "label": "通过率"},
                "tooltip": [
                    {"field": "evaluator_passes", "type": "quantitative", "label": "Evaluator PASS"},
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
            "id": "holdout_terminal_chart",
            "title": "三条件终态组成",
            "subtitle": "Evaluator PASS 仍可能因 Harness preservation Gate 降为终态 FAIL",
            "showDescription": True,
            "intent": "composition",
            "question": "PASS、FAIL、BLOCKED 在三个条件中如何组成？",
            "rationale": "相同 90 次分母的堆叠柱揭示质量终点与端到端安全终态之间的差异。",
            "comparisonContext": {"denominator": "90 attempts per condition", "grain": "condition by terminal status", "unit": "attempts"},
            "type": "stackedBar",
            "dataset": "holdout_terminal_status",
            "sourceId": "holdout_terminal_query",
            "encodings": {
                "x": {"field": "condition", "type": "nominal", "label": "条件"},
                "y": {"field": "attempt_count", "type": "quantitative", "format": "number", "label": "尝试数"},
                "color": {"field": "status", "type": "nominal", "label": "终态"},
                "tooltip": [{"field": "condition_total", "type": "quantitative", "label": "条件总数"}],
            },
            "valueFormat": "number",
            "layout": "full",
            "labels": {"values": "all"},
            "palette": {"kind": "categorical", "name": "blue"},
            "legend": {"position": "bottom", "sort": "spec"},
            "surface": {"surface": "card", "interactiveLegend": False, "showControls": False, "viewMode": "both"},
        },
    ]
    artifact["manifest"]["charts"] = _replace(artifact["manifest"]["charts"], charts)

    tables = [
        {
            "id": "holdout_condition_table",
            "title": "未见任务三条件汇总",
            "subtitle": "主要质量、端到端终态、任务稳定性和资源；Harness cost=0 表示未计量",
            "showDescription": True,
            "dataset": "holdout_condition_summary",
            "sourceId": "holdout_condition_query",
            "defaultSort": {"field": "condition", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "condition", "label": "条件", "type": "text"},
                {"field": "evaluator_passes", "label": "Evaluator PASS", "format": "number"},
                {"field": "evaluator_pass_rate", "label": "主要通过率", "format": "percent"},
                {"field": "evaluator_not_run", "label": "NOT RUN", "format": "number"},
                {"field": "terminal_passes", "label": "终态 PASS", "format": "number"},
                {"field": "blocked", "label": "BLOCKED", "format": "number"},
                {"field": "majority_pass_tasks", "label": "多数通过任务", "format": "number"},
                {"field": "duration_total_seconds", "label": "总秒数", "format": "number"},
                {"field": "duration_median_seconds", "label": "中位秒数", "format": "number"},
                {"field": "duration_p95_seconds", "label": "P95 秒数", "format": "number"},
                {"field": "input_tokens", "label": "报告 input tokens", "format": "number"},
                {"field": "cost_interpretation", "label": "成本口径", "type": "text"},
            ],
        },
        {
            "id": "holdout_pairwise_table",
            "title": "任务级配对差异与不确定性",
            "subtitle": "100,000-sample task-clustered bootstrap；多数结果用双侧精确 McNemar",
            "showDescription": True,
            "dataset": "holdout_pairwise",
            "sourceId": "holdout_pairwise_query",
            "defaultSort": {"field": "comparison", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "comparison", "label": "比较", "type": "text"},
                {"field": "task_mean_delta", "label": "平均差值", "format": "percent"},
                {"field": "ci95_low", "label": "95% CI 下界", "format": "percent"},
                {"field": "ci95_high", "label": "95% CI 上界", "format": "percent"},
                {"field": "first_only_majority_tasks", "label": "前者独赢", "format": "number"},
                {"field": "second_only_majority_tasks", "label": "后者独赢", "format": "number"},
                {"field": "mcnemar_exact_p", "label": "McNemar p", "format": "number"},
            ],
        },
        {
            "id": "holdout_stratum_table",
            "title": "预注册分层结果",
            "subtitle": "15 题 2D、9 题 UI、3 题 3D、3 题 script/resource；小层仅作描述",
            "showDescription": True,
            "dataset": "holdout_stratum_summary",
            "sourceId": "holdout_stratum_query",
            "defaultSort": {"field": "stratum", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "stratum", "label": "分层", "type": "text"},
                {"field": "condition", "label": "条件", "type": "text"},
                {"field": "task_count", "label": "任务数", "format": "number"},
                {"field": "passes", "label": "Evaluator PASS", "format": "number"},
                {"field": "pass_rate", "label": "通过率", "format": "percent"},
                {"field": "majority_pass_tasks", "label": "多数通过任务", "format": "number"},
            ],
        },
        {
            "id": "holdout_task_table",
            "title": "30 个未见任务逐题三次审计",
            "subtitle": "Evaluator 列为三次中的 PASS 数；Programmable 终态单列 preservation 等 Gate 影响",
            "showDescription": True,
            "dataset": "holdout_tasks",
            "sourceId": "holdout_task_query",
            "defaultSort": {"field": "task_id", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "task_id", "label": "任务", "type": "text"},
                {"field": "task_name", "label": "名称", "type": "text"},
                {"field": "stratum", "label": "分层", "type": "text"},
                {"field": "official_evaluator", "label": "Official Eval", "type": "text"},
                {"field": "legacy_evaluator", "label": "Legacy Eval", "type": "text"},
                {"field": "programmable_evaluator", "label": "Programmable Eval", "type": "text"},
                {"field": "programmable_terminal", "label": "Programmable 终态", "type": "text"},
                {"field": "programmable_delta_vs_official", "label": "对 Official 差值", "format": "percent"},
                {"field": "diagnosis", "label": "证据摘要", "type": "text"},
            ],
        },
        {
            "id": "holdout_preservation_table",
            "title": "Programmable preservation 失败模式",
            "subtitle": "44/86 到达 preservation 的尝试失败；所有 changed protected paths 均为 Godot .import/.uid sidecar",
            "showDescription": True,
            "dataset": "holdout_preservation_patterns",
            "sourceId": "holdout_preservation_query",
            "defaultSort": {"field": "attempts", "direction": "desc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "changed_protected_paths", "label": "检测到变化的保护路径", "type": "text"},
                {"field": "attempts", "label": "尝试数", "format": "number"},
                {"field": "sidecar_only", "label": "仅 sidecar", "type": "boolean"},
                {"field": "missing_identity_count", "label": "缺失 identity", "format": "number"},
                {"field": "broken_reference_count", "label": "新增断链", "format": "number"},
            ],
        },
    ]
    artifact["manifest"]["tables"] = _replace(artifact["manifest"]["tables"], tables)

    holdout_blocks = [
        {
            "id": "holdout_executive_summary",
            "type": "markdown",
            "sourceId": "holdout_analysis",
            "body": "## 当前结论：未见任务复核不支持‘已匹配 Official’，但确认了相对 Legacy 的进步\n\n- **主要质量终点：** Official default 47/90（52.2%），Programmable 35/90（38.9%），Legacy 20/90（22.2%）。Programmable − Official 为 −13.3 个百分点，任务聚类 bootstrap 95% CI 为 [−28.9, +2.2]；样本没有证明显著差异，却也不能支持先前 10 题上的质量平局。\n- **相对 Legacy 的改进可复现。** Programmable − Legacy 为 +16.7 点，95% CI [5.6, 28.9]；多数通过任务为 12/30 对 5/30，精确 McNemar `p = 0.015625`。BLOCKED 从 25 降到 4。\n- **端到端终态暴露新缺陷。** Programmable 只有 17/90 终态 PASS；18 次 evaluator PASS 被 preservation 降级。44 个 preservation FAIL 的 changed protected paths 全是 Godot 自动生成的 `.import`/`.uid` sidecar，说明安全模型存在系统性引擎副作用误报。\n- **成熟度判断下调。** 当前候选比 Legacy 更可靠、更强，但尚不能作为 Official default 的成熟替代；需要先修复 engine-aware preservation、输出路径预审批、场景语义断言和几何精度，再用新的未见集确认。",
        },
        {"id": "holdout_metric_strip", "type": "metric-strip", "cardIds": ["holdout_quality", "holdout_effect", "holdout_end_to_end", "holdout_reliability"]},
        {
            "id": "holdout_endpoint_note",
            "type": "markdown",
            "sourceId": "holdout_analysis",
            "body": "## 先分清两个终点：任务质量与安全终态\n\n预注册主要终点是官方 evaluator PASS；preservation、lineage、policy 和 terminal state 是二级 Harness 指标。二者不能混为一谈：Programmable 有 35 次 evaluator PASS，却只有 17 次完整 Gate 后的终态 PASS。报告因此同时展示 38.9% 的任务质量和 18.9% 的端到端安全成功率。",
        },
        {"id": "holdout_primary_chart_block", "type": "chart", "chartId": "holdout_primary_chart"},
        {"id": "holdout_condition_table_block", "type": "table", "tableId": "holdout_condition_table"},
        {
            "id": "holdout_pairwise_finding",
            "type": "markdown",
            "sourceId": "holdout_analysis",
            "body": "## 配对证据：未证明 Programmable 与 Official 不同，但已否定‘证据支持平局’\n\n30 题上 Programmable 的点估计低 13.3 点，区间跨过 0；因此不能下‘显著更差’的结论。与此同时，区间大部分位于负侧、三轮分别为 36.7%、33.3%、46.7%，而 Official 为 50.0%、56.7%、50.0%。谨慎结论是候选仍可能接近 Official，但现有证据没有达到替代成熟线。相对 Legacy 的区间完全为正，说明可编程机制的改进不是只出现在原 10 题。",
        },
        {"id": "holdout_pairwise_table_block", "type": "table", "tableId": "holdout_pairwise_table"},
        {
            "id": "holdout_safety_finding",
            "type": "markdown",
            "sourceId": "holdout_analysis",
            "body": "## 最大的新问题是 engine-aware preservation，而不是 evaluator 本身\n\n44/86 个到达 preservation 的 Programmable 尝试失败。逐份证据审计显示 changed protected paths 只包含 `.import` 和 `.uid`；没有缺失 identity，只有 3 次 `.import` 指向被忽略 `.godot/imported/*.ctex` 的缓存断链。当前字节级保护把 Godot 的确定性导入/UID 副作用等同于未授权语义修改。安全 Gate 的严格方向正确，但适配层不成熟。另有 11 次策略守卫事件分布在 9 次尝试、3 个任务；守卫阻止了越界操作，也暴露了固定 editable paths 无法在执行前完整覆盖新文件需求。",
        },
        {"id": "holdout_terminal_chart_block", "type": "chart", "chartId": "holdout_terminal_chart"},
        {"id": "holdout_preservation_table_block", "type": "table", "tableId": "holdout_preservation_table"},
        {
            "id": "holdout_stratum_finding",
            "type": "markdown",
            "sourceId": "holdout_analysis",
            "body": "## 分层结果指向明确的能力取舍\n\nProgrammable 在 script/resource 小层为 5/9，高于 Official 2/9；`task_0108` 更是 3/3 对 Official 0/3，证明可编程资源操作有真实价值。它在 UI 接近 Official（11/27 对 12/27），但在 2D（16/45 对 28/45）和 3D（3/9 对 5/9）落后。保存的差异任务指向路径规划、精确 collider/anchor 几何与场景序列化，而不是单一 workflow 写死问题。",
        },
        {"id": "holdout_stratum_table_block", "type": "table", "tableId": "holdout_stratum_table"},
        {
            "id": "holdout_resource_finding",
            "type": "markdown",
            "sourceId": "holdout_analysis",
            "body": "## 时延改善存在，但不再是对两个控制组都更快\n\nProgrammable 总时长 8,668.332 秒，比 Official 少 15.1%，比 Legacy 多 4.7%；median/p95 为 78.718/179.610 秒，Official 为 90.402/231.551，Legacy 为 62.820/243.733。它的尾部更短，但总时间没有优于 Legacy。Harness 的 cost 字段本轮全部为 0，表示未计量，不表示零花费；Official 的 $8.862558 也只能作为 runner-reported 原值。",
        },
        {"id": "holdout_task_table_block", "type": "table", "tableId": "holdout_task_table"},
        {
            "id": "holdout_revised_decision",
            "type": "markdown",
            "sourceId": "iteration_log",
            "body": "## 修订后的成熟度与下一步\n\n当前版本的结论是‘比 Legacy 成熟，但尚未达到 Official 替代成熟度’。本留出集内未修改候选代码，也不应把这 30 题变成新的泛化调参集。下一周期应先做四项通用机制修复：engine-aware sidecar 语义保护；写前 output-manifest 与一次性审批扩展；节点/脚本/序列化/transform/collider/relative-anchor 的 evaluator-independent semantic assertions；保留本集作机制回归后，再用新的预注册未见集验证 outcome。",
        },
        {
            "id": "historical_section",
            "type": "markdown",
            "body": "---\n\n## 历史：10 题迭代集与开放请求验证\n\n以下部分保留原始迭代证据，但其‘有限样本成熟’判断已由上面的未见任务复核修正。",
        },
    ]
    holdout_ids = {block["id"] for block in holdout_blocks}
    old_blocks = [
        block for block in artifact["manifest"]["blocks"] if block["id"] not in holdout_ids
    ]
    title = next(block for block in old_blocks if block["id"] == "title")
    title["body"] = "# GameEngine Harness 成熟度迭代评估\n\n30-task preregistered unseen holdout extension"
    old_blocks = [block for block in old_blocks if block["id"] != "title"]
    for block in old_blocks:
        if block["id"] == "executive_summary":
            block["body"] = block["body"].replace(
                "## 结论：当前小样本上已达到工程成熟线，但没有证明质量优胜",
                "## 历史 10 题结论：当时达到有限样本工程成熟线",
            )
            history_note = "> 该结论已被 30 题未见留出复核修正；只作为迭代历史保留。"
            block["body"] = block["body"].replace(f"\n\n{history_note}", "").rstrip()
            block["body"] += f"\n\n{history_note}"
        elif block["id"] == "fair_comparison_finding":
            block["body"] = block["body"].replace("## 默认 setting", "## 历史 10 题：默认 setting")
        elif block["id"] == "stop_decision":
            block["body"] = block["body"].replace(
                "## 停止理由：达到有限样本成熟线，而不是额度不足",
                "## 历史停止理由：先冻结再做未见复核",
            )
            stop_note = "上述下一步已经执行；当前成熟度以本报告顶部的 30 题结果为准。"
            block["body"] = block["body"].replace(f"\n\n{stop_note}", "").rstrip()
            block["body"] += f"\n\n{stop_note}"
        elif block["id"] == "recommendations":
            block["body"] = block["body"].replace("## 下一步", "## 留出实验前的下一步（第 1–2 项已执行）")
        elif block["id"] == "limitations":
            block["body"] = "## 当前局限\n\n- 30 题、每条件每题 3 次显著强于原 10 题单次设计，但仍不足以精确界定 10–15 个百分点的差异。\n- 3D 与 script/resource 各只有 3 题，分层差异只作描述。\n- Harness 与 Official 的 token/cache/cost 归集不同；本轮 Harness cost=0 是缺失计量。\n- preservation 的字节级 sidecar 判定已被证明存在系统性误报，修复前不能把终态 PASS 当纯任务质量。\n- 保存 evaluator 证据可诊断失败，但留出任务现已被观察；修复后必须用新的未见集作 outcome 复核。\n- 原 7 个开放案例仍是 curated integration suite，不能外推为真实用户成功率。"
        elif block["id"] == "methodology":
            block["body"] = "## 方法与可复现性\n\n当前主证据使用 `benchmarks/gamedevbench-unseen30-maturity-v1.json`：30 个未见任务、三条件、每格 3 次，共 270 次；同一 `gpt-5.6-sol`、`medium`、串行、pinned GameDevBench commit 与官方 evaluator。任务顺序轮换，条件顺序用 Latin rotation 平衡到每个 within-task position 各 30 次。主要终点是 official evaluator PASS；终态、BLOCKED、preservation、lineage、policy、时延与资源单列。任务级差异使用 100,000-sample clustered bootstrap；多数结果使用 exact McNemar。270 份收据、schedule、source hash 与 runtime digest 已全部验证。"
        elif block["id"] == "sources_note":
            block["body"] = "## 来源\n\n报告由冻结 manifest、materialized schedule、protocol freeze、270 份逐尝试收据、结果源 SHA-256、`analysis.json`、iteration log 与历史规范化快照构建。`data.json` 保存完整源哈希和报告数据；`report-data.sql` 记录投影。HTML 是同一 canonical artifact 的自包含只读包装。"
    artifact["manifest"]["blocks"] = [title] + holdout_blocks + old_blocks

    artifact["manifest"]["title"] = "GameEngine Harness 成熟度迭代评估 — 未见 30 题复核"
    artifact["manifest"]["description"] = "冻结可编程 Harness，在 30 个未见 GameDevBench 任务上与 Official default、Legacy 各重复三次，并复核质量、安全、可靠性与资源。"
    artifact["manifest"]["generatedAt"] = generated_at
    artifact["snapshot"]["generatedAt"] = generated_at
    artifact["snapshot"]["datasets"].update(
        {
            "holdout_headline": headline,
            "holdout_condition_summary": conditions,
            "holdout_terminal_status": terminal,
            "holdout_repetition_summary": repeats,
            "holdout_stratum_summary": strata,
            "holdout_position_summary": positions,
            "holdout_pairwise": pairwise,
            "holdout_tasks": tasks,
            "holdout_preservation_patterns": preservation,
            "iterations": data["iteration_summary"],
        }
    )
    _write(ARTIFACT_PATH, artifact)

    print(DATA_PATH)
    print(ARTIFACT_PATH)


if __name__ == "__main__":
    main()
