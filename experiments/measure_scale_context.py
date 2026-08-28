#!/usr/bin/env python3
"""Measure public text discovery and program-state projection across project scales."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from gameforge.benchmarking.gamedevbench import GodotHarnessEngineAdapter
from gameforge.harness.program_runtime import _bounded_program_state
from gameforge.harness.project_scope import ProjectAccessScope

ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _index_case(root: Path, file_count: int) -> dict[str, object]:
    project = root / f"project-{file_count}"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    ordinary_count = file_count - 2
    for index in range(ordinary_count):
        (scripts / f"file_{index:05d}.gd").write_text(
            f"extends Node\nvar ordinary_{index} = {index}\n",
            encoding="utf-8",
        )
    target = scripts / "zz_target.gd"
    target.write_text(
        "extends Node\nconst SCALE_SENTINEL = 'needle-at-project-tail'\n",
        encoding="utf-8",
    )
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=root / f"run-{file_count}",
        editable_paths=("project.godot",),
        godot=root / "unused-godot",
    )
    scope = ProjectAccessScope(("project.godot",), allow_text_scope_expansion=True)
    started = time.perf_counter()
    adapter.bind_project_scope(scope)
    bind_seconds = time.perf_counter() - started

    started = time.perf_counter()
    listed = adapter.invoke("list_project_files", {})
    list_seconds = time.perf_counter() - started
    pages = 1
    paged_paths = [item["path"] for item in listed["files"]]
    cursor = listed.get("next_cursor")
    while cursor is not None:
        page = adapter.invoke("list_project_files", {"cursor": cursor})
        pages += 1
        paged_paths.extend(item["path"] for item in page["files"])
        cursor = page.get("next_cursor")
    started = time.perf_counter()
    searched = adapter.invoke("search_project_text", {"query": "SCALE_SENTINEL"})
    search_seconds = time.perf_counter() - started
    list_bytes = len(json.dumps(listed, sort_keys=True).encode("utf-8"))
    search_bytes = len(json.dumps(searched, sort_keys=True).encode("utf-8"))
    matches = searched["matches"]
    return {
        "requested_text_files": file_count,
        "reported_file_count_total": listed["file_count_total"],
        "listed_files": len(listed["files"]),
        "paged_files": len(paged_paths),
        "list_pages": pages,
        "pagination_complete": len(paged_paths) == listed["file_count_indexed"],
        "list_coverage_complete": listed["coverage_complete"],
        "list_truncated": listed["truncated"],
        "search_files_indexed": searched["files_indexed"],
        "search_files_scanned": searched["files_scanned"],
        "search_truncated": searched["truncated"],
        "tail_target_found": any(
            match["path"] == "scripts/zz_target.gd" for match in matches
        ),
        "bind_seconds": bind_seconds,
        "list_seconds": list_seconds,
        "search_seconds": search_seconds,
        "list_observation_bytes": list_bytes,
        "search_observation_bytes": search_bytes,
    }


def _projection_case(variable_count: int, diagnostic_position: str) -> dict[str, object]:
    variables: dict[str, object] = {}
    diagnostic = {
        "status": "failed",
        "diagnostics": ["Parse Error: SCALE_DIAGNOSTIC_SENTINEL"],
    }
    if diagnostic_position == "first":
        variables["import_result"] = diagnostic
    for index in range(variable_count - 1):
        variables[f"value_{index:05d}"] = {
            "status": "success",
            "value": index,
            "label": f"bounded-value-{index}",
        }
    if diagnostic_position == "last":
        variables["import_result"] = diagnostic
    started = time.perf_counter()
    bounded = _bounded_program_state(variables)
    duration = time.perf_counter() - started
    encoded = json.dumps(bounded, sort_keys=True)
    retained = "SCALE_DIAGNOSTIC_SENTINEL" in encoded
    projected = bounded.get("import_result")
    summarized = isinstance(projected, dict) and projected.get("truncated") is True
    return {
        "variable_count": variable_count,
        "diagnostic_position": diagnostic_position,
        "projection_bytes": len(encoded.encode("utf-8")),
        "projection_seconds": duration,
        "diagnostic_value_retained": retained,
        "diagnostic_summarized": summarized,
        "whole_state_fallback": bounded.get("truncated") is True,
        "projected_key_count": len(bounded),
    }


def _file_paging_case(root: Path, file_bytes: int) -> dict[str, object]:
    project = root / f"large-file-{file_bytes}"
    source = project / "scripts" / "large.gd"
    source.parent.mkdir(parents=True)
    content = "x" * file_bytes
    source.write_text(content, encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=project,
        run_directory=root / f"large-file-run-{file_bytes}",
        editable_paths=("scripts/large.gd",),
        godot=root / "unused-godot",
    )
    pages = 0
    cursor: int | None = 0
    reconstructed: list[str] = []
    observation_bytes: list[int] = []
    started = time.perf_counter()
    while cursor is not None:
        page = adapter.invoke(
            "read_text_file",
            {"path": "scripts/large.gd", "cursor": cursor},
        )
        pages += 1
        reconstructed.append(str(page["content"]))
        observation_bytes.append(len(json.dumps(page, sort_keys=True).encode("utf-8")))
        cursor = page["next_cursor"]  # type: ignore[assignment]
    duration = time.perf_counter() - started
    return {
        "file_bytes": file_bytes,
        "pages": pages,
        "coverage_complete": "".join(reconstructed) == content,
        "maximum_observation_bytes": max(observation_bytes),
        "total_observation_bytes": sum(observation_bytes),
        "paging_seconds": duration,
    }


def main() -> int:
    arguments = _arguments()
    with tempfile.TemporaryDirectory(prefix="gameforge-scale-") as temporary:
        temporary_path = Path(temporary)
        index_cases = [
            _index_case(temporary_path, count)
            for count in (128, 512, 2_048, 4_096)
        ]
        file_paging_cases = [
            _file_paging_case(temporary_path, size)
            for size in (64 * 1024, 512 * 1024, 2 * 1024 * 1024)
        ]
    projection_cases = [
        _projection_case(count, position)
        for count in (32, 128, 512, 2_048)
        for position in ("first", "last")
    ]
    payload = {
        "schema_version": 1,
        "label": arguments.label,
        "generated_at": datetime.now(UTC).isoformat(),
        "index_cases": index_cases,
        "file_paging_cases": file_paging_cases,
        "projection_cases": projection_cases,
    }
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
