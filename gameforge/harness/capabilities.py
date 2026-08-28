from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from gameforge.harness.base import StrictModel
from gameforge.harness.errors import InfrastructureFailure
from gameforge.harness.events import EventKind, EventStore


class CapabilityPhase(StrEnum):
    OBSERVE = "observe"
    AUTHOR = "author"
    ENGINE_SYNC = "engine_sync"
    BUILD = "build"
    PLAYTEST = "playtest"
    CAPTURE = "capture"


class CapabilityEffect(StrEnum):
    NONE = "none"
    PROJECT_READ = "project_read"
    PROJECT_WRITE = "project_write"
    ENGINE_STATE = "engine_state"
    RUNTIME_STATE = "runtime_state"
    ARTIFACT_WRITE = "artifact_write"
    BINARY_ASSET_WRITE = "binary_asset_write"


class Idempotency(StrEnum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class ConcurrencyMode(StrEnum):
    PARALLEL_READ = "parallel_read"
    SERIAL_PROJECT = "serial_project"
    EXCLUSIVE_ENGINE = "exclusive_engine"


class CostClass(StrEnum):
    CHEAP = "cheap"
    ENGINE_SYNC = "engine_sync"
    BUILD = "build"
    RUNTIME = "runtime"
    JUDGE = "judge"


class Determinism(StrEnum):
    DETERMINISTIC = "deterministic"
    SEEDED = "seeded"
    BEST_EFFORT = "best_effort"


class RetryPolicy(StrictModel):
    maximum_attempts: int = Field(default=1, ge=1, le=10)
    retryable_errors: tuple[str, ...] = ()


class StatePredicate(StrictModel):
    key: str = Field(min_length=1)
    allowed_values: tuple[str, ...] = Field(min_length=1)


class CapabilityDescriptor(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
    version: str = Field(min_length=1)
    implementation_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    purpose: str = Field(min_length=1)
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    effects: tuple[CapabilityEffect, ...] = (CapabilityEffect.NONE,)
    phase: CapabilityPhase
    required_permissions: tuple[str, ...] = ()
    prerequisites: tuple[StatePredicate, ...] = ()
    invalidates: tuple[str, ...] = ()
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    idempotency: Idempotency = Idempotency.NON_IDEMPOTENT
    concurrency: ConcurrencyMode = ConcurrencyMode.SERIAL_PROJECT
    cost_class: CostClass = CostClass.CHEAP
    determinism: Determinism = Determinism.DETERMINISTIC
    evidence_outputs: tuple[str, ...] = ()
    affected_scopes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def effects_are_consistent(self) -> CapabilityDescriptor:
        if len(self.effects) != len(set(self.effects)):
            raise ValueError("capability effects must be unique")
        if CapabilityEffect.NONE in self.effects and len(self.effects) > 1:
            raise ValueError("none cannot be combined with other capability effects")
        if self.concurrency is ConcurrencyMode.PARALLEL_READ and any(
            effect
            in {
                CapabilityEffect.PROJECT_WRITE,
                CapabilityEffect.ENGINE_STATE,
                CapabilityEffect.RUNTIME_STATE,
                CapabilityEffect.BINARY_ASSET_WRITE,
            }
            for effect in self.effects
        ):
            raise ValueError("parallel_read capability cannot declare mutating effects")
        return self

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class CapabilityPolicy(Protocol):
    def authorize(
        self, descriptor: CapabilityDescriptor, arguments: Mapping[str, object]
    ) -> None: ...


class PrerequisiteProvider(Protocol):
    def value(self, key: str) -> str | None: ...


CapabilityHandler = Callable[[Mapping[str, object]], object]
CapabilityBeginHook = Callable[[CapabilityDescriptor, Mapping[str, object]], object]
CapabilityCommitHook = Callable[[CapabilityDescriptor, Mapping[str, object], object, object], None]
CapabilityRollbackHook = Callable[
    [CapabilityDescriptor, Mapping[str, object], object, Exception], None
]
_CACHE_MISS = object()


@dataclass(frozen=True)
class CapabilityRegistration:
    descriptor: CapabilityDescriptor
    handler: CapabilityHandler


class CapabilityRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, CapabilityRegistration] = {}
        self._frozen = False

    def register(self, descriptor: CapabilityDescriptor, handler: CapabilityHandler) -> None:
        if self._frozen:
            raise RuntimeError("capability registry is frozen")
        if descriptor.name in self._registrations:
            raise ValueError(f"capability is already registered: {descriptor.name}")
        self._registrations[descriptor.name] = CapabilityRegistration(descriptor, handler)

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def get(self, name: str) -> CapabilityRegistration:
        try:
            return self._registrations[name]
        except KeyError as error:
            raise CapabilityCallError(f"capability is not registered: {name}") from error

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._registrations[name].descriptor for name in sorted(self._registrations))

    def digests(self) -> dict[str, str]:
        return {descriptor.name: descriptor.digest() for descriptor in self.descriptors()}


