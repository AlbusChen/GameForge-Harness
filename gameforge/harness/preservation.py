from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".godot",
        "Build",
        "Builds",
        "Library",
        "Logs",
        "Temp",
        "UserSettings",
        "obj",
    }
)
IGNORED_FILE_SUFFIXES = frozenset({".import", ".uid"})


class ProjectSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: Path
    files: dict[str, str]


class PreservationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changed_paths: tuple[str, ...]
    allowed_changed_paths: tuple[str, ...]
    unrelated_changed_paths: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.unrelated_changed_paths


@dataclass(frozen=True)
class SnapshotPolicy:
    maximum_files: int = 50_000
    chunk_bytes: int = 1024 * 1024


def snapshot_project(
    project_path: Path,
    *,
    policy: SnapshotPolicy | None = None,
) -> ProjectSnapshot:
    active_policy = policy or SnapshotPolicy()
    project = project_path.resolve(strict=True)
    if not project.is_dir():
        raise ValueError(f"project path is not a directory: {project}")

    fingerprints: dict[str, str] = {}
    for directory, names, files in os.walk(project, topdown=True, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in IGNORED_DIRECTORY_NAMES and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or path.suffix.lower() in IGNORED_FILE_SUFFIXES:
                continue
            relative = path.relative_to(project).as_posix()
            fingerprints[relative] = _sha256_file(path, active_policy.chunk_bytes)
            if len(fingerprints) > active_policy.maximum_files:
                raise ValueError(
                    f"project snapshot exceeds the {active_policy.maximum_files}-file limit"
                )
    return ProjectSnapshot(project_path=project, files=fingerprints)


def compare_snapshots(
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    editable_paths: tuple[str, ...],
) -> PreservationReport:
    if before.project_path != after.project_path:
        raise ValueError("project snapshots refer to different roots")

    changed = tuple(
        sorted(
            path
            for path in set(before.files) | set(after.files)
            if Path(path).suffix.lower() not in IGNORED_FILE_SUFFIXES
            and before.files.get(path) != after.files.get(path)
        )
    )
    allowed = _allowed_paths(editable_paths)
    allowed_changed = tuple(path for path in changed if path in allowed)
    unrelated = tuple(path for path in changed if path not in allowed)
    return PreservationReport(
        changed_paths=changed,
        allowed_changed_paths=allowed_changed,
        unrelated_changed_paths=unrelated,
    )


def _allowed_paths(editable_paths: tuple[str, ...]) -> frozenset[str]:
    allowed: set[str] = set()
    for raw_path in editable_paths:
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"editable path must be project-relative: {raw_path}")
        normalized = relative.as_posix()
        allowed.add(normalized)
        allowed.add(f"{normalized}.meta")
    return frozenset(allowed)


def _sha256_file(path: Path, chunk_bytes: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()
