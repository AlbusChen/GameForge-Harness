from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.capabilities import Idempotency
from gameforge.harness.events import EventKind, EventStore, RunEvent
from gameforge.harness.program_state import ControlSnapshot, ProgramStateStore


class WorkerStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DETACHED = "detached"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkerLease(StrictModel):
    owner: str = Field(min_length=1)
    expires_at: datetime

    def active(self, now: datetime) -> bool:
        return now < self.expires_at


class RecoveryReport(StrictModel):
    safe_to_resume: bool
    uncertain_call_ids: tuple[str, ...] = ()
    replayable_call_ids: tuple[str, ...] = ()
    completed_call_ids: tuple[str, ...] = ()


class WorkerState(StrictModel):
    run_id: str
    status: WorkerStatus = WorkerStatus.IDLE
    lease: WorkerLease | None = None
    cancel_requested: bool = False
    last_checkpoint_sequence: int = 0
    detail: str = ""


class WorkerError(RuntimeError):
    pass


class RunRecoveryPolicy:
    def inspect(self, events: tuple[RunEvent, ...]) -> RecoveryReport:
        requested: dict[str, RunEvent] = {}
        terminal: dict[str, EventKind] = {}
        for event in events:
            if event.call_id is None:
                continue
            if event.kind is EventKind.CAPABILITY_REQUESTED:
                requested[event.call_id] = event
            elif event.kind in {
                EventKind.CAPABILITY_COMPLETED,
                EventKind.CAPABILITY_FAILED,
                EventKind.CAPABILITY_UNCERTAIN,
            }:
                terminal[event.call_id] = event.kind
        uncertain: list[str] = []
        replayable: list[str] = []
        completed: list[str] = []
        for call_id, request in requested.items():
            outcome = terminal.get(call_id)
            if outcome is EventKind.CAPABILITY_COMPLETED:
                completed.append(call_id)
                continue
            if outcome is EventKind.CAPABILITY_FAILED:
                continue
            raw_idempotency = request.payload.get("idempotency")
            try:
                idempotency = Idempotency(str(raw_idempotency))
            except ValueError:
                idempotency = Idempotency.NON_IDEMPOTENT
            key = request.payload.get("idempotency_key")
            if (
                outcome is EventKind.CAPABILITY_UNCERTAIN
                or idempotency is Idempotency.NON_IDEMPOTENT
                or (idempotency is Idempotency.IDEMPOTENT and not key)
            ):
                uncertain.append(call_id)
            else:
                replayable.append(call_id)
        return RecoveryReport(
            safe_to_resume=not uncertain,
            uncertain_call_ids=tuple(uncertain),
            replayable_call_ids=tuple(replayable),
            completed_call_ids=tuple(completed),
        )


class ResumableRunWorker:
    def __init__(
        self,
        *,
        run_id: str,
        events: EventStore,
        snapshots: ProgramStateStore,
        recovery: RunRecoveryPolicy | None = None,
    ) -> None:
        self.events = events
        self.snapshots = snapshots
        self.recovery = recovery or RunRecoveryPolicy()
        self.state = WorkerState(run_id=run_id)
        self._lock = threading.RLock()

    def acquire(
        self,
        owner: str,
        *,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> WorkerState:
        current = now or datetime.now(UTC)
        with self._lock:
            lease = self.state.lease
            if lease is not None and lease.active(current) and lease.owner != owner:
                raise WorkerError(f"run worker is leased by {lease.owner}")
            self.state = self.state.model_copy(
                update={
                    "status": WorkerStatus.RUNNING,
                    "lease": WorkerLease(
                        owner=owner,
                        expires_at=current + timedelta(seconds=ttl_seconds),
                    ),
                }
            )
            return self.state

    def heartbeat(
        self,
        owner: str,
        *,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> WorkerState:
        current = now or datetime.now(UTC)
        with self._lock:
            self._require_owner(owner, current)
            self.state = self.state.model_copy(
                update={
                    "lease": WorkerLease(
                        owner=owner,
                        expires_at=current + timedelta(seconds=ttl_seconds),
                    )
                }
            )
            return self.state

    def detach(self, owner: str, *, now: datetime | None = None) -> WorkerState:
        current = now or datetime.now(UTC)
        with self._lock:
            self._require_owner(owner, current)
            self.state = self.state.model_copy(
                update={"status": WorkerStatus.DETACHED, "lease": None}
            )
            return self.state

    def request_cancel(self) -> WorkerState:
        with self._lock:
            self.state = self.state.model_copy(update={"cancel_requested": True})
            return self.state

    def acknowledge_cancel(self, owner: str, *, now: datetime | None = None) -> WorkerState:
        current = now or datetime.now(UTC)
        with self._lock:
            self._require_owner(owner, current)
            if not self.state.cancel_requested:
                raise WorkerError("cancellation was not requested")
            self.state = self.state.model_copy(
                update={"status": WorkerStatus.CANCELLED, "lease": None}
            )
            return self.state

    def checkpoint(self, owner: str, snapshot: ControlSnapshot) -> WorkerState:
        with self._lock:
            self._require_owner(owner, datetime.now(UTC))
            if snapshot.run_id != self.state.run_id:
                raise WorkerError("snapshot belongs to a different run")
            self.snapshots.save(snapshot)
            self.state = self.state.model_copy(
                update={"last_checkpoint_sequence": snapshot.last_event_sequence}
            )
            return self.state

    def resume(
        self,
        owner: str,
        *,
        capability_digests: dict[str, str],
        plugin_digests: dict[str, str],
    ) -> tuple[ControlSnapshot, RecoveryReport]:
        with self._lock:
            self._require_owner(owner, datetime.now(UTC))
            snapshot = self.snapshots.load()
            snapshot.assert_compatible(
                run_id=self.state.run_id,
                capability_digests=capability_digests,
                plugin_digests=plugin_digests,
            )
            report = self.recovery.inspect(self.events.events())
            if not report.safe_to_resume:
                self.state = self.state.model_copy(
                    update={
                        "status": WorkerStatus.BLOCKED,
                        "detail": "uncertain non-idempotent capability requires reconciliation",
                    }
                )
            return snapshot, report

    def complete(self, owner: str, *, detail: str = "") -> WorkerState:
        with self._lock:
            self._require_owner(owner, datetime.now(UTC))
            self.state = self.state.model_copy(
                update={"status": WorkerStatus.COMPLETED, "lease": None, "detail": detail}
            )
            return self.state

    def _require_owner(self, owner: str, now: datetime) -> WorkerLease:
        lease = self.state.lease
        if lease is None or lease.owner != owner or not lease.active(now):
            raise WorkerError("run worker lease is missing, expired, or owned by another client")
        return lease
