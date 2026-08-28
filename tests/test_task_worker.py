from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gameforge.harness.artifacts import ContentAddressedArtifactStore
from gameforge.harness.events import EventKind, InMemoryEventStore
from gameforge.harness.evidence import EvidenceGraph
from gameforge.harness.program_state import ControlSnapshot, ProgramState, ProgramStateStore
from gameforge.harness.task_service import (
    ChildTaskStatus,
    TaskBudget,
    TaskService,
    TaskServiceError,
    TaskUsage,
)
from gameforge.harness.worker import (
    ResumableRunWorker,
    RunRecoveryPolicy,
    WorkerStatus,
)


def test_child_tasks_default_read_only_and_mutation_leases_cannot_overlap(
    tmp_path: Path,
) -> None:
    events = InMemoryEventStore("run")
    service = TaskService(
        parent_run_id="run",
        parent_budget=TaskBudget(wall_seconds=100, capability_calls=20, cost_usd=5),
        events=events,
    )
    read_only = service.start(
        capability_set=("inspect",),
        budget=TaskBudget(wall_seconds=10, capability_calls=3, cost_usd=1),
        result_schema={"type": "object"},
    )
    mutating = service.start(
        capability_set=("write",),
        editable_scope=("scenes/Main.tscn",),
        budget=TaskBudget(wall_seconds=10, capability_calls=3, cost_usd=1),
        result_schema={"type": "object"},
    )

    assert read_only.editable_scope == ()
    assert mutating.lease_id is not None
    with pytest.raises(TaskServiceError, match="overlaps"):
        service.start(
            capability_set=("write",),
            editable_scope=("scenes",),
            budget=TaskBudget(wall_seconds=10, capability_calls=3, cost_usd=1),
            result_schema={"type": "object"},
        )
    service.record_usage(
        read_only.child_run_id,
        TaskUsage(wall_seconds=2, capability_calls=1, cost_usd=0.2),
    )
    artifact = ContentAddressedArtifactStore(tmp_path / "artifacts").put_json(
        {"result": "ok"}, kind="child_result"
    )
    completed = service.complete(mutating.child_run_id, artifact)
    assert completed.status is ChildTaskStatus.COMPLETED
    assert not service.leases.active()


def test_recovery_policy_never_replays_uncertain_non_idempotent_calls() -> None:
    events = InMemoryEventStore("run")
    events.append(
        EventKind.CAPABILITY_REQUESTED,
        call_id="run:call:1",
        payload={"idempotency": "non_idempotent", "idempotency_key": None},
    )
    events.append(EventKind.CAPABILITY_STARTED, call_id="run:call:1")

    report = RunRecoveryPolicy().inspect(events.events())

    assert not report.safe_to_resume
    assert report.uncertain_call_ids == ("run:call:1",)


def test_worker_snapshot_resume_checks_digests_and_supports_detach(tmp_path: Path) -> None:
    events = InMemoryEventStore("run")
    worker = ResumableRunWorker(
        run_id="run",
        events=events,
        snapshots=ProgramStateStore(tmp_path / "snapshot.json"),
    )
    now = datetime.now(UTC)
    worker.acquire("owner", now=now, ttl_seconds=100)
    snapshot = ControlSnapshot(
        run_id="run",
        last_event_sequence=0,
        capability_digests={"read": "a" * 64},
        plugin_digests={"godot": "b" * 64},
        program=ProgramState(variables={"value": 1}),
        evidence=EvidenceGraph().snapshot(),
    )
    worker.checkpoint("owner", snapshot)
    loaded, report = worker.resume(
        "owner",
        capability_digests={"read": "a" * 64},
        plugin_digests={"godot": "b" * 64},
    )

    assert loaded.program.variables["value"] == 1
    assert report.safe_to_resume
    detached = worker.detach("owner", now=now)
    assert detached.status is WorkerStatus.DETACHED
