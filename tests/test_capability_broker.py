from __future__ import annotations

import threading
from pathlib import Path

import pytest

from gameforge.harness.capabilities import (
    CapabilityBroker,
    CapabilityCallError,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPhase,
    CapabilityRegistry,
    ConcurrencyMode,
    Idempotency,
)
from gameforge.harness.errors import InfrastructureFailure
from gameforge.harness.events import EventKind, InMemoryEventStore, JsonlEventStore
from gameforge.harness.policy_engine import (
    CapabilityPolicyConfig,
    CapabilityPolicyViolation,
    ScopedCapabilityPolicy,
)
from gameforge.harness.project_scope import ProjectAccessScope


def _descriptor(
    name: str,
    *,
    write: bool = False,
    idempotency: Idempotency = Idempotency.PURE,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        version="1",
        purpose="test capability",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        effects=(CapabilityEffect.PROJECT_WRITE if write else CapabilityEffect.PROJECT_READ,),
        phase=CapabilityPhase.AUTHOR if write else CapabilityPhase.OBSERVE,
        required_permissions=("project:write" if write else "project:read",),
        idempotency=idempotency,
        concurrency=(ConcurrencyMode.SERIAL_PROJECT if write else ConcurrencyMode.PARALLEL_READ),
    )


def test_broker_enforces_schema_scope_events_and_idempotency() -> None:
    calls = 0

    def handler(arguments: object) -> object:
        nonlocal calls
        calls += 1
        return {"ok": True}

    registry = CapabilityRegistry()
    registry.register(_descriptor("write", write=True, idempotency=Idempotency.IDEMPOTENT), handler)
    events = InMemoryEventStore("run")
    broker = CapabilityBroker(
        run_id="run",
        registry=registry,
        events=events,
        policy=ScopedCapabilityPolicy(
            CapabilityPolicyConfig(
                allowed_permissions=("project:write",), editable_paths=("scene.tscn",)
            )
        ),
    )

    first = broker.invoke("write", {"path": "scene.tscn"}, idempotency_key="same")
    second = broker.invoke("write", {"path": "scene.tscn"}, idempotency_key="same")

    assert first.cached is False
    assert second.cached is True
    assert calls == 1
    assert [event.kind for event in events.events()].count(EventKind.CAPABILITY_REQUESTED) == 2
    with pytest.raises(CapabilityPolicyViolation, match="editable scope"):
        broker.invoke("write", {"path": "other.tscn"})
    with pytest.raises(CapabilityCallError, match="unknown keys"):
        broker.invoke("write", {"path": "scene.tscn", "extra": True})


def test_non_idempotent_timeout_is_recorded_as_uncertain() -> None:
    descriptor = _descriptor("write", write=True, idempotency=Idempotency.NON_IDEMPOTENT)
    registry = CapabilityRegistry()
    registry.register(descriptor, lambda arguments: (_ for _ in ()).throw(TimeoutError("late")))
    events = InMemoryEventStore("run")
    broker = CapabilityBroker(
        run_id="run",
        registry=registry,
        events=events,
        policy=ScopedCapabilityPolicy(
            CapabilityPolicyConfig(
                allowed_permissions=("project:write",), editable_paths=("scene.tscn",)
            )
        ),
    )

    with pytest.raises(CapabilityCallError) as caught:
        broker.invoke("write", {"path": "scene.tscn"})

    assert caught.value.uncertain
    assert events.events()[-1].kind is EventKind.CAPABILITY_UNCERTAIN


def test_broker_rolls_back_write_when_postcondition_rejects_mutation() -> None:
    registry = CapabilityRegistry()
    project = {"scene.tscn": "before"}

    def write(arguments: object) -> object:
        del arguments
        project["scene.tscn"] = "broken"
        return {"ok": True}

    def reject(
        descriptor: CapabilityDescriptor,
        arguments: object,
        output: object,
    ) -> None:
        del descriptor, arguments, output
        raise CapabilityCallError("broken reference")

    def begin(descriptor: CapabilityDescriptor, arguments: object) -> object:
        del descriptor, arguments
        return dict(project)

    def rollback(
        descriptor: CapabilityDescriptor,
        arguments: object,
        transaction: object,
        error: Exception,
    ) -> None:
        del descriptor, arguments, error
        assert isinstance(transaction, dict)
        project.clear()
        project.update(transaction)

    registry.register(_descriptor("write", write=True), write)
    broker = CapabilityBroker(
        run_id="run",
        registry=registry,
        events=InMemoryEventStore("run"),
        completion_hook=reject,
        begin_hook=begin,
        rollback_hook=rollback,
    )

    with pytest.raises(CapabilityCallError, match="broken reference"):
        broker.invoke("write", {"path": "scene.tscn"})

    assert project == {"scene.tscn": "before"}


