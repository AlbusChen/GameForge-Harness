from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from gameforge.harness.contracts import NormalizedTask, TaskSourceKind
from gameforge.harness.interfaces import BenchmarkAdapter


def _safe_task_id(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value).strip("-.")
    return normalized or fallback


@dataclass(frozen=True)
class CreateBriefTaskSource:
    brief_path: Path
    acceptance: tuple[str, ...] = ()
    editable_paths: tuple[str, ...] = ()

    def load(self, project_path: Path) -> NormalizedTask:
        try:
            instruction = self.brief_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError(f"cannot read create brief: {self.brief_path}") from error
        if not instruction:
            raise ValueError("create brief cannot be empty")
        payload = {
            "kind": TaskSourceKind.CREATE.value,
            "instruction": instruction,
            "acceptance": self.acceptance,
            "editable_paths": self.editable_paths,
        }
        return NormalizedTask(
            id=_safe_task_id(f"create-{self.brief_path.stem}", "create-task"),
            source=TaskSourceKind.CREATE,
            instruction=instruction,
            project_path=project_path.resolve(),
            acceptance=self.acceptance,
            editable_paths=self.editable_paths,
            source_sha256=NormalizedTask.source_hash(payload),
            metadata={"brief_file": self.brief_path.name},
        )


@dataclass(frozen=True)
class ChangeRequestTaskSource:
    request: str
    task_id: str = "project-change"
    acceptance: tuple[str, ...] = ()
    editable_paths: tuple[str, ...] = ()

    def load(self, project_path: Path) -> NormalizedTask:
        instruction = self.request.strip()
        if not instruction:
            raise ValueError("change request cannot be empty")
        payload = {
            "kind": TaskSourceKind.CHANGE.value,
            "instruction": instruction,
            "acceptance": self.acceptance,
            "editable_paths": self.editable_paths,
        }
        return NormalizedTask(
            id=_safe_task_id(self.task_id, "project-change"),
            source=TaskSourceKind.CHANGE,
            instruction=instruction,
            project_path=project_path.resolve(),
            acceptance=self.acceptance,
            editable_paths=self.editable_paths,
            source_sha256=NormalizedTask.source_hash(payload),
        )


@dataclass(frozen=True)
class AdapterBenchmarkTaskSource:
    adapter: BenchmarkAdapter
    task_id: str

    def load(self, project_path: Path) -> NormalizedTask:
        return self.adapter.load_task(self.task_id, project_path.resolve())
