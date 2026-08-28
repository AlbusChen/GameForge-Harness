from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from gameforge.harness.contracts import GateOutcome, NormalizedTask, RunSpec


class TaskSource(Protocol):
    def load(self, project_path: Path) -> NormalizedTask: ...


class BenchmarkAdapter(Protocol):
    name: str
    version: str

    def list_tasks(self) -> tuple[str, ...]: ...

    def load_task(self, task_id: str, project_path: Path) -> NormalizedTask: ...

    def prepare_workspace(self, task: NormalizedTask, destination: Path) -> Path: ...

    def evaluate_official(self, run_spec: RunSpec, workspace: Path) -> tuple[GateOutcome, ...]: ...


class EngineAdapter(Protocol):
    name: str
    version: str

    def available_tools(self) -> tuple[str, ...]: ...

    def automatic_validation_tool(self) -> str | None: ...

    def completion_feedback_tools(self) -> tuple[str, ...]: ...

    def invoke(self, tool_name: str, arguments: Mapping[str, object]) -> object: ...


class EvaluatorAdapter(Protocol):
    name: str
    version: str

    def evaluate(self, run_spec: RunSpec, workspace: Path) -> tuple[GateOutcome, ...]: ...
