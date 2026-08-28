from __future__ import annotations

import shutil
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gameforge.harness.contracts import NormalizedTask, TaskSourceKind
from gameforge.orchestrator.repair_loop import apply_text_replacement
from gameforge.schemas.diagnostic import TextReplacement


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailureClass(StrEnum):
    COMPILATION = "compilation"
    STRUCTURE = "structure"
    GAMEPLAY = "gameplay"
    BUILD_LAUNCH = "build_launch"


class FaultFixture(StrictModel):
    path: str = Field(pattern=r"^[^/].*\.cs$")
    expected: str = Field(min_length=1)
    replacement: str
    regenerate_scene: bool = False


class HeterogeneousTask(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    title: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    failure_class: FailureClass
    required_gates: tuple[str, ...] = Field(min_length=1)
    editable_paths: tuple[str, ...] = Field(min_length=1)
    fixture: FaultFixture

    @model_validator(mode="after")
    def fixture_is_inside_editable_scope(self) -> HeterogeneousTask:
        if self.fixture.path not in self.editable_paths:
            raise ValueError("fixture path must be included in editable_paths")
        if len(self.editable_paths) != len(set(self.editable_paths)):
            raise ValueError("editable_paths must be unique")
        return self

    def to_normalized_task(
        self,
        project_path: Path,
        *,
        benchmark_name: str,
        benchmark_version: str,
    ) -> NormalizedTask:
        public_payload = {
            "id": self.id,
            "title": self.title,
            "instruction": self.instruction,
            "failure_class": self.failure_class.value,
            "required_gates": self.required_gates,
            "editable_paths": self.editable_paths,
            "regenerate_scene": self.fixture.regenerate_scene,
        }
        return NormalizedTask(
            id=self.id,
            source=TaskSourceKind.BENCHMARK,
            instruction=self.instruction,
            project_path=project_path.resolve(),
            acceptance=tuple(f"required gate: {gate}" for gate in self.required_gates),
            editable_paths=self.editable_paths,
            benchmark_name=benchmark_name,
            benchmark_version=benchmark_version,
            source_sha256=NormalizedTask.source_hash(public_payload),
            metadata={
                "failure_class": self.failure_class.value,
                "title": self.title,
                "regenerate_scene": self.fixture.regenerate_scene,
            },
        )


class TaskCatalog(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    engine: str = Field(pattern=r"^unity$")
    tasks: tuple[HeterogeneousTask, ...] = Field(min_length=20)

    @model_validator(mode="after")
    def task_set_is_balanced_and_unique(self) -> TaskCatalog:
        identifiers = [task.id for task in self.tasks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("benchmark task IDs must be unique")
        counts = Counter(task.failure_class for task in self.tasks)
        missing = [category.value for category in FailureClass if counts[category] < 5]
        if missing:
            raise ValueError(
                "benchmark requires at least five tasks in every failure class: "
                + ", ".join(missing)
            )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> TaskCatalog:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read benchmark catalog: {path}") from error
        except yaml.YAMLError as error:
            raise ValueError(f"invalid benchmark catalog YAML: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("benchmark catalog root must be a mapping")
        return cls.model_validate(payload)

    def task(self, task_id: str) -> HeterogeneousTask:
        matches = [task for task in self.tasks if task.id == task_id]
        if not matches:
            raise KeyError(f"unknown benchmark task: {task_id}")
        return matches[0]

    def validate_template(self, project_path: Path) -> None:
        project = project_path.resolve(strict=True)
        for task in self.tasks:
            path = project / task.fixture.path
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"fixture source is missing or unsafe: {task.fixture.path}")
            content = path.read_text(encoding="utf-8")
            count = content.count(task.fixture.expected)
            if count != 1:
                raise ValueError(
                    f"fixture anchor for {task.id} must occur exactly once, found {count}"
                )
            mutated = content.replace(task.fixture.expected, task.fixture.replacement, 1)
            replacement_count = mutated.count(task.fixture.replacement)
            if replacement_count != 1:
                raise ValueError(
                    f"repair anchor for {task.id} must occur exactly once after injection, "
                    f"found {replacement_count}"
                )


@dataclass(frozen=True)
class HeterogeneousBenchmarkAdapter:
    catalog: TaskCatalog
    template_project: Path

    @property
    def name(self) -> str:
        return self.catalog.name

    @property
    def version(self) -> str:
        return self.catalog.version

    def list_tasks(self) -> tuple[str, ...]:
        return tuple(task.id for task in self.catalog.tasks)

    def load_task(self, task_id: str, project_path: Path) -> NormalizedTask:
        return self.catalog.task(task_id).to_normalized_task(
            project_path,
            benchmark_name=self.name,
            benchmark_version=self.version,
        )

    def prepare_workspace(self, task: NormalizedTask, destination: Path) -> Path:
        fixture_task = self.catalog.task(task.id)
        if task.benchmark_name != self.name or task.benchmark_version != self.version:
            raise ValueError("normalized task does not belong to this benchmark adapter")
        if destination.exists():
            raise FileExistsError(destination)
        source = self.template_project.resolve(strict=True)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*sorted(IGNORED_WORKSPACE_DIRECTORIES)),
        )
        proposal = TextReplacement(
            path=fixture_task.fixture.path,
            expected=fixture_task.fixture.expected,
            replacement=fixture_task.fixture.replacement,
            rationale="Inject the private benchmark fixture into an isolated workspace.",
            evidence_sources=("versioned_private_fixture",),
        )
        relative = Path(fixture_task.fixture.path)
        apply_text_replacement(
            destination,
            proposal,
            allowed_roots=(relative.parent,),
        )
        if fixture_task.fixture.regenerate_scene:
            scene = destination / "Assets" / "Scenes" / "Arena.unity"
            scene.unlink(missing_ok=True)
            scene.with_suffix(".unity.meta").unlink(missing_ok=True)
        return destination


IGNORED_WORKSPACE_DIRECTORIES = frozenset(
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
