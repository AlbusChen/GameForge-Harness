#!/usr/bin/env python3
# ruff: noqa: E501
"""Build the final Fresh-50 adaptive Harness evaluation report artifact."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/harness-fresh50-adaptive-final-evaluation-2026-08-11"
BASELINE = ROOT / "runs/experiments/gamedevbench-fresh50-official-v4-v1"
V6 = ROOT / "runs/experiments/gamedevbench-fresh50-programmable-open-adaptive-v6-run1"
V7 = ROOT / "runs/experiments/gamedevbench-fresh50-programmable-open-adaptive-v7-run1"
V10 = ROOT / "runs/experiments/gamedevbench-fresh50-programmable-open-adaptive-v10-run1"
V11 = ROOT / "runs/experiments/gamedevbench-fresh50-programmable-open-adaptive-v11-run1"
OPEN = ROOT / "runs/experiments/open-request-proxy-v2-programmable-open-adaptive-v13-run1"
SCALE = ROOT / "runs/experiments/scale-context-v11/measurements.json"
TAXONOMY = V11 / "failure-taxonomy-analysis.json"
ITERATION_LOG = ROOT / "docs/harness-fresh50-delivery-iteration-log-2026-08-11.md"
DESIGN = ROOT / "docs/harness-exploration-control-boundary-2026-08-11.md"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _condition_row(
    analysis: dict[str, Any],
    condition_id: str,
    label: str,
    order: int,
    role: str,
) -> dict[str, object]:
    row = analysis["overall"][condition_id]
    evaluator = row["evaluator"]
    terminal = row["terminal"]
    resources = row["resources"]
    controls = analysis.get("candidate_controls", {}) if "adaptive" in condition_id else {}
    preservation = controls.get("preservation_gate", {})
    return {
        "condition_order": order,
        "condition": label,
        "condition_id": condition_id,
        "role": role,
        "attempts": row["attempts"],
        "passes": evaluator["status_counts"].get("PASS", 0),
        "failures": evaluator["status_counts"].get("FAIL", 0),
        "not_run": evaluator["status_counts"].get("NOT_RUN", 0),
        "pass_rate": evaluator["pass_rate"],
        "terminal_passes": terminal["status_counts"].get("PASS", 0),
        "blocked": terminal["status_counts"].get("BLOCKED", 0),
        "preservation_passes": preservation.get("PASS", 0),
        "policy_violations": controls.get("policy_violations", 0),
        "duration_median_seconds": resources["duration_seconds"]["median"],
        "duration_p95_seconds": resources["duration_seconds"]["p95"],
        "duration_max_seconds": resources["duration_seconds"]["max"],
        "duration_total_seconds": resources["duration_seconds"]["total"],
        "model_turns": resources["model_turns"],
        "tool_calls": resources["tool_calls"],
        "input_tokens": resources["tokens"]["input_tokens"],
        "cached_input_tokens": resources["tokens"]["cached_input_tokens"],
        "output_tokens": resources["tokens"]["output_tokens"],
    }


def _query_source(source_id: str, sql: str, description: str) -> dict[str, object]:
    return {
        "id": source_id,
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "id": f"fresh50-adaptive-final-{source_id}",
            "sql": sql,
            "description": description,
            "tables_used": [source_id.removesuffix("_query")],
        },
    }


def main() -> None:
    required = [
        BASELINE / "analysis.json",
        V6 / "analysis.json",
        V7 / "analysis.json",
        V10 / "analysis.json",
        V11 / "analysis.json",
        V11 / "complete.json",
        OPEN / "analysis.json",
        OPEN / "complete.json",
        SCALE,
        TAXONOMY,
        ITERATION_LOG,
        DESIGN,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"final evaluation artifacts are incomplete: {missing}")

    baseline = _load(BASELINE / "analysis.json")
    v6 = _load(V6 / "analysis.json")
    v7 = _load(V7 / "analysis.json")
    v10 = _load(V10 / "analysis.json")
    v11 = _load(V11 / "analysis.json")
    open_analysis = _load(OPEN / "analysis.json")
    scale = _load(SCALE)
    taxonomy = _load(TAXONOMY)
    if not all(
        item["validation"]["valid"]
        for item in (baseline, v6, v7, v10, v11, open_analysis, taxonomy)
    ):
        raise RuntimeError("one or more source analyses failed validation")

    conditions = [
        _condition_row(v11, "official-default", "Official Default", 1, "frozen baseline"),
        _condition_row(
            v11,
            "programmable-open-global-diagnostic-v4",
            "Harness v4",
            2,
            "frozen prior Harness",
        ),
        _condition_row(v6, "programmable-open-adaptive-v6", "Harness v6", 3, "complete iteration"),
        _condition_row(v7, "programmable-open-adaptive-v7", "Harness v7", 4, "complete iteration"),
        _condition_row(v10, "programmable-open-adaptive-v10", "Harness v10", 5, "complete iteration"),
        _condition_row(v11, "programmable-open-adaptive-v11", "Harness v11", 6, "final Fresh-50 candidate"),
    ]
    by_id = {str(row["condition_id"]): row for row in conditions}

    v4_pair = baseline["paired"]
    pairwise = [
        {
            "comparison_order": 1,
            "comparison": "Harness v11 − Official Default",
            "estimate": v11["paired"]["candidate_minus_official"]["estimate"],
            "ci95_low": v11["paired"]["candidate_minus_official"]["ci95"][0],
            "ci95_high": v11["paired"]["candidate_minus_official"]["ci95"][1],
            "first_only": v11["paired"]["candidate_vs_official_mcnemar"]["first_only"],
            "second_only": v11["paired"]["candidate_vs_official_mcnemar"]["second_only"],
            "mcnemar_p": v11["paired"]["candidate_vs_official_mcnemar"]["two_sided_exact_p"],
        },
        {
            "comparison_order": 2,
            "comparison": "Harness v11 − Harness v4",
            "estimate": v11["paired"]["candidate_minus_v4"]["estimate"],
            "ci95_low": v11["paired"]["candidate_minus_v4"]["ci95"][0],
            "ci95_high": v11["paired"]["candidate_minus_v4"]["ci95"][1],
            "first_only": v11["paired"]["candidate_vs_v4_mcnemar"]["first_only"],
            "second_only": v11["paired"]["candidate_vs_v4_mcnemar"]["second_only"],
            "mcnemar_p": v11["paired"]["candidate_vs_v4_mcnemar"]["two_sided_exact_p"],
        },
        {
            "comparison_order": 3,
            "comparison": "Harness v4 − Official Default",
            "estimate": v4_pair["v4_minus_official"]["estimate"],
            "ci95_low": v4_pair["v4_minus_official"]["ci95"][0],
            "ci95_high": v4_pair["v4_minus_official"]["ci95"][1],
            "first_only": v4_pair["mcnemar"]["first_only"],
            "second_only": v4_pair["mcnemar"]["second_only"],
            "mcnemar_p": v4_pair["mcnemar"]["two_sided_exact_p"],
        },
    ]

    strata = []
    official_strata = v11["overall"]["official-default"]["by_stratum"]
    candidate_strata = v11["overall"]["programmable-open-adaptive-v11"]["by_stratum"]
    for order, stratum in enumerate(("2d", "ui", "3d", "script_resource"), start=1):
        strata.append(
            {
                "stratum_order": order,
                "stratum": stratum,
                "attempts": official_strata[stratum]["attempts"],
                "official_passes": official_strata[stratum]["passes"],
                "official_pass_rate": official_strata[stratum]["pass_rate"],
                "v11_passes": candidate_strata[stratum]["passes"],
                "v11_pass_rate": candidate_strata[stratum]["pass_rate"],
                "delta": candidate_strata[stratum]["pass_rate"]
                - official_strata[stratum]["pass_rate"],
            }
        )

    taxonomy_primary = [
        {
            "category": category,
            "failure_count": count,
            "failure_rate": count / taxonomy["failure_count"],
        }
        for category, count in sorted(
            taxonomy["primary_categories"].items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    taxonomy_flags = [
        {
            "flag": "Visual / geometry grounding",
            "failure_count": taxonomy["overlapping_flags"]["visual_or_geometry_grounding"],
            "failure_rate": taxonomy["overlapping_flags"]["visual_or_geometry_grounding"]
            / taxonomy["failure_count"],
        },
        {
            "flag": "Visible requirement omission",
            "failure_count": taxonomy["overlapping_flags"]["visible_requirement_omission"],
            "failure_rate": taxonomy["overlapping_flags"]["visible_requirement_omission"]
            / taxonomy["failure_count"],
        },
        {
            "flag": "Observed Harness contribution",
            "failure_count": taxonomy["overlapping_flags"]["harness_contribution"],
            "failure_rate": taxonomy["overlapping_flags"]["harness_contribution"]
            / taxonomy["failure_count"],
        },
    ]

    open_cases = [
        {
            **row,
            "changed_paths": ", ".join(row.get("changed_paths", [])),
        }
        for row in open_analysis["cases"]
    ]
    open_rework = [
        {"rework_rounds": int(rounds), "cases": count}
        for rounds, count in sorted(
            open_analysis["mechanism_metrics"]["rework_rounds"]["distribution"].items(),
            key=lambda item: int(item[0]),
        )
    ]
    scale_index = [
        {
            "requested_text_files": row["requested_text_files"],
            "pages": row["list_pages"],
            "paged_files": row["paged_files"],
            "tail_target_found": row["tail_target_found"],
            "coverage_complete": row["pagination_complete"],
            "maximum_list_observation_bytes": row["list_observation_bytes"],
        }
        for row in scale["index_cases"]
    ]
    scale_projection = [
        {
            "variable_count": row["variable_count"],
            "diagnostic_position": row["diagnostic_position"],
            "projection_bytes": row["projection_bytes"],
            "projected_key_count": row["projected_key_count"],
            "diagnostic_retained": row["diagnostic_value_retained"],
        }
        for row in scale["projection_cases"]
    ]
    scale_files = [
        {
            "file_bytes": row["file_bytes"],
            "pages": row["pages"],
            "maximum_observation_bytes": row["maximum_observation_bytes"],
            "coverage_complete": row["coverage_complete"],
        }
        for row in scale["file_paging_cases"]
    ]

    tasks = [
        {
            "task_id": row["task_id"],
            "task_name": row["task_name"],
            "stratum": row["stratum"],
            "official": row["official"],
            "v4": row["v4"],
            "v11": row["candidate"],
            "program_cell_errors": row["candidate_trace"]["program_cell_errors"],
            "decision_errors": row["candidate_trace"]["decision_errors"],
        }
        for row in v11["tasks"]
    ]

    iterations = [
        {"version": "v4", "status": "complete frozen", "result": "17/50", "general_change": "开放全局诊断基线；2 个 admission BLOCKED"},
        {"version": "v5", "status": "partial excluded", "result": "3/14", "general_change": "分页索引、状态投影、可协商写范围；暴露二进制上下文耦合"},
        {"version": "v6", "status": "complete", "result": "20/50", "general_change": "二进制资源元数据、planned NodePath、字符串/JSON 属性"},
        {"version": "v7", "status": "complete", "result": "19/50", "general_change": "复合键、受限语言补全、2 MiB 分页读取"},
        {"version": "v8", "status": "31/50 excluded", "result": "11 PASS / 19 FAIL / 1 BLOCKED", "general_change": "接口完整性；因用户并发 Godot 修复触发 runtime guard"},
        {"version": "v9", "status": "8/50 excluded", "result": "4/8", "general_change": "基线感知验证；发现读取后缀误扩成写入范围"},
        {"version": "v10", "status": "complete", "result": "19/50", "general_change": "读写后缀分离；零 BLOCKED/基础设施失败"},
        {"version": "v11", "status": "complete final benchmark", "result": "20/50", "general_change": "多编码公共文本读取；Fresh-50 最终比较"},
        {"version": "v12", "status": "open 20/20", "result": "first-pass 5/16", "general_change": "宽引用拆分；发现 Unity 缺失读/审阅能力"},
        {
            "version": "v13",
            "status": "delivery candidate",
            "result": f"open {open_analysis['overall']['passes']}/{open_analysis['overall']['cases']}",
            "general_change": "Unity 分页/批量只读与有界 Host diff 审阅；不改变 Fresh-50 路径",
        },
    ]

    first_pass = open_analysis["mechanism_metrics"]["first_pass"]
    rework = open_analysis["mechanism_metrics"]["rework_rounds"]
    safe = open_analysis["mechanism_metrics"]["underspecified_safe_disposition"]
    preservation = open_analysis["mechanism_metrics"]["unrelated_file_preservation"]
    final_candidate = by_id["programmable-open-adaptive-v11"]
    official = by_id["official-default"]
    v4 = by_id["programmable-open-global-diagnostic-v4"]
    headline = [
        {
            "v11_pass_rate": final_candidate["pass_rate"],
            "official_pass_rate": official["pass_rate"],
            "v4_pass_rate": v4["pass_rate"],
            "v11_official_delta": v11["paired"]["candidate_minus_official"]["estimate"],
            "v11_official_ci_low": v11["paired"]["candidate_minus_official"]["ci95"][0],
            "v11_official_ci_high": v11["paired"]["candidate_minus_official"]["ci95"][1],
            "open_pass_rate": open_analysis["overall"]["pass_rate"],
            "open_first_pass_rate": first_pass["rate"],
            "open_rework_mean": rework["mean"],
            "safe_disposition_rate": safe["rate"],
            "preservation_rate": preservation["rate"],
            "visual_flag_rate": taxonomy["overlapping_flags"]["visual_or_geometry_grounding"]
            / taxonomy["failure_count"],
            "omission_flag_rate": taxonomy["overlapping_flags"]["visible_requirement_omission"]
            / taxonomy["failure_count"],
            "harness_contribution_rate": taxonomy["overlapping_flags"]["harness_contribution"]
            / taxonomy["failure_count"],
            "quota_remaining_minimum": min(
                v11["quota"]["minimum_remaining_percent_observed"],
                open_analysis["resources"]["minimum_quota_remaining_percent"],
            )
            / 100,
        }
    ]

    source_paths = [
        ROOT / "benchmarks/gamedevbench-fresh50-v1.json",
        ROOT / "benchmarks/unity-open-request-proxy-v2.json",
        BASELINE / "analysis.json",
        V6 / "analysis.json",
        V7 / "analysis.json",
        V10 / "analysis.json",
        V11 / "analysis.json",
        TAXONOMY,
        SCALE,
        OPEN / "analysis.json",
        OPEN / "protocol-freeze.json",
        ITERATION_LOG,
        DESIGN,
    ]
    generated_at = datetime.now(UTC).isoformat()
    data = {
        "schema_version": 1,
        "report_id": "harness-fresh50-adaptive-final-evaluation-2026-08-11",
        "generated_at": generated_at,
        "scope": {
            "benchmark": "GameDevBench Fresh-50",
            "task_count": 50,
            "attempts_per_condition": 50,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "primary_endpoint": "official evaluator PASS",
            "freshness": "Official/v4 were genuinely unseen at freeze; later v6-v11 runs are retrospective iterations on the same set",
            "delivery_version": "v13",
            "benchmark_version": "v11; v12-v13 changes are isolated to project-query and Unity adapter paths",
            "stop_reason": "control and open-path criteria pass; remaining benchmark gap is dominated by quality/grounding rather than recurring hard Harness blocks",
        },
        "headline": headline,
        "condition_summary": conditions,
        "pairwise": pairwise,
        "strata": strata,
        "taxonomy_primary": taxonomy_primary,
        "taxonomy_flags": taxonomy_flags,
        "open_cases": open_cases,
        "open_rework": open_rework,
        "scale_index": scale_index,
        "scale_projection": scale_projection,
        "scale_files": scale_files,
        "tasks": tasks,
        "iterations": iterations,
        "source_integrity": [
            {"source": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
            for path in source_paths
        ],
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    _write(REPORT / "data.json", data)
    (REPORT / "report-data.sql").write_text(
        """-- Reproducible projections over reports/harness-fresh50-adaptive-final-evaluation-2026-08-11/data.json
