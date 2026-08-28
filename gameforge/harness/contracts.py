from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from gameforge.harness.base import StrictModel
from gameforge.harness.game_tasks import (
    EngineEnvironment,
    GameTaskSpec,
    PluginPin,
    RuntimeProtocol,
)


class TaskSourceKind(StrEnum):
    BENCHMARK = "benchmark"
    CREATE = "create"
    CHANGE = "change"


class EngineName(StrEnum):
    UNITY = "unity"
    GODOT = "godot"


class HarnessProfile(StrEnum):
    OFFICIAL_COMPATIBLE = "official_compatible"
    AUGMENTED = "augmented"
    PROJECT = "project"


class RunStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class NormalizedTask(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    source: TaskSourceKind
    instruction: str = Field(min_length=1, max_length=100_000)
    project_path: Path
    acceptance: tuple[str, ...] = ()
    editable_paths: tuple[str, ...] = ()
    benchmark_name: str | None = None
    benchmark_version: str | None = None
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def benchmark_fields_match_source(self) -> NormalizedTask:
        if self.source is TaskSourceKind.BENCHMARK:
            if not self.benchmark_name or not self.benchmark_version:
                raise ValueError("benchmark tasks require benchmark_name and benchmark_version")
        elif self.benchmark_name or self.benchmark_version:
            raise ValueError("non-benchmark tasks cannot set benchmark metadata")
        return self

    @classmethod
    def source_hash(cls, payload: object) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ModelRunSpec(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str | None = None
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunBudgets(StrictModel):
    wall_seconds: int = Field(default=1800, ge=1, le=86_400)
    max_turns: int = Field(default=40, ge=1, le=500)
    max_tool_calls: int = Field(default=80, ge=0, le=1000)
    max_repairs: int = Field(default=3, ge=0, le=20)
    max_cost_usd: float = Field(default=10.0, ge=0, le=100_000)


class EvaluationSpec(StrictModel):
    required_gates: tuple[str, ...] = (
        "specification",
        "preservation",
        "compilation",
        "structure",
        "gameplay",
        "build",
        "smoke",
    )
    official_evaluator: bool = False
    evaluator_version: str = "native-v1"
    seed: int = 0

    @model_validator(mode="after")
    def gates_are_unique(self) -> EvaluationSpec:
        if len(self.required_gates) != len(set(self.required_gates)):
            raise ValueError("required gates must be unique")
        return self


class RunSpec(StrictModel):
    version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    engine: EngineName
    engine_version: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    profile: HarnessProfile
    task: NormalizedTask
    model: ModelRunSpec
    budgets: RunBudgets = Field(default_factory=RunBudgets)
    evaluation: EvaluationSpec = Field(default_factory=EvaluationSpec)

    @model_validator(mode="after")
    def official_mode_requires_benchmark(self) -> RunSpec:
        if (
            self.profile is HarnessProfile.OFFICIAL_COMPATIBLE
            and self.task.source is not TaskSourceKind.BENCHMARK
        ):
            raise ValueError("official-compatible runs require a benchmark task")
        return self


class RunSpecV2(RunSpec):
    version: Literal[2] = 2
    game_task: GameTaskSpec
    engine_environment: EngineEnvironment
    runtime_protocol: RuntimeProtocol = RuntimeProtocol.PROGRAMMABLE_V1
    plugins: tuple[PluginPin, ...] = ()
    capability_digests: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def game_environment_is_pinned(self) -> RunSpecV2:
        if self.engine_environment.engine_version != self.engine_version:
            raise ValueError("engine environment version must match RunSpec engine_version")
        if self.engine_environment.target_platform not in self.game_task.target_platforms:
            raise ValueError("engine target platform must be declared by GameTaskSpec")
        plugin_names = tuple(plugin.name for plugin in self.plugins)
        if len(plugin_names) != len(set(plugin_names)):
            raise ValueError("plugin pins must have unique names")
        invalid = sorted(
            name
            for name, digest in self.capability_digests.items()
            if not name
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        )
        if invalid:
            raise ValueError(f"invalid capability digests: {invalid}")
        return self


class ArtifactRecord(StrictModel):
    path: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)


class GateOutcome(StrictModel):
    gate: str = Field(min_length=1)
    status: GateStatus
    detail: str = ""
    artifacts: tuple[str, ...] = ()


class RunResult(StrictModel):
    version: Literal[1] = 1
    run_id: str
    status: RunStatus
    started_at: datetime
    ended_at: datetime
    model: ModelRunSpec
    gates: tuple[GateOutcome, ...]
    artifacts: tuple[ArtifactRecord, ...] = ()
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    failure_category: str | None = None
    failure_detail: str | None = None

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> RunResult:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot be before started_at")
        return self


class RunResultV2(RunResult):
    version: Literal[2] = 2
    program_cells: int = Field(default=0, ge=0)
    model_turns: int = Field(default=0, ge=0)
    prompt_bytes: int = Field(default=0, ge=0)
    capability_calls_per_turn: float = Field(default=0.0, ge=0)
    evidence_subjects: int = Field(default=0, ge=0)
    required_evidence_ids: tuple[str, ...] = ()
    required_evidence_coverage: float = Field(default=0.0, ge=0, le=1)
    lineage_complete: bool = False
    dimension_status: dict[str, str] = Field(default_factory=dict)
    policy_violations: int = Field(default=0, ge=0)
