#!/usr/bin/env python3
"""Create a self-verifying, non-overwriting Harness runtime version snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

RUNTIME_PATHS = (
    ".github",
    "gameforge",
    "benchmarks",
    "config",
    "docs",
    "scripts",
    "specs",
    "tests",
    "unity",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "pyproject.toml",
    "uv.lock",
)

IGNORED_RUNTIME_NAMES = {
    ".DS_Store",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "Build",
    "Builds",
    "Library",
    "Logs",
    "Obj",
    "Temp",
    "UserSettings",
    "__pycache__",
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--versions-root", type=Path, default=Path("runs/internal-versions"))
    parser.add_argument("--expected-runtime-digest", required=True)
    parser.add_argument("--expected-runtime-file-count", type=int, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--parent-version")
    parser.add_argument("--provenance", required=True)
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_files(root: Path) -> list[Path]:
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
            and not IGNORED_RUNTIME_NAMES.intersection(path.relative_to(root).parts)
            and path.suffix not in {".pyc", ".pyo"}
        )
    return sorted(files)


def _runtime_digest(root: Path) -> tuple[str, int]:
    files = _runtime_files(root)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(f"{relative}\0{_sha256(path)}\n".encode())
    return digest.hexdigest(), len(files)


def _copy_runtime(source_root: Path, snapshot_root: Path) -> None:
    source = snapshot_root / "source"
    source.mkdir(parents=True)
    for name in RUNTIME_PATHS:
        origin = source_root / name
        if not origin.exists():
            continue
        destination = source / name
        if origin.is_file():
            shutil.copy2(origin, destination)
        else:
            shutil.copytree(
                origin,
                destination,
                ignore=shutil.ignore_patterns(
                    *sorted(IGNORED_RUNTIME_NAMES), "*.pyc", "*.pyo"
                ),
            )


def main() -> int:
    arguments = _arguments()
    source_root = arguments.source_root.resolve(strict=True)
    versions_root = arguments.versions_root.resolve()
    snapshot_root = versions_root / arguments.version
    if snapshot_root.exists():
        raise RuntimeError(f"refusing to overwrite frozen version: {snapshot_root}")
    digest, file_count = _runtime_digest(source_root)
    if (
        digest != arguments.expected_runtime_digest
        or file_count != arguments.expected_runtime_file_count
    ):
        raise RuntimeError(
            "source runtime does not match requested freeze: "
            f"expected {arguments.expected_runtime_digest}/"
            f"{arguments.expected_runtime_file_count}, got {digest}/{file_count}"
        )
    for artifact in arguments.artifact:
        artifact.resolve(strict=True)

    snapshot_root.mkdir(parents=True)
    _copy_runtime(source_root, snapshot_root)
    copied_digest, copied_count = _runtime_digest(snapshot_root / "source")
    if copied_digest != digest or copied_count != file_count:
        raise RuntimeError("copied runtime failed exact digest verification")

    evidence_root = snapshot_root / "evidence"
    evidence_root.mkdir()
    artifacts: list[dict[str, object]] = []
    used_names: set[str] = set()
    for artifact in arguments.artifact:
        resolved = artifact.resolve(strict=True)
        if resolved.name in used_names:
            raise RuntimeError(f"duplicate artifact basename: {resolved.name}")
        used_names.add(resolved.name)
        destination = evidence_root / resolved.name
        shutil.copy2(resolved, destination)
        artifacts.append(
            {
                "name": resolved.name,
                "sha256": _sha256(destination),
                "source_path": str(resolved),
            }
        )

    source_checksums = [
        f"{_sha256(path)}  {path.relative_to(snapshot_root).as_posix()}"
        for path in _runtime_files(snapshot_root / "source")
    ]
    (snapshot_root / "SOURCE_SHA256SUMS").write_text(
        "\n".join(source_checksums) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "schema_version": 1,
        "version": arguments.version,
        "label": arguments.label,
        "parent_version": arguments.parent_version,
        "created_at": datetime.now(UTC).isoformat(),
        "immutability": "refuse-overwrite snapshot; publish corrections as a new version",
        "source_scope": [
            f"{name}/**" if (source_root / name).is_dir() else name
            for name in RUNTIME_PATHS
            if (source_root / name).exists()
        ],
        "source_completeness": (
            "Complete installable Harness runtime source and locked dependency definition"
        ),
        "runtime_tree_sha256": digest,
        "runtime_file_count": file_count,
        "provenance": arguments.provenance,
        "notes": arguments.note,
        "artifacts": artifacts,
    }
    (snapshot_root / "VERSION.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    top_level = [
        snapshot_root / "SOURCE_SHA256SUMS",
        snapshot_root / "VERSION.json",
        *sorted(evidence_root.iterdir()),
    ]
    (snapshot_root / "SHA256SUMS").write_text(
        "\n".join(
            f"{_sha256(path)}  {path.relative_to(snapshot_root).as_posix()}" for path in top_level
        )
        + "\n",
        encoding="utf-8",
    )
    print(snapshot_root)
    print(f"runtime_tree_sha256={digest}")
    print(f"runtime_file_count={file_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