SELECT * FROM headline;
SELECT * FROM condition_summary ORDER BY condition_order;
SELECT * FROM pairwise ORDER BY comparison_order;
SELECT * FROM strata ORDER BY stratum_order;
SELECT * FROM taxonomy_primary ORDER BY failure_count DESC;
SELECT * FROM taxonomy_flags ORDER BY failure_count DESC;
SELECT * FROM open_cases;
SELECT * FROM open_rework ORDER BY rework_rounds;
SELECT * FROM scale_index ORDER BY requested_text_files;
SELECT * FROM scale_projection ORDER BY variable_count, diagnostic_position;
SELECT * FROM scale_files ORDER BY file_bytes;
SELECT * FROM tasks ORDER BY task_id;
SELECT * FROM iterations;
""",
        encoding="utf-8",
    )

    report_path = "reports/harness-fresh50-adaptive-final-evaluation-2026-08-11"
    file_sources = [
        {"id": "report_data", "label": "Normalized final evaluation snapshot", "path": f"{report_path}/data.json"},
        {"id": "iteration_log", "label": "Append-only Fresh-50 iteration log", "path": "docs/harness-fresh50-delivery-iteration-log-2026-08-11.md"},
        {"id": "design_record", "label": "Exploration/control boundary record", "path": "docs/harness-exploration-control-boundary-2026-08-11.md"},
        {"id": "v11_analysis", "label": "Validated Fresh-50 v11 analysis", "path": "runs/experiments/gamedevbench-fresh50-programmable-open-adaptive-v11-run1/analysis.json"},
        {"id": "taxonomy_analysis", "label": "Validated v11 failure taxonomy", "path": "runs/experiments/gamedevbench-fresh50-programmable-open-adaptive-v11-run1/failure-taxonomy-analysis.json"},
        {"id": "open_analysis", "label": "Validated v13 open-request proxy analysis", "path": "runs/experiments/open-request-proxy-v2-programmable-open-adaptive-v13-run1/analysis.json"},
        {"id": "scale_measurements", "label": "v11 deterministic scale measurements", "path": "runs/experiments/scale-context-v11/measurements.json"},
    ]
    specs = {
        "headline_query": ("SELECT * FROM headline", "Loads reviewed headline quality, open-path, taxonomy, quota, and stop metrics."),
        "condition_summary_query": ("SELECT * FROM condition_summary ORDER BY condition_order", "Loads complete versioned Fresh-50 conditions."),
        "pairwise_query": ("SELECT * FROM pairwise ORDER BY comparison_order", "Loads paired task-bootstrap intervals and exact McNemar results."),
        "strata_query": ("SELECT * FROM strata ORDER BY stratum_order", "Loads Official and v11 quality by task stratum."),
        "taxonomy_primary_query": ("SELECT * FROM taxonomy_primary ORDER BY failure_count DESC", "Loads mutually exclusive primary failure causes."),
        "taxonomy_flags_query": ("SELECT * FROM taxonomy_flags ORDER BY failure_count DESC", "Loads overlapping visual, omission, and Harness-contribution flags."),
        "open_cases_query": ("SELECT * FROM open_cases", "Loads all 20 authored open-request proxy results."),
        "open_rework_query": ("SELECT * FROM open_rework ORDER BY rework_rounds", "Loads executed-case rework distribution."),
        "scale_index_query": ("SELECT * FROM scale_index ORDER BY requested_text_files", "Loads project-index scale and paging coverage."),
        "scale_projection_query": ("SELECT * FROM scale_projection ORDER BY variable_count, diagnostic_position", "Loads bounded context-projection measurements."),
        "scale_files_query": ("SELECT * FROM scale_files ORDER BY file_bytes", "Loads large-file paging measurements."),
        "tasks_query": ("SELECT * FROM tasks ORDER BY task_id", "Loads the 50-task paired audit."),
        "iterations_query": ("SELECT * FROM iterations", "Loads the complete general-mechanism iteration record."),
    }
    query_sources = [
        _query_source(source_id, sql, description)
        for source_id, (sql, description) in specs.items()
    ]
    manifest_sources = file_sources + [
        {"id": source["id"], "label": source["query"]["description"], "path": f"{report_path}/report-data.sql"}
        for source in query_sources
    ]

    cards = [
        {
            "id": "quality_card",
            "description": "50 paired tasks; official evaluator PASS",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "Harness v11", "field": "v11_pass_rate", "format": "percent"},
                {"label": "Official", "field": "official_pass_rate", "format": "percent"},
                {"label": "Gap", "field": "v11_official_delta", "format": "percent", "signed": True},
            ],
        },
        {
            "id": "open_card",
            "description": "20 authored open-request proxy scenarios",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "Expected outcome", "field": "open_pass_rate", "format": "percent"},
                {"label": "First pass", "field": "open_first_pass_rate", "format": "percent"},
                {"label": "Mean rework", "field": "open_rework_mean", "format": "number"},
            ],
        },
        {
            "id": "control_card",
            "description": "Safety/control outcomes",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "Safe disposition", "field": "safe_disposition_rate", "format": "percent"},
                {"label": "Preservation", "field": "preservation_rate", "format": "percent"},
                {"label": "Min quota left", "field": "quota_remaining_minimum", "format": "percent"},
            ],
        },
        {
            "id": "cause_card",
            "description": "Overlapping labels among 30 v11 failures",
            "dataset": "headline",
            "sourceId": "headline_query",
            "metrics": [
                {"label": "Visual/geometry", "field": "visual_flag_rate", "format": "percent"},
                {"label": "Visible omission", "field": "omission_flag_rate", "format": "percent"},
                {"label": "Harness contribution", "field": "harness_contribution_rate", "format": "percent"},
            ],
        },
    ]
    charts = [
        {
            "id": "quality_chart",
            "title": "Fresh-50 official-evaluator quality",
            "subtitle": "One attempt per task and condition; incomplete v5/v8/v9 runs excluded",
            "showDescription": True,
            "intent": "comparison",
            "question": "Did the adaptive Harness close the Official Default quality gap?",
            "rationale": "Zero-baseline bars compare the identical primary endpoint across complete versioned conditions.",
            "comparisonContext": {"denominator": "50 tasks per condition", "grain": "condition", "unit": "official evaluator PASS rate"},
            "type": "horizontalBar",
            "dataset": "condition_summary",
            "sourceId": "condition_summary_query",
            "encodings": {
                "x": {"field": "condition", "type": "nominal", "label": "Condition"},
                "y": {"field": "pass_rate", "type": "quantitative", "format": "percent", "label": "PASS rate"},
                "tooltip": [{"field": "passes", "type": "quantitative", "label": "PASS"}, {"field": "blocked", "type": "quantitative", "label": "BLOCKED"}],
            },
            "valueFormat": "percent",
            "layout": "full",
            "labels": {"values": "all"},
            "palette": {"kind": "sequential", "name": "blue"},
            "surface": {"surface": "card", "interactiveLegend": False, "showControls": False, "viewMode": "both"},
        },
        {
            "id": "taxonomy_chart",
            "title": "Primary cause of the 30 v11 failures",
            "subtitle": "Manual evidence-grounded classification; one primary category per failure",
            "showDescription": True,
            "intent": "comparison",
            "question": "Which mechanism dominates the remaining failures?",
            "rationale": "A sorted bar chart keeps primary causes mutually exclusive; overlapping flags are reported separately.",
            "comparisonContext": {"denominator": "30 failed tasks", "grain": "primary category", "unit": "failures"},
            "type": "horizontalBar",
            "dataset": "taxonomy_primary",
            "sourceId": "taxonomy_primary_query",
            "encodings": {
                "x": {"field": "category", "type": "nominal", "label": "Primary category"},
                "y": {"field": "failure_count", "type": "quantitative", "format": "number", "label": "Failures"},
            },
            "valueFormat": "number",
            "layout": "full",
            "labels": {"values": "all"},
            "palette": {"kind": "sequential", "name": "orange"},
            "surface": {"surface": "card", "interactiveLegend": False, "showControls": False, "viewMode": "both"},
        },
        {
            "id": "rework_chart",
            "title": "Open-request rework distribution",
            "subtitle": "16 executed query/change/create cases; unapproved cases do not invoke the model",
            "showDescription": True,
            "intent": "distribution",
            "question": "How often did the final open path need a recovery turn?",
            "rationale": "Discrete bars show exact counts rather than hiding recovery behind a mean.",
            "comparisonContext": {"denominator": "16 executed cases", "grain": "case", "unit": "program/decision error rounds"},
            "type": "bar",
            "dataset": "open_rework",
            "sourceId": "open_rework_query",
            "encodings": {
                "x": {"field": "rework_rounds", "type": "ordinal", "label": "Rework rounds"},
                "y": {"field": "cases", "type": "quantitative", "format": "number", "label": "Cases"},
            },
            "valueFormat": "number",
            "layout": "full",
            "labels": {"values": "all"},
            "palette": {"kind": "sequential", "name": "blue"},
            "surface": {"surface": "card", "interactiveLegend": False, "showControls": False, "viewMode": "both"},
        },
    ]
    tables = [
        {
            "id": "condition_table",
            "title": "Complete Fresh-50 conditions",
            "subtitle": "All use the same frozen 50 tasks, model, reasoning effort, and official evaluator",
            "showDescription": True,
            "dataset": "condition_summary",
            "sourceId": "condition_summary_query",
            "defaultSort": {"field": "condition", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "condition", "label": "Condition", "type": "text"},
                {"field": "role", "label": "Role", "type": "text"},
                {"field": "passes", "label": "PASS /50", "format": "number"},
                {"field": "pass_rate", "label": "Rate", "format": "percent"},
                {"field": "blocked", "label": "BLOCKED", "format": "number"},
                {"field": "duration_median_seconds", "label": "Median s", "format": "number"},
                {"field": "duration_p95_seconds", "label": "P95 s", "format": "number"},
                {"field": "duration_max_seconds", "label": "Max s", "format": "number"},
                {"field": "model_turns", "label": "Turns", "format": "number"},
                {"field": "tool_calls", "label": "Calls", "format": "number"},
            ],
        },
        {
            "id": "pairwise_table",
            "title": "Paired task-level effects",
            "subtitle": "100,000-sample paired task bootstrap; exact two-sided McNemar",
            "showDescription": True,
            "dataset": "pairwise",
            "sourceId": "pairwise_query",
            "defaultSort": {"field": "comparison", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "comparison", "label": "Comparison", "type": "text"},
                {"field": "estimate", "label": "Delta", "format": "percent"},
                {"field": "ci95_low", "label": "95% low", "format": "percent"},
                {"field": "ci95_high", "label": "95% high", "format": "percent"},
                {"field": "first_only", "label": "First-only", "format": "number"},
                {"field": "second_only", "label": "Second-only", "format": "number"},
                {"field": "mcnemar_p", "label": "McNemar p", "format": "number"},
            ],
        },
        {
            "id": "strata_table",
            "title": "Quality by task stratum",
            "subtitle": "Small 3D and script/resource cells are descriptive only",
            "showDescription": True,
            "dataset": "strata",
            "sourceId": "strata_query",
            "defaultSort": {"field": "stratum", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "stratum", "label": "Stratum", "type": "text"},
                {"field": "attempts", "label": "Tasks", "format": "number"},
                {"field": "official_passes", "label": "Official PASS", "format": "number"},
                {"field": "official_pass_rate", "label": "Official rate", "format": "percent"},
                {"field": "v11_passes", "label": "v11 PASS", "format": "number"},
                {"field": "v11_pass_rate", "label": "v11 rate", "format": "percent"},
                {"field": "delta", "label": "v11 − Official", "format": "percent", "movement": True},
            ],
        },
        {
            "id": "taxonomy_flags_table",
            "title": "Overlapping failure mechanisms",
            "subtitle": "A failed task may be both visually grounded and missing a visible requirement",
            "showDescription": True,
            "dataset": "taxonomy_flags",
            "sourceId": "taxonomy_flags_query",
            "defaultSort": {"field": "failure_count", "direction": "desc"},
            "density": "spacious",
            "layout": "full",
            "columns": [
                {"field": "flag", "label": "Mechanism flag", "type": "text"},
                {"field": "failure_count", "label": "Failures /30", "format": "number"},
                {"field": "failure_rate", "label": "Rate", "format": "percent"},
            ],
        },
        {
            "id": "open_table",
            "title": "Twenty open-request proxy cases",
            "subtitle": "Designed coverage suite, not a sample of production user traffic",
            "showDescription": True,
            "dataset": "open_cases",
            "sourceId": "open_cases_query",
            "defaultSort": {"field": "id", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "id", "label": "Case", "type": "text"},
                {"field": "intent_class", "label": "Intent", "type": "text"},
                {"field": "mode", "label": "Mode", "type": "text"},
                {"field": "status", "label": "Status", "type": "text"},
                {"field": "first_pass", "label": "First pass", "type": "text"},
                {"field": "rework_rounds", "label": "Rework", "format": "number"},
                {"field": "unrelated_files_preserved", "label": "Preserved", "type": "text"},
                {"field": "duration_seconds", "label": "Seconds", "format": "number"},
            ],
        },
        {
            "id": "scale_index_table",
            "title": "Project-size adaptive retrieval",
            "subtitle": "Observation size stays bounded while pages grow with project size",
            "showDescription": True,
            "dataset": "scale_index",
            "sourceId": "scale_index_query",
            "defaultSort": {"field": "requested_text_files", "direction": "asc"},
            "density": "dense",
            "layout": "full",
            "columns": [
                {"field": "requested_text_files", "label": "Text files", "format": "number"},
                {"field": "pages", "label": "Pages", "format": "number"},
                {"field": "paged_files", "label": "Covered files", "format": "number"},
                {"field": "tail_target_found", "label": "Tail found", "type": "text"},
                {"field": "coverage_complete", "label": "Complete", "type": "text"},
                {"field": "maximum_list_observation_bytes", "label": "Page bytes", "format": "number"},
            ],
        },
        {
            "id": "iteration_table",
            "title": "General-mechanism iteration audit",
            "subtitle": "Partial conditions remain documented but are excluded from final quality estimates",
            "showDescription": True,
            "dataset": "iterations",
            "sourceId": "iterations_query",
            "defaultSort": {"field": "version", "direction": "asc"},
            "density": "spacious",
            "layout": "full",
            "columns": [
                {"field": "version", "label": "Version", "type": "text"},
                {"field": "status", "label": "Disposition", "type": "text"},
                {"field": "result", "label": "Result", "type": "text"},
                {"field": "general_change", "label": "General mechanism change", "type": "text"},
            ],
        },
        {
            "id": "task_table",
            "title": "Fifty-task paired audit",
            "subtitle": "Official and v4 are frozen; v11 is the final benchmark-path candidate",
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
                {"field": "official", "label": "Official", "type": "text"},
                {"field": "v4", "label": "v4", "type": "text"},
                {"field": "v11", "label": "v11", "type": "text"},
                {"field": "program_cell_errors", "label": "Cell errors", "format": "number"},
                {"field": "decision_errors", "label": "Decision errors", "format": "number"},
            ],
        },
    ]

    gap = v11["paired"]["candidate_minus_official"]
    blocks = [
        {"id": "title", "type": "markdown", "body": "# GameEngine Harness Fresh-50 与开放请求最终评估"},
        {
            "id": "answer_first",
            "type": "markdown",
            "sourceId": "report_data",
            "body": (
                "## 结论：控制层已可交付，但任务质量仍没有追平 Official\n\n"
                f"- **质量差距仍存在。** v11 在 Fresh-50 上通过 {final_candidate['passes']}/50（{final_candidate['pass_rate']:.0%}），Official 为 {official['passes']}/50（{official['pass_rate']:.0%}）；差值 {gap['estimate']:+.0%}，任务配对 95% CI [{gap['ci95'][0]:+.0%}, {gap['ci95'][1]:+.0%}]，exact McNemar p={v11['paired']['candidate_vs_official_mcnemar']['two_sided_exact_p']:.4f}。\n"
                f"- **差距不是超时或本地失败主导。** v11 为 0 BLOCKED、50/50 preservation PASS、0 policy violation，P95 {final_candidate['duration_p95_seconds']:.1f}s，低于 Official 的 {official['duration_p95_seconds']:.1f}s。\n"
                f"- **剩余失败主要是质量问题。** 30 个失败中 17 个涉及视觉/几何 grounding，20 个涉及可见要求漏实现，只有 5 个存在可观察的 Harness 贡献。\n"
                f"- **开放路径已闭环。** v13 的 20/20 设计案例符合预期；16 个实际执行案例的一次通过率为 {first_pass['rate']:.1%}，平均返工 {rework['mean']:.2f} 轮，20/20 保留无关文件。\n"
                "- **停止迭代。** 当前无重复的硬接口阻塞，规模与安全标准均通过；继续围绕已观察的 50 题调参会提高过拟合风险。下一次任务质量改进应使用新的预注册留出集。"
            ),
        },
        {"id": "metric_strip", "type": "metric-strip", "cardIds": ["quality_card", "open_card", "control_card", "cause_card"]},
        {
            "id": "quality_section",
            "type": "markdown",
            "sourceId": "report_data",
            "body": "## 1. 对 Official 的差距缩小了，但没有消失\n\nv11 相对 v4 增加 3 个 PASS（+6pp），配对区间 [-4pp, +16pp]，不足以证明稳定提升；相对 Official 仍少 9 个 PASS。3D 持平，但该层仅 4 题；主要缺口来自 2D（-24pp）、UI（-7.1pp）与 script/resource（-28.6pp）。这支持继续提升 grounding 和资源语义正确性，而不是扩大超时。",
        },
        {"id": "quality_chart_block", "type": "chart", "chartId": "quality_chart"},
        {"id": "condition_table_block", "type": "table", "tableId": "condition_table"},
        {"id": "pairwise_table_block", "type": "table", "tableId": "pairwise_table"},
        {"id": "strata_table_block", "type": "table", "tableId": "strata_table"},
        {
            "id": "taxonomy_section",
            "type": "markdown",
            "sourceId": "taxonomy_analysis",
            "body": "## 2. 失败归因：视觉 grounding 与漏实现高度重叠\n\n主因互斥分类为 14 个视觉/几何、7 个资源语义接线、6 个可见要求漏实现、2 个 evaluator 契约错位、1 个坏夹具依赖。重叠标签更适合回答机制问题：17/30 涉及视觉/几何，20/30 漏掉至少一个公开可见要求，5/30 有 Harness 贡献。对 Official 能通过而 v11 失败的 10 题，8 个涉及视觉/几何、6 个涉及漏实现、仅 1 个有 Harness 贡献。归因是单人事后证据审阅，不是盲法标注；原始逐题 rationale 已保留。",
        },
        {"id": "taxonomy_chart_block", "type": "chart", "chartId": "taxonomy_chart"},
        {"id": "taxonomy_flags_table_block", "type": "table", "tableId": "taxonomy_flags_table"},
        {
            "id": "scale_section",
            "type": "markdown",
            "sourceId": "scale_measurements",
            "body": "## 3. 大项目应自适应总探索量，而不是线性放大单次 context\n\n4,096 个文本文件可在 8 个有界页面中完整遍历并找到尾部目标；2 MiB 文件以 32 页重建，单页最大约 65.9 KiB。512 与 2,048 个变量的状态投影都约 25.2 KiB，并在诊断位于首尾时保留它。证据支持：单次 observation 保持硬上限，项目越大只增加分页数、搜索轮数或总探索预算；不按文件数或变量数线性扩大每次 prompt。生产化时应把页数/总读取字节/墙钟设为分段函数并保留硬上限。",
        },
        {"id": "scale_index_table_block", "type": "table", "tableId": "scale_index_table"},
        {
            "id": "open_section",
            "type": "markdown",
            "sourceId": "open_analysis",
            "body": (
                "## 4. 开放请求：覆盖通过，真实用户指标仍待遥测\n\n"
                f"5 个只读问答、4 个故意未审批的模糊修改、8 个限定修改和 3 个创建任务全部符合预期。故意未审批组 {safe['observed']}/{safe['eligible_cases']} 安全停下，但这只能称为 **safe disposition rate**，不能称作真实用户澄清率。16 个执行案例中 {first_pass['observed']} 个无程序/决策错误地一次通过；返工中位数 {rework['median']}、均值 {rework['mean']:.2f}、最大 {rework['maximum']}。所有 20 例无关文件均保留。真实分布仍需要记录自然请求、澄清轮、审批迁移、完成后 reopen、人工验收与隐私保留策略。"
            ),
        },
        {"id": "rework_chart_block", "type": "chart", "chartId": "rework_chart"},
        {"id": "open_table_block", "type": "table", "tableId": "open_table"},
        {
            "id": "iteration_section",
            "type": "markdown",
            "sourceId": "iteration_log",
            "body": "## 5. 迭代记录与版本边界\n\nv8 因用户并发修复触发 runtime guard，v9 因读写后缀边界问题中止；两者均保留但不计入最终质量。v10 首次消除 BLOCKED/基础设施失败，v11 修复多编码公共文本后完整重跑 Fresh-50。v12 的 20/20 开放套件暴露 Unity 缺少系统契约已要求的读/审阅能力；v13 只补齐 project-query/Unity 路径，因此不伪装成新的 Fresh-50 运行，Fresh-50 结果沿用行为隔离的 v11。",
        },
        {"id": "iteration_table_block", "type": "table", "tableId": "iteration_table"},
        {"id": "task_table_block", "type": "table", "tableId": "task_table"},
        {
            "id": "methodology",
            "type": "markdown",
            "body": "## 方法与可复现性\n\nFresh-50 清单在首次运行前冻结，包含 25 个 2D、14 个 UI、4 个 3D、7 个 script/resource 家族新题；模型固定 `gpt-5.6-sol`、reasoning effort `medium`，主要终点为官方 evaluator PASS。每格只运行一次，任务是配对推断单位；区间为固定 seed 的 100,000 次任务配对 bootstrap，方向变化用 exact McNemar。v6-v11 都是对已观察 Fresh-50 的工程回归，不重新声称“未见”。v13 开放套件固定 20 个设计案例，收据、日志、结果 hash、配额与源项目 hash 都经分析器验证。",
        },
        {
            "id": "limitations",
            "type": "markdown",
            "body": "## 局限\n\n- 每题每条件仅一次，不能估计模型同题方差；后续版本差异混合真实机制变化与采样波动。\n- 50 题在首次 Official/v4 比较后已被观察；v11 对 v4 是回归证据，不是独立泛化验证。\n- 失败归因为单审阅者事后标注，并允许重叠；可复核但没有 inter-rater reliability。\n- 开放套件是人工设计的覆盖代理，不代表真实请求类别占比、澄清率或成功率。\n- v12-v13 修改与 GameDevBench 路径隔离，因此没有再消耗 50 次模型运行；这是代码路径继承，不是声称跑过 v13 Fresh-50。\n- Official 与 Harness 的 token/cache 记账口径不同，不据此作成本优劣结论。",
        },
        {
            "id": "delivery",
            "type": "markdown",
            "body": "## 交付与下一轮门槛\n\n1. 将 v13 作为当前控制层交付候选：开放探索、审批写入、基线感知验证、preservation 与有界 observation 均保留。\n2. 不再针对这 50 个已观察任务增加提示词、坐标或资源专用修复。\n3. 下一轮若要宣称质量提升，先冻结新的 50 题或真实项目留出集；优先加入可渲染的视觉回看、逐项需求核对和资源语义断言。\n4. 生产开放请求遥测单独统计自然澄清率、一次通过率、返工轮数、reopen 与无关文件保留，不与本报告的设计代理混合。",
        },
    ]

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "GameEngine Harness Fresh-50 与开放请求最终评估",
            "description": "A paired Fresh-50 quality comparison, scale probe, failure taxonomy, and 20-case open-request evaluation for the final adaptive programmable Harness.",
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
                "pairwise": pairwise,
                "strata": strata,
                "taxonomy_primary": taxonomy_primary,
                "taxonomy_flags": taxonomy_flags,
                "open_cases": open_cases,
                "open_rework": open_rework,
                "scale_index": scale_index,
                "scale_projection": scale_projection,
                "scale_files": scale_files,
                "tasks": tasks,
                "iterations": iterations,
            },
        },
        "sources": file_sources + query_sources,
    }
    _write(REPORT / "artifact.json", artifact)
    print(REPORT / "artifact.json")


if __name__ == "__main__":
    main()