class CapabilityCallError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class UncertainCapabilityError(RuntimeError):
    pass


class AllowAllCapabilityPolicy:
    def authorize(self, descriptor: CapabilityDescriptor, arguments: Mapping[str, object]) -> None:
        del descriptor, arguments


class EmptyPrerequisiteProvider:
    def value(self, key: str) -> str | None:
        del key
        return None


@dataclass(frozen=True)
class CapabilityResult:
    call_id: str
    output: object
    cached: bool
    event_id: str


class CapabilityBroker:
    def __init__(
        self,
        *,
        run_id: str,
        registry: CapabilityRegistry,
        events: EventStore,
        policy: CapabilityPolicy | None = None,
        prerequisites: PrerequisiteProvider | None = None,
        invalidator: Callable[[tuple[str, ...]], None] | None = None,
        completion_hook: Callable[[CapabilityDescriptor, Mapping[str, object], object], None]
        | None = None,
        begin_hook: CapabilityBeginHook | None = None,
        commit_hook: CapabilityCommitHook | None = None,
        rollback_hook: CapabilityRollbackHook | None = None,
        maximum_calls: int = 1_000,
        maximum_observation_bytes: int = 64 * 1024,
    ) -> None:
        if maximum_calls < 0:
            raise ValueError("maximum capability calls cannot be negative")
        if maximum_observation_bytes < 1:
            raise ValueError("maximum observation bytes must be positive")
        if not registry.frozen:
            registry.freeze()
        self.run_id = run_id
        self.registry = registry
        self.events = events
        self.policy = policy or AllowAllCapabilityPolicy()
        self.prerequisites = prerequisites or EmptyPrerequisiteProvider()
        self.invalidator = invalidator
        self.completion_hook = completion_hook
        self.begin_hook = begin_hook
        self.commit_hook = commit_hook
        self.rollback_hook = rollback_hook
        self.maximum_calls = maximum_calls
        self.maximum_observation_bytes = maximum_observation_bytes
        self.call_count = 0
        self._cache: dict[str, object] = {}
        self._state_lock = threading.RLock()
        self._project_lock = threading.RLock()
        self._engine_lock = threading.RLock()

    def descriptions(self) -> tuple[dict[str, object], ...]:
        return tuple(
            descriptor.model_dump(mode="json") for descriptor in self.registry.descriptors()
        )

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> CapabilityResult:
        registration = self.registry.get(name)
        descriptor = registration.descriptor
        with self._state_lock:
            if self.call_count >= self.maximum_calls:
                raise CapabilityCallError("capability-call budget exceeded")
            self.call_count += 1
            call_id = f"{self.run_id}:call:{self.call_count:08d}"
        requested = self.events.append(
            EventKind.CAPABILITY_REQUESTED,
            call_id=call_id,
            payload={
                "name": name,
                "arguments": dict(arguments),
                "descriptor_digest": descriptor.digest(),
                "idempotency": descriptor.idempotency.value,
                "idempotency_key": idempotency_key,
            },
        )
        try:
            self.policy.authorize(descriptor, arguments)
            _validate_json_schema(dict(arguments), descriptor.input_schema, path="arguments")
            self._check_prerequisites(descriptor)
        except Exception as error:
            self.events.append(
                EventKind.CAPABILITY_FAILED,
                call_id=call_id,
                payload={
                    "name": name,
                    "error": str(error),
                    "type": type(error).__name__,
                    "request_event_id": requested.event_id,
                },
            )
            raise
        cache_key = self._cache_key(descriptor, arguments, idempotency_key)
        with self._state_lock:
            if cache_key is not None and cache_key in self._cache:
                completed = self.events.append(
                    EventKind.CAPABILITY_COMPLETED,
                    call_id=call_id,
                    payload={"name": name, "cached": True},
                )
                return CapabilityResult(call_id, self._cache[cache_key], True, completed.event_id)
        self.events.append(
            EventKind.CAPABILITY_STARTED,
            call_id=call_id,
            payload={"name": name, "request_event_id": requested.event_id},
        )
        execution_lock = self._execution_lock(descriptor.concurrency)
        transaction: object = _CACHE_MISS
        try:
            with execution_lock:
                if self.begin_hook is not None:
                    transaction = self.begin_hook(descriptor, arguments)
                output = registration.handler(arguments)
            encoded = json.dumps(output, sort_keys=True).encode("utf-8")
            if len(encoded) > self.maximum_observation_bytes:
                raise CapabilityCallError("capability output exceeds observation byte limit")
            _validate_json_schema(output, descriptor.output_schema, path="output")
            if descriptor.invalidates and self.invalidator is not None:
                self.invalidator(descriptor.invalidates)
            if self.completion_hook is not None:
                self.completion_hook(descriptor, arguments, output)
            if transaction is not _CACHE_MISS and self.commit_hook is not None:
                self.commit_hook(descriptor, arguments, transaction, output)
        except InfrastructureFailure as error:
            self._rollback_transaction(descriptor, arguments, transaction, error, call_id)
            self.events.append(
                EventKind.CAPABILITY_FAILED,
                call_id=call_id,
                payload={"name": name, "error": str(error), "type": type(error).__name__},
            )
            raise
        except UncertainCapabilityError as error:
            self._rollback_transaction(descriptor, arguments, transaction, error, call_id)
            uncertain = self.events.append(
                EventKind.CAPABILITY_UNCERTAIN,
                call_id=call_id,
                payload={"name": name, "error": str(error)},
            )
            raise CapabilityCallError(
                f"capability outcome is uncertain ({uncertain.event_id}): {error}",
                uncertain=True,
            ) from error
        except CapabilityCallError as error:
            self._rollback_transaction(descriptor, arguments, transaction, error, call_id)
            self.events.append(
                EventKind.CAPABILITY_FAILED,
                call_id=call_id,
                payload={"name": name, "error": str(error)},
            )
            raise
        except Exception as error:
            self._rollback_transaction(descriptor, arguments, transaction, error, call_id)
            kind = (
                EventKind.CAPABILITY_UNCERTAIN
                if descriptor.idempotency is Idempotency.NON_IDEMPOTENT
                and isinstance(error, (OSError, TimeoutError))
                else EventKind.CAPABILITY_FAILED
            )
            event = self.events.append(
                kind,
                call_id=call_id,
                payload={"name": name, "error": str(error), "type": type(error).__name__},
            )
            raise CapabilityCallError(
                f"capability failed ({event.event_id}): {error}",
                uncertain=kind is EventKind.CAPABILITY_UNCERTAIN,
            ) from error
        with self._state_lock:
            if cache_key is not None:
                self._cache[cache_key] = output
        completed = self.events.append(
            EventKind.CAPABILITY_COMPLETED,
            call_id=call_id,
            payload={"name": name, "cached": False, "bytes": len(encoded)},
        )
        return CapabilityResult(call_id, output, False, completed.event_id)

    def _rollback_transaction(
        self,
        descriptor: CapabilityDescriptor,
        arguments: Mapping[str, object],
        transaction: object,
        error: Exception,
        call_id: str,
    ) -> None:
        if transaction is _CACHE_MISS or self.rollback_hook is None:
            return
        try:
            self.rollback_hook(descriptor, arguments, transaction, error)
        except Exception as rollback_error:
            self.events.append(
                EventKind.CAPABILITY_FAILED,
                call_id=call_id,
                payload={
                    "name": descriptor.name,
                    "error": f"transaction rollback failed: {rollback_error}",
                    "type": type(rollback_error).__name__,
                },
            )
            raise InfrastructureFailure(
                f"capability transaction rollback failed: {rollback_error}"
            ) from rollback_error

    def invoke_parallel_read(
        self,
        calls: tuple[tuple[str, Mapping[str, object], str | None], ...],
    ) -> tuple[CapabilityResult, ...]:
        if not calls:
            return ()
        prepared: list[
            tuple[
                CapabilityRegistration,
                dict[str, object],
                str | None,
                str,
                str,
                object | None,
            ]
        ] = []
        registrations: list[CapabilityRegistration] = []
        normalized_calls: list[tuple[dict[str, object], str | None]] = []
        for name, arguments, idempotency_key in calls:
            registration = self.registry.get(name)
            descriptor = registration.descriptor
            try:
                if descriptor.concurrency is not ConcurrencyMode.PARALLEL_READ:
                    raise CapabilityCallError(
                        f"parallel batch only accepts parallel_read capabilities: {name}"
                    )
                self.policy.authorize(descriptor, arguments)
                normalized = dict(arguments)
                _validate_json_schema(normalized, descriptor.input_schema, path="arguments")
                self._check_prerequisites(descriptor)
            except Exception as error:
                with self._state_lock:
                    if self.call_count >= self.maximum_calls:
                        raise CapabilityCallError("capability-call budget exceeded") from error
                    self.call_count += 1
                    call_id = f"{self.run_id}:call:{self.call_count:08d}"
                requested = self.events.append(
                    EventKind.CAPABILITY_REQUESTED,
                    call_id=call_id,
                    payload={
                        "name": name,
                        "arguments": dict(arguments),
                        "descriptor_digest": descriptor.digest(),
                        "idempotency": descriptor.idempotency.value,
                        "idempotency_key": idempotency_key,
                    },
                )
                self.events.append(
                    EventKind.CAPABILITY_FAILED,
                    call_id=call_id,
                    payload={
                        "name": name,
                        "error": str(error),
                        "type": type(error).__name__,
                        "request_event_id": requested.event_id,
                    },
                )
                raise
            registrations.append(registration)
            normalized_calls.append((normalized, idempotency_key))
        with self._state_lock:
            if self.call_count + len(calls) > self.maximum_calls:
                raise CapabilityCallError("capability-call budget exceeded")
            first_sequence = self.call_count + 1
            self.call_count += len(calls)
        for index, (registration, normalized) in enumerate(
            zip(registrations, normalized_calls, strict=True)
        ):
            arguments, idempotency_key = normalized
            descriptor = registration.descriptor
            call_id = f"{self.run_id}:call:{first_sequence + index:08d}"
            requested = self.events.append(
                EventKind.CAPABILITY_REQUESTED,
                call_id=call_id,
                payload={
                    "name": descriptor.name,
                    "arguments": arguments,
                    "descriptor_digest": descriptor.digest(),
                    "idempotency": descriptor.idempotency.value,
                    "idempotency_key": idempotency_key,
                },
            )
            cache_key = self._cache_key(descriptor, arguments, idempotency_key)
            with self._state_lock:
                cached_output = (
                    self._cache.get(cache_key, _CACHE_MISS)
                    if cache_key is not None
                    else _CACHE_MISS
                )
            prepared.append(
                (
                    registration,
                    arguments,
                    cache_key,
                    call_id,
                    requested.event_id,
                    cached_output,
                )
            )
        uncached = tuple(item for item in prepared if item[5] is _CACHE_MISS)
        for registration, _, _, call_id, requested_id, _ in uncached:
            self.events.append(
                EventKind.CAPABILITY_STARTED,
                call_id=call_id,
                payload={
                    "name": registration.descriptor.name,
                    "request_event_id": requested_id,
                },
            )

        def execute(item):
            registration, arguments, _, _, _, _ = item
            try:
                return registration.handler(arguments), None
            except Exception as error:  # The main thread records terminal events in call order.
                return None, error

        executions: dict[str, tuple[object | None, Exception | None]] = {}
        if uncached:
            with ThreadPoolExecutor(max_workers=min(32, len(uncached))) as executor:
                outputs = tuple(executor.map(execute, uncached))
            executions = {item[3]: output for item, output in zip(uncached, outputs, strict=True)}

        results: list[CapabilityResult] = []
        first_error: CapabilityCallError | None = None
        first_infrastructure_failure: InfrastructureFailure | None = None
        for registration, arguments, cache_key, call_id, _, cached_output in prepared:
            descriptor = registration.descriptor
            if cached_output is not _CACHE_MISS:
                completed = self.events.append(
                    EventKind.CAPABILITY_COMPLETED,
                    call_id=call_id,
                    payload={"name": descriptor.name, "cached": True},
                )
                results.append(CapabilityResult(call_id, cached_output, True, completed.event_id))
                continue
            output, error = executions[call_id]
            if error is not None:
                kind = (
                    EventKind.CAPABILITY_UNCERTAIN
                    if descriptor.idempotency is Idempotency.NON_IDEMPOTENT
                    and isinstance(error, (OSError, TimeoutError, UncertainCapabilityError))
                    else EventKind.CAPABILITY_FAILED
                )
                terminal = self.events.append(
                    kind,
                    call_id=call_id,
                    payload={
                        "name": descriptor.name,
                        "error": str(error),
                        "type": type(error).__name__,
                    },
                )
                if first_infrastructure_failure is None and isinstance(
                    error, InfrastructureFailure
                ):
                    first_infrastructure_failure = error
                    continue
                if first_error is None:
                    first_error = CapabilityCallError(
                        f"capability failed ({terminal.event_id}): {error}",
                        uncertain=kind is EventKind.CAPABILITY_UNCERTAIN,
                    )
                continue
            try:
                encoded = json.dumps(output, sort_keys=True).encode("utf-8")
                if len(encoded) > self.maximum_observation_bytes:
                    raise CapabilityCallError("capability output exceeds observation byte limit")
                _validate_json_schema(output, descriptor.output_schema, path="output")
                if self.completion_hook is not None:
                    self.completion_hook(descriptor, arguments, output)
            except Exception as validation_error:
                terminal = self.events.append(
                    EventKind.CAPABILITY_FAILED,
                    call_id=call_id,
                    payload={
                        "name": descriptor.name,
                        "error": str(validation_error),
                        "type": type(validation_error).__name__,
                    },
                )
                if first_error is None:
                    first_error = CapabilityCallError(
                        f"capability failed ({terminal.event_id}): {validation_error}"
                    )
                continue
            with self._state_lock:
                if cache_key is not None:
                    self._cache[cache_key] = output
            completed = self.events.append(
                EventKind.CAPABILITY_COMPLETED,
                call_id=call_id,
                payload={"name": descriptor.name, "cached": False, "bytes": len(encoded)},
            )
            results.append(CapabilityResult(call_id, output, False, completed.event_id))
        if first_infrastructure_failure is not None:
            raise first_infrastructure_failure
        if first_error is not None:
            raise first_error
        return tuple(results)

    def _execution_lock(self, concurrency: ConcurrencyMode):
        if concurrency is ConcurrencyMode.PARALLEL_READ:
            return nullcontext()
        if concurrency is ConcurrencyMode.EXCLUSIVE_ENGINE:
            return self._engine_lock
        return self._project_lock

    def _check_prerequisites(self, descriptor: CapabilityDescriptor) -> None:
        for predicate in descriptor.prerequisites:
            actual = self.prerequisites.value(predicate.key)
            if actual not in predicate.allowed_values:
                raise CapabilityCallError(
                    f"capability prerequisite failed: {predicate.key}={actual!r}, "
                    f"allowed={list(predicate.allowed_values)!r}"
                )

    def _cache_key(
        self,
        descriptor: CapabilityDescriptor,
        arguments: Mapping[str, object],
        idempotency_key: str | None,
    ) -> str | None:
        if idempotency_key is None or descriptor.idempotency is Idempotency.NON_IDEMPOTENT:
            return None
        payload = json.dumps(
            {
                "descriptor": descriptor.digest(),
                "arguments": dict(arguments),
                "key": idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _validate_json_schema(
    value: object,
    schema: Mapping[str, object],
    *,
    path: str,
    _root: Mapping[str, object] | None = None,
) -> None:
    root = schema if _root is None else _root
    reference = schema.get("$ref")
    if isinstance(reference, str):
        resolved = _resolve_local_reference(root, reference)
        _validate_json_schema(value, resolved, path=path, _root=root)
        return
    for keyword in ("allOf", "anyOf", "oneOf"):
        alternatives = schema.get(keyword)
        if not isinstance(alternatives, list):
            continue
        matches = 0
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                raise CapabilityCallError(f"{path} contains an invalid {keyword} schema")
            try:
                _validate_json_schema(value, alternative, path=path, _root=root)
            except CapabilityCallError:
                continue
            matches += 1
        if keyword == "allOf" and matches != len(alternatives):
            raise CapabilityCallError(f"{path} does not satisfy every allOf schema")
        if keyword == "anyOf" and matches == 0:
            raise CapabilityCallError(f"{path} does not satisfy any allowed schema")
        if keyword == "oneOf" and matches != 1:
            raise CapabilityCallError(f"{path} must satisfy exactly one allowed schema")

    if "const" in schema and value != schema["const"]:
        raise CapabilityCallError(f"{path} must equal {schema['const']!r}")
    expected = schema.get("type")
    if isinstance(expected, list):
        accepted = tuple(item for item in expected if isinstance(item, str))
        if not any(_matches_type(value, item) for item in accepted):
            raise CapabilityCallError(f"{path} does not match schema types {accepted}")
    elif isinstance(expected, str) and not _matches_type(value, expected):
        raise CapabilityCallError(f"{path} must be {expected}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise CapabilityCallError(f"{path} must be one of {enum!r}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        property_map = properties if isinstance(properties, dict) else {}
        required = schema.get("required", [])
        required_names = set(required) if isinstance(required, list) else set()
        missing = sorted(name for name in required_names if name not in value)
        if missing:
            raise CapabilityCallError(f"{path} is missing required keys: {missing}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(property_map))
            if extras:
                raise CapabilityCallError(f"{path} contains unknown keys: {extras}")
        minimum = schema.get("minProperties")
        maximum = schema.get("maxProperties")
        if isinstance(minimum, int) and len(value) < minimum:
            raise CapabilityCallError(f"{path} contains fewer than {minimum} properties")
        if isinstance(maximum, int) and len(value) > maximum:
            raise CapabilityCallError(f"{path} contains more than {maximum} properties")
        for name, child in value.items():
            child_schema = property_map.get(name)
            if isinstance(child_schema, dict):
                _validate_json_schema(child, child_schema, path=f"{path}.{name}", _root=root)
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate_json_schema(
                    child,
                    schema["additionalProperties"],  # type: ignore[arg-type]
                    path=f"{path}.{name}",
                    _root=root,
                )

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise CapabilityCallError(f"{path} contains fewer than {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise CapabilityCallError(f"{path} contains more than {maximum} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema(
                    item,
                    item_schema,
                    path=f"{path}[{index}]",
                    _root=root,
                )

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(minimum, int) and len(value) < minimum:
            raise CapabilityCallError(f"{path} is shorter than {minimum}")
        if isinstance(maximum, int) and len(value) > maximum:
            raise CapabilityCallError(f"{path} is longer than {maximum}")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            raise CapabilityCallError(f"{path} does not match required pattern")

    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int | float) and value < minimum:
            raise CapabilityCallError(f"{path} is below minimum {minimum}")
        if isinstance(maximum, int | float) and value > maximum:
            raise CapabilityCallError(f"{path} is above maximum {maximum}")


def _matches_type(value: object, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _resolve_local_reference(root: Mapping[str, object], reference: str) -> Mapping[str, object]:
    if not reference.startswith("#/"):
        raise CapabilityCallError(f"only local JSON Schema references are supported: {reference}")
    current: object = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise CapabilityCallError(f"unresolved JSON Schema reference: {reference}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise CapabilityCallError(f"JSON Schema reference is not an object: {reference}")
    return current
