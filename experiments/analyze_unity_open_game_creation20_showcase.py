#!/usr/bin/env python3
"""Aggregate and render evidence from the frozen Unity open-creation showcase."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

DEFAULT_RUN = Path(
    "runs/experiments/unity-open-game-creation20-showcase-run3"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot compute a percentile for an empty list")
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: list[float], digits: int = 3) -> dict[str, float]:
    return {
        "min": round(min(values), digits),
        "median": round(statistics.median(values), digits),
        "mean": round(statistics.fmean(values), digits),
        "p90": round(_percentile(values, 0.9), digits),
        "max": round(max(values), digits),
        "total": round(sum(values), digits),
    }


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _image_metrics(path: Path) -> dict[str, Any]:
    with Image.open(path) as source:
        rgb = source.convert("RGB")
        grayscale = rgb.convert("L")
        stat = ImageStat.Stat(grayscale)
        histogram = grayscale.histogram()
        total = grayscale.width * grayscale.height
        entropy = 0.0
        for count in histogram:
            if count:
                probability = count / total
                entropy -= probability * math.log2(probability)
        non_dark = sum(histogram[8:]) / total
        return {
            "width": rgb.width,
            "height": rgb.height,
            "mean_luminance": round(stat.mean[0], 3),
            "luminance_stddev": round(stat.stddev[0], 3),
            "entropy_bits": round(entropy, 3),
            "non_dark_fraction": round(non_dark, 6),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }


def _pair_metrics(initial_path: Path, later_path: Path) -> dict[str, Any]:
    with Image.open(initial_path) as initial_source, Image.open(later_path) as later_source:
        initial = initial_source.convert("RGB")
        later = later_source.convert("RGB")
        if initial.size != later.size:
            raise ValueError(f"screenshot sizes differ: {initial_path} and {later_path}")
        difference = ImageChops.difference(initial, later)
        stat = ImageStat.Stat(difference)
        rms = math.sqrt(sum(channel**2 for channel in stat.rms) / len(stat.rms))
        grayscale = difference.convert("L")
        histogram = grayscale.histogram()
        pixels = initial.width * initial.height
        changed = sum(histogram[5:]) / pixels
        bbox = difference.getbbox()
        return {
            "rms_difference": round(rms, 3),
            "changed_pixel_fraction_over_4": round(changed, 6),
            "pixel_identical": bbox is None,
        }


def _fit_image(path: Path, width: int, height: int) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), "black")
        left = (width - image.width) // 2
        top = (height - image.height) // 2
        canvas.paste(image, (left, top))
        return canvas


def _render_initial_sheet(rows: list[dict[str, Any]], output: Path) -> None:
    columns = 5
    image_width, image_height = 256, 144
    label_height = 28
    row_count = math.ceil(len(rows) / columns)
    sheet = Image.new(
        "RGB", (columns * image_width, row_count * (image_height + label_height)), "#15171b"
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=14)
    for index, row in enumerate(rows):
        column = index % columns
        line = index // columns
        x = column * image_width
        y = line * (image_height + label_height)
        sheet.paste(_fit_image(Path(row["initial_screenshot"]), image_width, image_height), (x, y))
        draw.text(
            (x + 6, y + image_height + 6),
            f'{row["ordinal"]:02d} {row["task_id"]}',
            fill="#f2f2f2",
            font=font,
        )
    sheet.save(output, optimize=True)


def _render_pair_sheets(rows: list[dict[str, Any]], output_directory: Path) -> list[str]:
    page_size = 5
    image_width, image_height = 480, 270
    label_height = 32
    page_paths: list[str] = []
    font = ImageFont.load_default(size=15)
    header_font = ImageFont.load_default(size=13)
    for page_index, start in enumerate(range(0, len(rows), page_size), start=1):
        page_rows = rows[start : start + page_size]
        sheet = Image.new(
            "RGB", (image_width * 2, len(page_rows) * (image_height + label_height)), "#15171b"
        )
        draw = ImageDraw.Draw(sheet)
        for line, row in enumerate(page_rows):
            y = line * (image_height + label_height)
            initial = _fit_image(Path(row["initial_screenshot"]), image_width, image_height)
            later = _fit_image(Path(row["later_screenshot"]), image_width, image_height)
            sheet.paste(initial, (0, y))
            sheet.paste(later, (image_width, y))
            label_y = y + image_height + 7
            draw.text(
                (8, label_y),
                f'{row["ordinal"]:02d} {row["task_id"]}',
                fill="#f2f2f2",
                font=font,
            )
            draw.text((image_width - 54, label_y), "initial", fill="#aeb6c2", font=header_font)
            draw.text((image_width * 2 - 48, label_y), "later", fill="#aeb6c2", font=header_font)
        path = output_directory / f"contact-sheet-pairs-{page_index:02d}.png"
        sheet.save(path, optimize=True)
        page_paths.append(str(path.resolve()))
    return page_paths


def analyze(run_directory: Path) -> dict[str, Any]:
    receipt_paths = sorted((run_directory / "receipts").glob("*.json"))
    if len(receipt_paths) != 20:
        raise ValueError(f"expected 20 receipts, found {len(receipt_paths)}")
    receipts = [json.loads(path.read_text()) for path in receipt_paths]
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for receipt, receipt_path in zip(receipts, receipt_paths, strict=True):
        evaluation = receipt["evaluation"]
        harness_run_directory = Path(receipt["harness_run_directory"])
        harness_result_path = harness_run_directory / "result.json"
        agent_result_path = harness_run_directory / "agent-result.json"
        audit_path = harness_run_directory / "agent-tool-audit.json"
        harness_result = json.loads(harness_result_path.read_text())
        agent_result = json.loads(agent_result_path.read_text())
        audit = json.loads(audit_path.read_text())
        solver_session_seconds = (
            _parse_timestamp(harness_result["ended_at"])
            - _parse_timestamp(harness_result["started_at"])
        ).total_seconds()
        command_events = [
            event for event in audit["events"] if event["kind"] == "command_execution"
        ]
        initial_path = Path(evaluation["initial_screenshot"])
        later_path = Path(evaluation["later_screenshot"])
        build_path = Path(evaluation["build_application"])
        for required in (initial_path, later_path, build_path, Path(receipt["workspace"])):
            if not required.exists():
                missing.append(str(required))
        rows.append(
            {
                "ordinal": receipt["ordinal"],
                "task_id": receipt["task_id"],
                "genre": receipt["genre"],
                "classification": receipt["classification"],
                "hard_gate": receipt["hard_gate"],
                "end_to_end_attempt_seconds": receipt["duration_seconds"],
                "solver_session_seconds": round(solver_session_seconds, 3),
                "usage": receipt["usage"],
                "build_duration_seconds": evaluation["build_process"]["duration_seconds"],
                "runtime_duration_seconds": evaluation["runtime_process"]["duration_seconds"],
                "runtime_metadata": evaluation["runtime_metadata"],
                "source_metrics": evaluation["source_metrics"],
                "initial_screenshot": str(initial_path.resolve()),
                "later_screenshot": str(later_path.resolve()),
                "initial_image_metrics": _image_metrics(initial_path),
                "later_image_metrics": _image_metrics(later_path),
                "temporal_image_metrics": _pair_metrics(initial_path, later_path),
                "receipt": str(receipt_path.resolve()),
                "receipt_sha256": _sha256(receipt_path),
                "solver_audit": {
                    "transport_attempts": agent_result["transport_attempts"],
                    "outside_project_changes_reported": agent_result[
                        "outside_project_changes_reported"
                    ],
                    "policy_violations": harness_result["policy_violations"],
                    "lineage_complete": harness_result["lineage_complete"],
                    "successful_command_events": sum(
                        event["status"] == "completed" for event in command_events
                    ),
                    "failed_command_events": sum(
                        event["status"] == "failed" for event in command_events
                    ),
                    "model_built_app_count": len(
                        list((Path(receipt["workspace"]) / "Builds").glob("**/*.app"))
                    ),
                },
            }
        )

    attempt_durations = [float(row["end_to_end_attempt_seconds"]) for row in rows]
    solver_durations = [float(row["solver_session_seconds"]) for row in rows]
    build_durations = [float(row["build_duration_seconds"]) for row in rows]
    runtime_durations = [float(row["runtime_duration_seconds"]) for row in rows]
    tool_calls = [float(row["usage"]["tool_calls"]) for row in rows]
    input_tokens = [float(row["usage"]["input_tokens"]) for row in rows]
    cached_tokens = [float(row["usage"]["cached_input_tokens"]) for row in rows]
    output_tokens = [float(row["usage"]["output_tokens"]) for row in rows]
    reasoning_tokens = [float(row["usage"]["reasoning_output_tokens"]) for row in rows]
    image_changes = [
        float(row["temporal_image_metrics"]["changed_pixel_fraction_over_4"])
        for row in rows
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_directory": str(run_directory.resolve()),
        "receipt_count": len(rows),
        "classification_counts": {
            status: sum(row["classification"] == status for row in rows)
            for status in sorted({row["classification"] for row in rows})
        },
        "hard_gate_counts": {
            status: sum(row["hard_gate"] == status for row in rows)
            for status in sorted({row["hard_gate"] for row in rows})
        },
        "missing_artifacts": missing,
        "distributions": {
            "end_to_end_attempt_seconds": _distribution(attempt_durations),
            "solver_session_seconds": _distribution(solver_durations),
            "independent_build_duration_seconds": _distribution(build_durations),
            "runtime_probe_duration_seconds": _distribution(runtime_durations),
            "tool_calls": _distribution(tool_calls),
            "input_tokens": _distribution(input_tokens, 0),
            "cached_input_tokens": _distribution(cached_tokens, 0),
            "output_tokens": _distribution(output_tokens, 0),
            "reasoning_output_tokens": _distribution(reasoning_tokens, 0),
            "changed_pixel_fraction_over_4": _distribution(image_changes, 6),
        },
        "solver_audit_totals": {
            "transport_attempts": sum(
                row["solver_audit"]["transport_attempts"] for row in rows
            ),
            "outside_project_changes_reported": sum(
                row["solver_audit"]["outside_project_changes_reported"] for row in rows
            ),
            "policy_violations": sum(
                row["solver_audit"]["policy_violations"] for row in rows
            ),
            "lineage_complete": sum(
                row["solver_audit"]["lineage_complete"] for row in rows
            ),
            "successful_command_events": sum(
                row["solver_audit"]["successful_command_events"] for row in rows
            ),
            "failed_command_events": sum(
                row["solver_audit"]["failed_command_events"] for row in rows
            ),
            "tasks_with_model_built_app": sum(
                row["solver_audit"]["model_built_app_count"] > 0 for row in rows
            ),
        },
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-directory", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    run_directory = args.run_directory.resolve()
    output_directory = run_directory / "analysis"
    output_directory.mkdir(parents=True, exist_ok=True)
    result = analyze(run_directory)
    initial_sheet = output_directory / "contact-sheet-initial.png"
    _render_initial_sheet(result["rows"], initial_sheet)
    result["contact_sheets"] = {
        "initial": str(initial_sheet.resolve()),
        "pairs": _render_pair_sheets(result["rows"], output_directory),
    }
    summary_path = output_directory / "summary.json"
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