def test_broker_commits_write_transaction_after_completion_hook() -> None:
    registry = CapabilityRegistry()
    project = {"scene.tscn": "before"}
    transactions: list[dict[str, str]] = []
    committed: list[dict[str, str]] = []

    def write(arguments: object) -> object:
        del arguments
        project["scene.tscn"] = "after"
        return {"ok": True}

    def begin(descriptor: CapabilityDescriptor, arguments: object) -> object:
        del descriptor, arguments
        transaction = dict(project)
        transactions.append(transaction)
        return transaction

    def commit(
        descriptor: CapabilityDescriptor,
        arguments: object,
        transaction: object,
        output: object,
    ) -> None:
        del descriptor, arguments, output
        assert isinstance(transaction, dict)
        committed.append(transaction)

    registry.register(_descriptor("write", write=True), write)
    broker = CapabilityBroker(
        run_id="run",
        registry=registry,
        events=InMemoryEventStore("run"),
        begin_hook=begin,
        commit_hook=commit,
    )

    broker.invoke("write", {"path": "scene.tscn"})

    assert project == {"scene.tscn": "after"}
    assert committed == transactions


def test_broker_preserves_infrastructure_failure_for_host_quarantine() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _descriptor("read"),
        lambda arguments: (_ for _ in ()).throw(InfrastructureFailure("engine crashed")),
    )
    events = InMemoryEventStore("run")
    broker = CapabilityBroker(run_id="run", registry=registry, events=events)

    with pytest.raises(InfrastructureFailure, match="engine crashed"):
        broker.invoke("read", {"path": "scene.tscn"})

    assert events.events()[-1].kind is EventKind.CAPABILITY_FAILED
    assert events.events()[-1].payload["type"] == "InfrastructureFailure"


def test_write_policy_scopes_mutation_targets_but_allows_safe_resource_references() -> None:
    descriptor = _descriptor("mutate_resource_objects", write=True)
    policy = ScopedCapabilityPolicy(
        CapabilityPolicyConfig(
            allowed_permissions=("project:write",),
            editable_paths=("scenes/main.tscn",),
        )
    )
    arguments = {
        "path": "scenes/main.tscn",
        "operations": [
            {
                "kind": "append_section",
                "attributes": {
                    "path": "res://assets/public-texture.png",
                    "type": "Texture2D",
                },
            }
        ],
    }

    policy.authorize(descriptor, arguments)

    arguments["operations"][0]["attributes"]["path"] = "res://../secret.png"
    with pytest.raises(CapabilityPolicyViolation, match="unsafe"):
        policy.authorize(descriptor, arguments)


def test_write_policy_checks_every_batch_file_target() -> None:
    descriptor = _descriptor("write_text_files", write=True)
    policy = ScopedCapabilityPolicy(
        CapabilityPolicyConfig(
            allowed_permissions=("project:write",),
            editable_paths=("one.gd",),
        )
    )

    with pytest.raises(CapabilityPolicyViolation, match="editable scope"):
        policy.authorize(
            descriptor,
            {"files": [{"path": "one.gd"}, {"path": "outside.gd"}]},
        )


def test_write_policy_uses_audited_runtime_scope_expansion() -> None:
    descriptor = _descriptor("write_text_file", write=True)
    scope = ProjectAccessScope(
        ("scene.tscn",),
        allow_text_scope_expansion=True,
    )
    policy = ScopedCapabilityPolicy(
        CapabilityPolicyConfig(
            allowed_permissions=("project:write",),
            editable_paths=("scene.tscn",),
        ),
        project_scope=scope,
    )

    with pytest.raises(CapabilityPolicyViolation, match="editable scope"):
        policy.authorize(descriptor, {"path": "scripts/new.gd"})
    scope.declare_output_manifest(
        ("scripts/new.gd",), reason="task explicitly requires a helper script"
    )

    policy.authorize(descriptor, {"path": "scripts/new.gd"})


