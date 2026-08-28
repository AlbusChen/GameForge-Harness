#!/usr/bin/env python3
"""Replay audited Codex patches forward from an exact runtime snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from experiments.recover_runtime_snapshot_from_codex_log import (
    _find_chunk_range,
    _parse_hunks,
    _runtime_digest,
    _runtime_relative,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-log", type=Path, required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--through", required=True)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--current-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--expected-file-count", type=int, required=True)
    return parser.parse_args()


def _changes_between(
    session_log: Path,
    *,
    after: str,
    through: str,
    current_root: Path,
) -> list[tuple[str, str, dict[str, Any]]]:
    changes: list[tuple[str, str, dict[str, Any]]] = []
    logs = sorted(session_log.rglob("*.jsonl")) if session_log.is_dir() else [session_log]
    seen: set[tuple[str, str, str]] = set()
    for log in logs:
        with log.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                payload = event.get("payload", {})
                timestamp = event.get("timestamp", "")
                if (
                    not after < timestamp <= through
                    or event.get("type") != "event_msg"
                    or payload.get("type") != "patch_apply_end"
                    or payload.get("success") is not True
                ):
                    continue
                for raw_path, change in payload.get("changes", {}).items():
                    relative = _runtime_relative(raw_path, current_root)
                    if relative is None:
                        continue
                    key = (
                        timestamp,
                        relative,
                        json.dumps(change, sort_keys=True, separators=(",", ":")),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    changes.append((timestamp, relative, change))
    return sorted(changes, key=lambda item: item[0])


def _copy_runtime(base_root: Path, output_root: Path) -> None:
    if output_root.exists():
        raise RuntimeError(f"refusing to overwrite replay directory: {output_root}")
    output_root.mkdir(parents=True)
    shutil.copytree(
        base_root / "gameforge",
        output_root / "gameforge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(base_root / name, output_root / name)


def _forward_update(path: Path, unified_diff: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for hunk in reversed(_parse_hunks(unified_diff)):
        expected = max(0, hunk.old_start - 1)
        start, end = _find_chunk_range(lines, hunk.old_lines, expected)
        lines[start:end] = hunk.new_lines
    path.write_text("".join(lines), encoding="utf-8")


def _forward_change(output_root: Path, relative: str, change: dict[str, Any]) -> None:
    path = output_root / relative
    change_type = change.get("type")
    if change_type == "update":
        if not path.is_file():
            raise RuntimeError(f"updated runtime file is missing: {relative}")
        unified_diff = change.get("unified_diff")
        if not isinstance(unified_diff, str):
            raise RuntimeError(f"runtime update lacks unified diff: {relative}")
        _forward_update(path, unified_diff)
        return
    if change_type == "add":
        if path.exists():
            raise RuntimeError(f"added runtime path already exists: {relative}")
        content = change.get("content")
        if not isinstance(content, str):
            raise RuntimeError(f"runtime add lacks content: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return
    if change_type == "delete":
        if not path.is_file():
            raise RuntimeError(f"deleted runtime file is missing: {relative}")
        path.unlink()
        return
    raise RuntimeError(f"unsupported runtime change {change_type!r}: {relative}")


def main() -> int:
    arguments = _arguments()
    base_root = arguments.base_root.resolve(strict=True)
    current_root = arguments.current_root.resolve(strict=True)
    session_log = arguments.session_log.resolve(strict=True)
    output_root = arguments.output_root.resolve()
    _copy_runtime(base_root, output_root)
    changes = _changes_between(
        session_log,
        after=arguments.after,
        through=arguments.through,
        current_root=current_root,
    )
    for timestamp, relative, change in changes:
        print(f"forward {timestamp} {change.get('type')} {relative}")
        _forward_change(output_root, relative, change)
    digest, file_count = _runtime_digest(output_root)
    print(f"runtime_tree_sha256={digest}")
    print(f"runtime_file_count={file_count}")
    if digest != arguments.expected_digest or file_count != arguments.expected_file_count:
        raise RuntimeError(
            "replayed runtime does not match the historical freeze: "
            f"expected {arguments.expected_digest}/{arguments.expected_file_count}, "
            f"got {digest}/{file_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
