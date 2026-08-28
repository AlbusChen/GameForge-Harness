from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.capabilities import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPhase,
    CapabilityRegistry,
    ConcurrencyMode,
    CostClass,
    Determinism,
    Idempotency,
)
from gameforge.harness.game_tasks import EngineEnvironment


class SkillCompatibility(StrictModel):
    engine_family: str = Field(min_length=1)
    engine_versions: tuple[str, ...] = ()
    render_pipelines: tuple[str, ...] = ()
    input_profiles: tuple[str, ...] = ()
    target_platforms: tuple[str, ...] = ()
    task_archetypes: tuple[str, ...] = ()

    def matches(
        self,
        *,
        engine_family: str,
        environment: EngineEnvironment,
        task_archetype: str,
    ) -> bool:
        return bool(
            self.engine_family == engine_family
            and (not self.engine_versions or environment.engine_version in self.engine_versions)
            and (not self.render_pipelines or environment.render_pipeline in self.render_pipelines)
            and (not self.input_profiles or environment.input_profile in self.input_profiles)
            and (not self.target_platforms or environment.target_platform in self.target_platforms)
            and (not self.task_archetypes or task_archetype in self.task_archetypes)
        )


class SkillManifest(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    package_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    tests_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    effects: tuple[CapabilityEffect, ...] = (CapabilityEffect.NONE,)
    phase: CapabilityPhase = CapabilityPhase.OBSERVE
    idempotency: Idempotency = Idempotency.PURE
    concurrency: ConcurrencyMode = ConcurrencyMode.PARALLEL_READ
    determinism: Determinism = Determinism.DETERMINISTIC
    compatibility: SkillCompatibility
    reviewed: bool = False

    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


SkillHandler = Callable[[Mapping[str, object]], object]


@dataclass(frozen=True)
class RegisteredSkill:
    manifest: SkillManifest
    handler: SkillHandler


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, RegisteredSkill] = {}
        self._frozen = False

    def install(self, manifest: SkillManifest, handler: SkillHandler) -> None:
        if self._frozen:
            raise RuntimeError("skill registry is frozen")
        if not manifest.reviewed:
            raise SkillError("executable skill must be reviewed")
        if manifest.name in self._skills:
            raise SkillError(f"skill is already installed: {manifest.name}")
        self._skills[manifest.name] = RegisteredSkill(manifest, handler)

    def freeze(self) -> None:
        self._frozen = True

    def manifests(self) -> tuple[SkillManifest, ...]:
        return tuple(self._skills[name].manifest for name in sorted(self._skills))

    def register_compatible_capabilities(
        self,
        registry: CapabilityRegistry,
        *,
        engine_family: str,
        environment: EngineEnvironment,
        task_archetype: str,
    ) -> tuple[str, ...]:
        names: list[str] = []
        for name in sorted(self._skills):
            registered = self._skills[name]
            manifest = registered.manifest
            if not manifest.compatibility.matches(
                engine_family=engine_family,
                environment=environment,
                task_archetype=task_archetype,
            ):
                continue
            capability_name = f"skill.{manifest.name}"
            mutates = CapabilityEffect.PROJECT_WRITE in manifest.effects
            registry.register(
                CapabilityDescriptor(
                    name=capability_name,
                    version=manifest.version,
                    implementation_digest=manifest.digest(),
                    purpose=manifest.description,
                    input_schema=manifest.input_schema,
                    output_schema=manifest.output_schema,
                    effects=manifest.effects,
                    phase=manifest.phase,
                    required_permissions=("skill:execute",),
                    invalidates=(
                        ("import", "build", "playtest", "capture", "gate") if mutates else ()
                    ),
                    idempotency=manifest.idempotency,
                    concurrency=manifest.concurrency,
                    cost_class=CostClass.CHEAP,
                    determinism=manifest.determinism,
                    evidence_outputs=("skill_result",),
                ),
                registered.handler,
            )
            names.append(capability_name)
        return tuple(names)


class SkillError(RuntimeError):
    pass
