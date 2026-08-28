from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass

from gameforge.harness.capabilities import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPhase,
    CapabilityRegistry,
    ConcurrencyMode,
    CostClass,
    Determinism,
    Idempotency,
    RetryPolicy,
)
from gameforge.harness.interfaces import EngineAdapter
from gameforge.plugins.base import PluginManifest
from gameforge.tooling.contracts import TOOL_CONTRACTS, ToolContract

_AUTHOR_TOOLS = frozenset(
    {
        "apply_code_patch",
        "create_game_object",
        "create_script",
        "mutate_resource_objects",
        "mutate_engine_resource_properties",
        "replace_text",
        "set_component_property",
        "write_text_file",
        "write_text_files",
    }
)
_SYNC_TOOLS = frozenset({"health_check", "import_project", "wait_for_compilation"})
_BUILD_TOOLS = frozenset({"build_player", "launch_build_smoke_test"})
_PLAYTEST_TOOLS = frozenset(
    {
        "enter_play_mode",
        "exit_play_mode",
        "read_game_state",
        "run_edit_mode_tests",
        "run_play_mode_tests",
        "send_test_command",
    }
)
_CAPTURE_TOOLS = frozenset({"capture_scene_evidence", "capture_screenshot", "review_visual_change"})


@dataclass(frozen=True)
class LegacyEnginePlugin:
    engine: EngineAdapter

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(
            _descriptor(TOOL_CONTRACTS[name])
            for name in sorted(self.engine.available_tools())
            if name in TOOL_CONTRACTS
        )

    def manifest(self) -> PluginManifest:
        descriptors = self.descriptors()
        try:
            source = inspect.getsource(type(self.engine))
        except (OSError, TypeError):
            source = f"{type(self.engine).__module__}.{type(self.engine).__qualname__}"
        implementation = json.dumps(
            {
                "engine": self.engine.name,
                "version": self.engine.version,
                "capabilities": [descriptor.name for descriptor in descriptors],
                "implementation_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return PluginManifest(
            name=self.engine.name,
            version=self.engine.version,
            implementation_digest=hashlib.sha256(implementation).hexdigest(),
            capability_digests={item.name: item.digest() for item in descriptors},
        )

    def register(self, registry: CapabilityRegistry) -> None:
        for descriptor in self.descriptors():
            name = descriptor.name
            registry.register(
                descriptor,
                lambda arguments, capability=name: self.engine.invoke(capability, arguments),
            )


def _descriptor(contract: ToolContract) -> CapabilityDescriptor:
    phase = _phase(contract.name)
    effects = _effects(contract, phase)
    return CapabilityDescriptor(
        name=contract.name,
        version="legacy-v1",
        purpose=contract.purpose,
        input_schema={
            "type": "object",
            "properties": {
                name: _legacy_schema(contract.name, name, description)
                for name, description in contract.input_schema.items()
            },
            "required": sorted(
                name
                for name, description in contract.input_schema.items()
                if not description.strip().lower().startswith("optional ")
            ),
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effects=effects,
        phase=phase,
        required_permissions=_permissions(effects, contract.approval_required),
        invalidates=(
            ("import", "build", "playtest", "capture", "gate")
            if phase is CapabilityPhase.AUTHOR
            else ()
        ),
        retry_policy=RetryPolicy(
            maximum_attempts=2 if contract.retryable else 1,
            retryable_errors=contract.error_codes if contract.retryable else (),
        ),
        timeout_seconds=contract.timeout_seconds,
        idempotency=(Idempotency.IDEMPOTENT if contract.retryable else Idempotency.NON_IDEMPOTENT),
        concurrency=(
            ConcurrencyMode.PARALLEL_READ
            if not contract.mutates_project and phase is CapabilityPhase.OBSERVE
            else (
                ConcurrencyMode.EXCLUSIVE_ENGINE
                if phase
                in {
                    CapabilityPhase.ENGINE_SYNC,
                    CapabilityPhase.BUILD,
                    CapabilityPhase.PLAYTEST,
                    CapabilityPhase.CAPTURE,
                }
                else ConcurrencyMode.SERIAL_PROJECT
            )
        ),
        cost_class=_cost_class(phase),
        determinism=(
            Determinism.BEST_EFFORT
            if phase is CapabilityPhase.CAPTURE
            else Determinism.DETERMINISTIC
        ),
        evidence_outputs=(phase.value,),
        affected_scopes=contract.affected_scope,
    )


def _phase(name: str) -> CapabilityPhase:
    if name in _AUTHOR_TOOLS:
        return CapabilityPhase.AUTHOR
    if name in _SYNC_TOOLS:
        return CapabilityPhase.ENGINE_SYNC
    if name in _BUILD_TOOLS:
        return CapabilityPhase.BUILD
    if name in _PLAYTEST_TOOLS:
        return CapabilityPhase.PLAYTEST
    if name in _CAPTURE_TOOLS:
        return CapabilityPhase.CAPTURE
    return CapabilityPhase.OBSERVE


def _effects(contract: ToolContract, phase: CapabilityPhase) -> tuple[CapabilityEffect, ...]:
    if phase is CapabilityPhase.AUTHOR:
        return (CapabilityEffect.PROJECT_WRITE,)
    if phase in {CapabilityPhase.ENGINE_SYNC, CapabilityPhase.BUILD}:
        return (CapabilityEffect.ENGINE_STATE, CapabilityEffect.ARTIFACT_WRITE)
    if phase is CapabilityPhase.PLAYTEST:
        return (CapabilityEffect.RUNTIME_STATE,)
    if phase is CapabilityPhase.CAPTURE:
        return (CapabilityEffect.ARTIFACT_WRITE,)
    return (CapabilityEffect.PROJECT_READ,)


def _permissions(effects: tuple[CapabilityEffect, ...], approval_required: bool) -> tuple[str, ...]:
    permissions: list[str] = []
    if CapabilityEffect.PROJECT_READ in effects:
        permissions.append("project:read")
    if CapabilityEffect.PROJECT_WRITE in effects:
        permissions.append("project:write")
    if CapabilityEffect.ENGINE_STATE in effects:
        permissions.append("engine:execute")
    if CapabilityEffect.RUNTIME_STATE in effects:
        permissions.append("runtime:control")
    if CapabilityEffect.ARTIFACT_WRITE in effects:
        permissions.append("artifact:write")
    if approval_required:
        permissions.append("approval:high-impact")
    return tuple(permissions)


def _cost_class(phase: CapabilityPhase) -> CostClass:
    return {
        CapabilityPhase.ENGINE_SYNC: CostClass.ENGINE_SYNC,
        CapabilityPhase.BUILD: CostClass.BUILD,
        CapabilityPhase.PLAYTEST: CostClass.RUNTIME,
        CapabilityPhase.CAPTURE: CostClass.JUDGE,
    }.get(phase, CostClass.CHEAP)


def _legacy_schema(
    contract_name: str,
    parameter_name: str,
    description: str,
) -> dict[str, object]:
    if (
        contract_name
        in {
            "declare_output_manifest",
            "inspect_resources",
            "read_text_files",
        }
        and parameter_name == "paths"
    ):
        return {
            "type": "array",
            "description": description,
            "minItems": 1,
            "maxItems": 64 if contract_name == "declare_output_manifest" else 16,
            "items": {"type": "string"},
        }
    if contract_name == "mutate_resource_objects" and parameter_name == "operations":
        primitive = {"type": ["string", "integer", "number", "boolean"]}
        attribute_values = {
            "type": "object",
            "additionalProperties": primitive,
        }
        property_values = {
            "type": "object",
            "additionalProperties": primitive,
        }
        selector = {
            "type": "object",
            "properties": {
                "section": {"type": "string"},
                "attributes": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["section", "attributes"],
            "additionalProperties": False,
        }
        return {
            "type": "array",
            "description": description,
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "set_properties"},
                            "selector": selector,
                            "properties": {**property_values, "minProperties": 1},
                        },
                        "required": ["kind", "selector", "properties"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "set_attributes"},
                            "selector": selector,
                            "attributes": {**attribute_values, "minProperties": 1},
                        },
                        "required": ["kind", "selector", "attributes"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "append_section"},
                            "section": {"type": "string"},
                            "attributes": attribute_values,
                            "properties": property_values,
                        },
                        "required": ["kind", "section", "attributes", "properties"],
                        "additionalProperties": False,
                    },
                ]
            },
        }
    if contract_name == "mutate_engine_resource_properties" and parameter_name == "operations":
        return {
            "type": "array",
            "description": description,
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "property": {"type": "string", "minLength": 1},
                    "value": {},
                },
                "required": ["property", "value"],
                "additionalProperties": False,
            },
        }
    lowered = description.lower()
    normalized = lowered.removeprefix("optional ").strip()
    if normalized.startswith("integer") or "integer" in normalized:
        return {"type": "integer", "description": description}
    if normalized.startswith("boolean"):
        return {"type": "boolean", "description": description}
    if normalized.startswith("array") or normalized.endswith("[]"):
        return {"type": "array", "description": description}
    if "typed value" in normalized or "tagged union" in normalized:
        return {"description": description}
    return {"type": "string", "description": description}
