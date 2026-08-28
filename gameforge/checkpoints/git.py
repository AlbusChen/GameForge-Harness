from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitCheckpoint:
    commit: str
    dirty: bool


def inspect_checkpoint(root: Path) -> GitCheckpoint:
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return GitCheckpoint(
        commit=commit.stdout.strip() if commit.returncode == 0 else "UNBORN",
        dirty=bool(status.stdout.strip()),
    )