def test_jsonl_event_store_resumes_contiguous_sequence(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = JsonlEventStore(path, "run")
    first = store.append(EventKind.RUN_STARTED, payload={"ok": True})
    resumed = JsonlEventStore(path, "run")
    second = resumed.append(EventKind.RUN_COMPLETED)

    assert first.event_id == "run:00000001"
    assert second.event_id == "run:00000002"


def test_broker_validates_nested_tagged_unions_and_local_schema_refs() -> None:
    registry = CapabilityRegistry()
    descriptor = CapabilityDescriptor(
        name="mutate",
        version="1",
        purpose="validate a structured mutation",
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "oneOf": [
                        {"$ref": "#/$defs/set"},
                        {"$ref": "#/$defs/append"},
                    ]
                }
            },
            "required": ["operation"],
            "additionalProperties": False,
            "$defs": {
                "set": {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "set"},
                        "path": {"type": "string"},
                        "value": {"type": ["string", "number", "null"]},
                    },
                    "required": ["kind", "path", "value"],
                    "additionalProperties": False,
                },
                "append": {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "append"},
                        "path": {"type": "string"},
                    },
                    "required": ["kind", "path"],
                    "additionalProperties": False,
                },
            },
        },
        output_schema={"type": "object"},
        effects=(CapabilityEffect.PROJECT_READ,),
        phase=CapabilityPhase.OBSERVE,
        concurrency=ConcurrencyMode.PARALLEL_READ,
    )
    registry.register(descriptor, lambda arguments: {"ok": True})
    broker = CapabilityBroker(
        run_id="run",
        registry=registry,
        events=InMemoryEventStore("run"),
    )

    result = broker.invoke(
        "mutate",
        {"operation": {"kind": "set", "path": "node/x", "value": None}},
    )

    assert result.output == {"ok": True}
    with pytest.raises(CapabilityCallError, match="exactly one"):
        broker.invoke("mutate", {"operation": {"kind": "remove", "path": "node/x"}})


def test_binary_capability_requires_allowed_provenance_and_records_denial() -> None:
    registry = CapabilityRegistry()
    registry.register(
        CapabilityDescriptor(
            name="write_binary",
            version="1",
            purpose="write a reviewed binary asset",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            effects=(CapabilityEffect.BINARY_ASSET_WRITE,),
            phase=CapabilityPhase.AUTHOR,
            required_permissions=("project:write",),
            concurrency=ConcurrencyMode.SERIAL_PROJECT,
        ),
        lambda arguments: {"ok": True},
    )
    events = InMemoryEventStore("run")
    broker = CapabilityBroker(
        run_id="run",
        registry=registry,
        events=events,
        policy=ScopedCapabilityPolicy(
            CapabilityPolicyConfig(
                allowed_permissions=("project:write",),
                editable_paths=("icon.png",),
                allow_binary_mutation=True,
                allowed_license_ids=("CC0-1.0",),
            )
        ),
    )

    with pytest.raises(CapabilityPolicyViolation, match="requires provenance"):
        broker.invoke("write_binary", {"path": "icon.png"})
    assert events.events()[-1].kind is EventKind.CAPABILITY_FAILED
    with pytest.raises(CapabilityPolicyViolation, match="license is not allowed"):
        broker.invoke(
            "write_binary",
            {
                "path": "icon.png",
                "provenance": {
                    "source": "https://example.invalid/icon.png",
                    "license_id": "proprietary",
                    "original_sha256": "a" * 64,
                },
            },
        )
    accepted = broker.invoke(
        "write_binary",
        {
            "path": "icon.png",
            "provenance": {
                "source": "https://example.invalid/icon.png",
                "license_id": "CC0-1.0",
                "original_sha256": "a" * 64,
            },
        },
    )
    assert accepted.output == {"ok": True}


def test_parallel_read_handlers_run_concurrently_with_deterministic_event_order() -> None:
    registry = CapabilityRegistry()
    barrier = threading.Barrier(2, timeout=2)

    def read(arguments: object) -> object:
        barrier.wait()
        return {"ok": True}

    registry.register(_descriptor("read"), read)
    events = InMemoryEventStore("run")
    broker = CapabilityBroker(run_id="run", registry=registry, events=events)

    results = broker.invoke_parallel_read(
        (
            ("read", {"path": "a"}, None),
            ("read", {"path": "b"}, None),
        )
    )

    assert [result.call_id for result in results] == [
        "run:call:00000001",
        "run:call:00000002",
    ]
    assert [event.kind for event in events.events()] == [
        EventKind.CAPABILITY_REQUESTED,
        EventKind.CAPABILITY_REQUESTED,
        EventKind.CAPABILITY_STARTED,
        EventKind.CAPABILITY_STARTED,
        EventKind.CAPABILITY_COMPLETED,
        EventKind.CAPABILITY_COMPLETED,
    ]
    assert [result.event_id for result in results] == ["run:00000005", "run:00000006"]
