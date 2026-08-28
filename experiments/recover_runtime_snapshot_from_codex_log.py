#!/usr/bin/env python3
"""Recover an exact historical runtime tree from Codex patch audit events."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_PATHS = ("gameforge", "pyproject.toml", "uv.lock")
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


@dataclass(frozen=True)
class Hunk:
    old_start: int
    new_start: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-log", type=Path, required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--current-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--expected-file-count", type=int, required=True)
    return parser.parse_args()


def _runtime_relative(path: str, current_root: Path) -> str | None:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(current_root)
        except ValueError:
            return None
    else:
        relative = candidate
    normalized = relative.as_posix()
    if normalized in {"pyproject.toml", "uv.lock"} or normalized.startswith("gameforge/"):
        return normalized
    return None


def _successful_runtime_changes(
    session_log: Path,
    *,
    cutoff: str,
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
                if (
                    event.get("timestamp", "") <= cutoff
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
                        event["timestamp"],
                        relative,
                        json.dumps(change, sort_keys=True, separators=(",", ":")),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    changes.append((event["timestamp"], relative, change))
    return sorted(changes, key=lambda item: item[0])


def _parse_hunks(unified_diff: str) -> list[Hunk]:
    lines = unified_diff.splitlines(keepends=True)
    hunks: list[Hunk] = []
    index = 0
    while index < len(lines):
        header = lines[index].rstrip("\n")
        matched = HUNK_HEADER.match(header)
        if matched is None:
            raise RuntimeError(f"unsupported unified diff line: {header!r}")
        old_count = int(matched.group(2) or "1")
        new_start = int(matched.group(3))
        new_count = int(matched.group(4) or "1")
        index += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            index += 1
            if line.startswith("\\ No newline at end of file"):
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise RuntimeError(f"unsupported hunk line: {line!r}")
            content = line[1:]
            if line[0] in {" ", "-"}:
                old_lines.append(content)
            if line[0] in {" ", "+"}:
                new_lines.append(content)
        if len(old_lines) != old_count or len(new_lines) != new_count:
            raise RuntimeError(
                "unified diff hunk counts do not match: "
                f"old {len(old_lines)} != {old_count}, new {len(new_lines)} != {new_count}"
            )
        hunks.append(Hunk(int(matched.group(1)), new_start, tuple(old_lines), tuple(new_lines)))
    return hunks


def _find_chunk_range(lines: list[str], chunk: tuple[str, ...], expected: int) -> tuple[int, int]:
    if lines[expected : expected + len(chunk)] == list(chunk):
        return expected, expected + len(chunk)
    matches = [
        index
        for index in range(len(lines) - len(chunk) + 1)
        if lines[index : index + len(chunk)] == list(chunk)
    ]
    if len(matches) == 1:
        return matches[0], matches[0] + len(chunk)

    # Ruff may have restored module-level blank lines after the recorded patch.
    # Match the same non-empty context while consuming those formatting-only lines.
    significant = [line for line in chunk if line.strip()]
    if not significant:
        return expected, expected
    flexible: list[tuple[int, int]] = []
    maximum_window = len(chunk) + 8
    for start in range(max(0, expected - 8), min(len(lines), expected + 9)):
        for end in range(start + 1, min(len(lines), start + maximum_window) + 1):
            if [line for line in lines[start:end] if line.strip()] == significant:
                flexible.append((start, end))
    if not flexible:
        # A formatter can also reflow a condition or call across a different
        # number of physical lines.  Match the exact non-whitespace token stream
        # in a small local window; the historical hunk still supplies the bytes
        # that replace it, so this only relaxes how we locate the new-side text.
        compact_chunk = "".join("".join(line.split()) for line in chunk)
        reformatted: list[tuple[int, int]] = []
        maximum_reflow_window = len(chunk) + 12
        for start in range(len(lines)):
            for end in range(
                start + 1,
                min(len(lines), start + maximum_reflow_window) + 1,
            ):
                compact_window = "".join("".join(line.split()) for line in lines[start:end])
                if compact_window == compact_chunk:
                    reformatted.append((start, end))
        if reformatted:
            print(f"  line-reflow match +{expected + 1}")
            return min(
                reformatted,
                key=lambda match: (abs(match[0] - expected), match[1] - match[0]),
            )
        raise RuntimeError(f"reverse hunk did not match near line {expected + 1}: exact={matches}")
    print(f"  blank-line match +{expected + 1}")
    return min(flexible, key=lambda match: (abs(match[0] - expected), match[1] - match[0]))


def _compact_with_offsets(text: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        if character.isspace():
            continue
        compact.append(character)
        offsets.append(index)
    return "".join(compact), offsets


def _compact_boundary(offsets: list[int], compact_index: int, raw_length: int) -> int:
    if compact_index <= 0:
        return 0
    if compact_index >= len(offsets):
        return raw_length
    return offsets[compact_index - 1] + 1


def _reverse_reformatted_hunk(text: str, hunk: Hunk, expected: int) -> str:
    """Reverse a hunk whose logical tokens survived formatter line reflow."""

    old_raw = "".join(hunk.old_lines)
    new_raw = "".join(hunk.new_lines)
    old_compact, old_offsets = _compact_with_offsets(old_raw)
    new_compact, _ = _compact_with_offsets(new_raw)
    if old_compact == new_compact:
        return text
    current_compact, current_offsets = _compact_with_offsets(text)
    prefix = 0
    limit = min(len(old_compact), len(new_compact))
    while prefix < limit and old_compact[prefix] == new_compact[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(old_compact) - prefix
        and suffix < len(new_compact) - prefix
        and old_compact[-suffix - 1] == new_compact[-suffix - 1]
    ):
        suffix += 1
    matches: list[int] = []
    cursor = 0
    while new_compact:
        match = current_compact.find(new_compact, cursor)
        if match < 0:
            break
        matches.append(match)
        cursor = match + 1
    expected_raw = sum(len(line) for line in text.splitlines(keepends=True)[:expected])
    if not matches:
        # Ruff can normalize adjacent string literals as well as line wrapping.
        # In that case the whole new side is not byte/token identical, but the
        # unchanged prefix and suffix still delimit the exact historical change.
        prefix_anchor = new_compact[:prefix]
        if len(prefix_anchor) < 16 or suffix < 16:
            raise RuntimeError(f"reformatted reverse hunk did not match near line {expected + 1}")
        prefix_matches: list[int] = []
        cursor = 0
        while True:
            match = current_compact.find(prefix_anchor, cursor)
            if match < 0:
                break
            prefix_matches.append(match)
            cursor = match + 1
        if not prefix_matches:
            raise RuntimeError(
                f"reformatted reverse hunk prefix did not match near line {expected + 1}"
            )
        compact_start = min(
            prefix_matches,
            key=lambda index: abs(current_offsets[index] - expected_raw),
        )
        current_change_compact_start = compact_start + prefix
        suffix_start = -1
        retained_suffix = suffix
        for candidate_length in dict.fromkeys(
            (suffix, min(suffix, 128), min(suffix, 64), min(suffix, 32), 16)
        ):
            suffix_anchor = new_compact[len(new_compact) - candidate_length :]
            suffix_start = current_compact.find(
                suffix_anchor,
                current_change_compact_start,
            )
            if suffix_start >= 0:
                retained_suffix = candidate_length
                break
        if suffix_start < 0:
            raise RuntimeError(
                f"reformatted reverse hunk suffix did not match near line {expected + 1}"
            )
        current_change_start = _compact_boundary(
            current_offsets,
            current_change_compact_start,
            len(text),
        )
        current_change_end = _compact_boundary(
            current_offsets,
            suffix_start,
            len(text),
        )
        old_change_start = _compact_boundary(old_offsets, prefix, len(old_raw))
        old_change_end = _compact_boundary(
            old_offsets,
            len(old_compact) - retained_suffix,
            len(old_raw),
        )
        return (
            text[:current_change_start]
            + old_raw[old_change_start:old_change_end]
            + text[current_change_end:]
        )

    compact_start = min(
        matches,
        key=lambda index: abs(current_offsets[index] - expected_raw),
    )
    current_change_start = _compact_boundary(
        current_offsets,
        compact_start + prefix,
        len(text),
    )
    current_change_end = _compact_boundary(
        current_offsets,
        compact_start + len(new_compact) - suffix,
        len(text),
    )
    old_change_start = _compact_boundary(old_offsets, prefix, len(old_raw))
    old_change_end = _compact_boundary(
        old_offsets,
        len(old_compact) - suffix,
        len(old_raw),
    )
    return (
        text[:current_change_start]
        + old_raw[old_change_start:old_change_end]
        + text[current_change_end:]
    )


def _reverse_update(path: Path, unified_diff: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for hunk in reversed(_parse_hunks(unified_diff)):
        expected = max(0, hunk.new_start - 1)
        try:
            start, end = _find_chunk_range(lines, hunk.new_lines, expected)
        except RuntimeError:
            print(f"  formatter fallback +{hunk.new_start} {path.name}")
            text = _reverse_reformatted_hunk("".join(lines), hunk, expected)
            lines = text.splitlines(keepends=True)
        else:
            lines[start:end] = hunk.old_lines
    path.write_text("".join(lines), encoding="utf-8")


def _copy_current_runtime(current_root: Path, output_root: Path) -> None:
    if output_root.exists():
        raise RuntimeError(f"refusing to overwrite snapshot directory: {output_root}")
    output_root.mkdir(parents=True)
    shutil.copytree(
        current_root / "gameforge",
        output_root / "gameforge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(current_root / name, output_root / name)


def _reverse_change(output_root: Path, relative: str, change: dict[str, Any]) -> None:
    path = output_root / relative
    change_type = change.get("type")
    if change_type == "update":
        if not path.is_file():
            raise RuntimeError(f"updated runtime file is missing: {relative}")
        unified_diff = change.get("unified_diff")
        if not isinstance(unified_diff, str):
            raise RuntimeError(f"runtime update lacks unified diff: {relative}")
        _reverse_update(path, unified_diff)
        return
    if change_type == "add":
        if not path.is_file():
            raise RuntimeError(f"added runtime file is missing during reversal: {relative}")
        path.unlink()
        return
    raise RuntimeError(f"unsupported historical runtime change {change_type!r}: {relative}")


def _runtime_digest(root: Path) -> tuple[str, int]:
    files: list[Path] = []
    for relative in RUNTIME_PATHS:
        candidate = root / relative
        if candidate.is_file():
            files.append(candidate)
            continue
        files.extend(
            path
            for path in candidate.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{relative}\0{file_digest}\n".encode())
    return digest.hexdigest(), len(files)


def main() -> int:
    arguments = _arguments()
    current_root = arguments.current_root.resolve(strict=True)
    session_log = arguments.session_log.resolve(strict=True)
    output_root = arguments.output_root.resolve()
    _copy_current_runtime(current_root, output_root)
    changes = _successful_runtime_changes(
        session_log,
        cutoff=arguments.cutoff,
        current_root=current_root,
    )
    for timestamp, relative, change in reversed(changes):
        print(f"reverse {timestamp} {change.get('type')} {relative}")
        _reverse_change(output_root, relative, change)
    digest, file_count = _runtime_digest(output_root)
    print(f"runtime_tree_sha256={digest}")
    print(f"runtime_file_count={file_count}")
    if digest != arguments.expected_digest or file_count != arguments.expected_file_count:
        raise RuntimeError(
            "recovered runtime does not match the historical freeze: "
            f"expected {arguments.expected_digest}/{arguments.expected_file_count}, "
            f"got {digest}/{file_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
