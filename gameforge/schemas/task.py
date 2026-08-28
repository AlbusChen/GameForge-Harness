from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskKind(StrEnum):
    INSPECT = "inspect"
    IMPLEMENT = "implement"
    COMPILE = "compile"
    STRUCTURE_TEST = "structure_test"
    PLAY_TEST = "play_test"
    BUILD = "build"
    REPORT = "report"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: TaskKind
    description: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    acceptance_ids: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING


class TaskDAG(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> TaskDAG:
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task ids must be unique")
        known: set[str] = set()
        for task in self.tasks:
            missing = set(task.dependencies) - known
            if missing:
                raise ValueError(
                    f"task {task.id} has unknown or forward dependencies: {sorted(missing)}"
                )
            known.add(task.id)
        return self
