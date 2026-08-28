from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from gameforge.harness.base import StrictModel


class EventKind(StrEnum):
    RUN_STARTED = "run_started"
    MODEL_RESPONSE_RECEIVED = "model_response_received"
    PROGRAM_CELL_STARTED = "program_cell_started"
    PROGRAM_CELL_COMPLETED = "program_cell_completed"
    PROGRAM_CELL_FAILED = "program_cell_failed"
    CAPABILITY_REQUESTED = "capability_requested"
    CAPABILITY_STARTED = "capability_started"
    CAPABILITY_COMPLETED = "capability_completed"
    CAPABILITY_FAILED = "capability_failed"
    CAPABILITY_UNCERTAIN = "capability_uncertain"
    PROJECT_MUTATION_COMMITTED = "project_mutation_committed"
    CANDIDATE_GENERATION_ADVANCED = "candidate_generation_advanced"
    ENGINE_SYNC_STARTED = "engine_sync_started"
    ENGINE_SYNC_COMPLETED = "engine_sync_completed"
    ENGINE_SYNC_FAILED = "engine_sync_failed"
    BUILD_STARTED = "build_started"
    BUILD_COMPLETED = "build_completed"
    BUILD_FAILED = "build_failed"
    PLAY_SESSION_STARTED = "play_session_started"
    PLAY_SESSION_STOPPED = "play_session_stopped"
    PLAY_SESSION_CRASHED = "play_session_crashed"
    INPUT_REPLAY_STARTED = "input_replay_started"
    INPUT_REPLAY_COMPLETED = "input_replay_completed"
    OBSERVATION_CAPTURED = "observation_captured"
    GATE_STARTED = "gate_started"
    GATE_COMPLETED = "gate_completed"
    ARTIFACT_RECORDED = "artifact_recorded"
    FINISH_PROPOSED = "finish_proposed"
    FINISH_REJECTED = "finish_rejected"
    FINISH_ACCEPTED = "finish_accepted"
    EVALUATION_STARTED = "evaluation_started"
    EVALUATION_COMPLETED = "evaluation_completed"
    RUN_COMPLETED = "run_completed"
    RUN_BLOCKED = "run_blocked"
    SKILL_PROPOSED = "skill_proposed"
    SKILL_PROMOTED = "skill_promoted"
    SKILL_REJECTED = "skill_rejected"
    CHILD_TASK_STARTED = "child_task_started"
    CHILD_TASK_COMPLETED = "child_task_completed"


class RunEvent(StrictModel):
    version: int = 1
    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    kind: EventKind
    occurred_at: datetime
    call_id: str | None = None
    subject_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class EventStoreError(RuntimeError):
    pass


class EventStore:
    def append(
        self,
        kind: EventKind,
        *,
        payload: dict[str, object] | None = None,
        call_id: str | None = None,
        subject_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RunEvent:
        raise NotImplementedError

    def events(self) -> tuple[RunEvent, ...]:
        raise NotImplementedError


class InMemoryEventStore(EventStore):
    def __init__(self, run_id: str, events: Iterable[RunEvent] = ()) -> None:
        self.run_id = run_id
        self._events = list(events)
        self._lock = threading.RLock()
        _validate_existing_events(run_id, self._events)

    def append(
        self,
        kind: EventKind,
        *,
        payload: dict[str, object] | None = None,
        call_id: str | None = None,
        subject_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RunEvent:
        with self._lock:
            event = _new_event(
                run_id=self.run_id,
                sequence=len(self._events) + 1,
                kind=kind,
                payload=payload or {},
                call_id=call_id,
                subject_id=subject_id,
                occurred_at=occurred_at,
            )
            self._events.append(event)
            return event

    def events(self) -> tuple[RunEvent, ...]:
        with self._lock:
            return tuple(self._events)


class JsonlEventStore(InMemoryEventStore):
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        existing: list[RunEvent] = []
        if path.exists():
            if not path.is_file() or path.is_symlink():
                raise EventStoreError(f"event path is not a regular file: {path}")
            lines = path.read_text(encoding="utf-8").splitlines()
            for line_number, raw_line in enumerate(lines, 1):
                try:
                    existing.append(RunEvent.model_validate_json(raw_line))
                except ValueError as error:
                    raise EventStoreError(
                        f"invalid event JSONL at line {line_number}: {path}"
                    ) from error
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(run_id, existing)

    def append(
        self,
        kind: EventKind,
        *,
        payload: dict[str, object] | None = None,
        call_id: str | None = None,
        subject_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RunEvent:
        with self._lock:
            event = super().append(
                kind,
                payload=payload,
                call_id=call_id,
                subject_id=subject_id,
                occurred_at=occurred_at,
            )
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(event.model_dump_json() + "\n")
                    stream.flush()
            except OSError as error:
                self._events.pop()
                raise EventStoreError(f"failed to append event: {self.path}") from error
            return event


def _new_event(
    *,
    run_id: str,
    sequence: int,
    kind: EventKind,
    payload: dict[str, object],
    call_id: str | None,
    subject_id: str | None,
    occurred_at: datetime | None,
) -> RunEvent:
    try:
        json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise EventStoreError("event payload must be JSON serializable") from error
    return RunEvent(
        event_id=f"{run_id}:{sequence:08d}",
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        occurred_at=occurred_at or datetime.now(UTC),
        call_id=call_id,
        subject_id=subject_id,
        payload=payload,
    )


def _validate_existing_events(run_id: str, events: list[RunEvent]) -> None:
    for expected, event in enumerate(events, 1):
        if event.run_id != run_id:
            raise EventStoreError("event store contains a different run id")
        if event.sequence != expected or event.event_id != f"{run_id}:{expected:08d}":
            raise EventStoreError("event store sequence is not contiguous")
