from __future__ import annotations

from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from gameforge.harness.base import StrictModel


class AcceptanceDimension(StrEnum):
    STRUCTURE = "structure"
    BEHAVIOR = "behavior"
    VISUAL = "visual"
    AUDIO = "audio"
    PERFORMANCE = "performance"
    BUILD = "build"
    PRESERVATION = "preservation"
    ASSET_COMPLIANCE = "asset_compliance"


class RequirementEnforcement(StrEnum):
    REQUIRED = "required"
    SCORED = "scored"
    ADVISORY = "advisory"


class CaptureKind(StrEnum):
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    AUDIO = "audio"
    PERFORMANCE = "performance"


class TimedInputAction(StrictModel):
    action: str = Field(min_length=1)
    value: str | int | float | bool | None = None
    frame: int | None = Field(default=None, ge=0)
    seconds: float | None = Field(default=None, ge=0)
    duration_frames: int = Field(default=1, ge=1, le=100_000)

    @model_validator(mode="after")
    def exactly_one_time_coordinate(self) -> TimedInputAction:
        if (self.frame is None) == (self.seconds is None):
            raise ValueError("input action requires exactly one of frame or seconds")
        return self


class RuntimeProbe(StrictModel):
    name: str = Field(min_length=1)
    at_frame: int | None = Field(default=None, ge=0)
    selector: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class CaptureRequest(StrictModel):
    kind: CaptureKind
    at_frame: int | None = Field(default=None, ge=0)
    frame_count: int = Field(default=1, ge=1, le=36_000)
    camera: str = Field(default="default", min_length=1)
    width: int = Field(default=1280, ge=1, le=16_384)
    height: int = Field(default=720, ge=1, le=16_384)


class PlaytestScenario(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    entry_point: str = Field(min_length=1)
    initial_state: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    seed: int = 0
    fixed_timestep_seconds: float | None = Field(default=None, gt=0, le=1)
    warmup_frames: int = Field(default=0, ge=0, le=100_000)
    actions: tuple[TimedInputAction, ...] = ()
    public_probes: tuple[RuntimeProbe, ...] = ()
    captures: tuple[CaptureRequest, ...] = ()
    repetitions: int = Field(default=1, ge=1, le=100)
    tolerance: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> PlaytestScenario:
        probe_names = tuple(probe.name for probe in self.public_probes)
        if len(probe_names) != len(set(probe_names)):
            raise ValueError("playtest probe names must be unique")
        return self


class AcceptanceRequirement(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    dimension: AcceptanceDimension
    enforcement: RequirementEnforcement
    evaluator_ref: str = Field(min_length=1)
    evaluator_arguments: dict[str, object] = Field(default_factory=dict)
    scenario_refs: tuple[str, ...] = ()
    threshold: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    model_visible: bool = True


class EvidencePolicy(StrictModel):
    maximum_observation_bytes: int = Field(default=64 * 1024, ge=1, le=16 * 1024 * 1024)
    require_environment_digest: bool = True
    require_input_trace_for_runtime_evidence: bool = True
    conservative_invalidation: bool = True


class AssetPolicy(StrictModel):
    allow_binary_mutation: bool = False
    require_provenance: bool = True
    allowed_license_ids: tuple[str, ...] = ()
    generated_roots: tuple[str, ...] = ()
    allow_text_scope_expansion: bool = False
    allow_native_workspace_agent: bool = False
    maximum_writable_paths: int = Field(default=64, ge=1, le=10_000)


class GameTaskSpec(StrictModel):
    engine_profile: str = Field(min_length=1)
    entry_points: tuple[str, ...] = Field(min_length=1)
    target_platforms: tuple[str, ...] = Field(min_length=1)
    requirements: tuple[AcceptanceRequirement, ...] = Field(min_length=1)
    playtest_scenarios: tuple[PlaytestScenario, ...] = ()
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)
    asset_policy: AssetPolicy = Field(default_factory=AssetPolicy)

    @model_validator(mode="after")
    def references_are_valid(self) -> GameTaskSpec:
        requirement_ids = tuple(requirement.id for requirement in self.requirements)
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("acceptance requirement ids must be unique")
        scenario_ids = tuple(scenario.id for scenario in self.playtest_scenarios)
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("playtest scenario ids must be unique")
        known_scenarios = set(scenario_ids)
        unknown = sorted(
            {
                scenario
                for requirement in self.requirements
                for scenario in requirement.scenario_refs
                if scenario not in known_scenarios
            }
        )
        if unknown:
            raise ValueError(f"requirements reference unknown playtest scenarios: {unknown}")
        return self

    def public_projection(self) -> GameTaskSpec:
        return self.model_copy(
            update={
                "requirements": tuple(
                    requirement for requirement in self.requirements if requirement.model_visible
                )
            }
        )

    def required_requirements(self) -> tuple[AcceptanceRequirement, ...]:
        return tuple(
            requirement
            for requirement in self.requirements
            if requirement.enforcement is RequirementEnforcement.REQUIRED
        )


class EngineEnvironment(StrictModel):
    engine_version: str = Field(min_length=1)
    target_platform: str = Field(min_length=1)
    render_pipeline: str = "default"
    graphics_backend: str = "default"
    quality_preset: str = "default"
    physics_profile: str = "default"
    input_profile: str = "default"
    locale: str = "C"
    timezone: str = "UTC"


class RuntimeProtocol(StrEnum):
    LEGACY_TOOL_V1 = "legacy-tool-v1"
    PROGRAMMABLE_V1 = "programmable-v1"
    MINIMAL_OPEN_V1 = "minimal-open-v1"


class PluginPin(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    capabilities: tuple[str, ...] = ()


def default_game_task_spec(
    *,
    engine_profile: str,
    target_platform: str,
    entry_point: str = "default",
) -> GameTaskSpec:
    return GameTaskSpec(
        engine_profile=engine_profile,
        entry_points=(entry_point,),
        target_platforms=(target_platform,),
        requirements=(
            AcceptanceRequirement(
                id="preservation",
                dimension=AcceptanceDimension.PRESERVATION,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="preservation",
            ),
        ),
    )


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
RuntimeMode: TypeAlias = Literal["authoring", "playtest"]
