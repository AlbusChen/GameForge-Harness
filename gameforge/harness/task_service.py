from __future__ import annotations

import threading
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from gameforge.harness.artifacts import ArtifactHandle
from gameforge.harness.base import StrictModel
from gameforge.harness.events import EventKind, EventStore


class ChildTaskStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskBudget(StrictModel):
    wall_seconds: int = Field(ge=1, le=86_400)
    capability_calls: int = Field(ge=0, le=10_000)
    cost_usd: float = Field(ge=0, le=100_000)


class TaskUsage(StrictModel):
    wall_seconds: float = Field(default=0, ge=0)
    capability_calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)


class MutationLease(StrictModel):
    id: str
    child_run_id: str
    scopes: tuple[str, ...] = Field(min_length=1)
    active: bool = True


class TaskHandle(StrictModel):
    child_run_id: str
    parent_run_id: str
    status: ChildTaskStatus
    capability_set: tuple[str, ...]
    editable_scope: tuple[str, ...]
    budget_allocation: TaskBudget
    result_schema: dict[str, object]
    result_artifact: ArtifactHandle | None = None
    usage: TaskUsage = Field(default_factory=TaskUsage)
    lease_id: str | None = None
    failure_detail: str | None = None

    @model_validator(mode="after")
    def mutation_requires_lease(self) -> TaskHandle:
        if self.editable_scope and self.lease_id is None:
            raise ValueError("mutating child task requires a mutation lease")
        return self


class TaskServiceError(RuntimeError):
    pass


class MutationLeaseManager:
    def __init__(self) -> None:
        self._leases: dict[str, MutationLease] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def acquire(self, child_run_id: str, scopes: tuple[str, ...]) -> MutationLease:
        normalized = tuple(sorted({_safe_scope(scope) for scope in scopes}))
        if not normalized:
            raise TaskServiceError("mutation lease requires at least one scope")
        with self._lock:
            for lease in self._leases.values():
                if lease.active and any(
                    _overlaps(left, right) for left in normalized for right in lease.scopes
                ):
                    raise TaskServiceError(
                        f"mutation scope overlaps active lease {lease.id}: {lease.scopes}"
                    )
            self._counter += 1
            lease = MutationLease(
                id=f"lease:{self._counter:08d}",
                child_run_id=child_run_id,
                scopes=normalized,
            )
            self._leases[lease.id] = lease
            return lease

    def release(self, lease_id: str) -> None:
        with self._lock:
            try:
                lease = self._leases[lease_id]
            except KeyError as error:
                raise TaskServiceError(f"unknown mutation lease: {lease_id}") from error
            self._leases[lease_id] = lease.model_copy(update={"active": False})

    def active(self) -> tuple[MutationLease, ...]:
        return tuple(lease for lease in self._leases.values() if lease.active)


class TaskService:
    def __init__(
        self,
        *,
        parent_run_id: str,
        parent_budget: TaskBudget,
        events: EventStore,
        leases: MutationLeaseManager | None = None,
    ) -> None:
        self.parent_run_id = parent_run_id
        self.parent_budget = parent_budget
        self.events = events
        self.leases = leases or MutationLeaseManager()
        self._handles: dict[str, TaskHandle] = {}
        self._counter = 0
        self._lock = threading.RLock()

    def start(
        self,
        *,
        capability_set: tuple[str, ...],
        budget: TaskBudget,
        result_schema: dict[str, object],
        editable_scope: tuple[str, ...] = (),
    ) -> TaskHandle:
        with self._lock:
            self._ensure_allocation_fits(budget)
            self._counter += 1
            child_run_id = f"{self.parent_run_id}:child:{self._counter:04d}"
            lease = self.leases.acquire(child_run_id, editable_scope) if editable_scope else None
            handle = TaskHandle(
                child_run_id=child_run_id,
                parent_run_id=self.parent_run_id,
                status=ChildTaskStatus.RUNNING,
                capability_set=tuple(sorted(set(capability_set))),
                editable_scope=editable_scope,
                budget_allocation=budget,
                result_schema=result_schema,
                lease_id=None if lease is None else lease.id,
            )
            self._handles[child_run_id] = handle
            self.events.append(
                EventKind.CHILD_TASK_STARTED,
                subject_id=child_run_id,
                payload=handle.model_dump(mode="json"),
            )
            return handle

    def record_usage(self, child_run_id: str, usage: TaskUsage) -> TaskHandle:
        with self._lock:
            handle = self.get(child_run_id)
            if handle.status is not ChildTaskStatus.RUNNING:
                raise TaskServiceError("cannot add usage to a finished child task")
            combined = TaskUsage(
                wall_seconds=handle.usage.wall_seconds + usage.wall_seconds,
                capability_calls=handle.usage.capability_calls + usage.capability_calls,
                cost_usd=handle.usage.cost_usd + usage.cost_usd,
            )
            budget = handle.budget_allocation
            if (
                combined.wall_seconds > budget.wall_seconds
                or combined.capability_calls > budget.capability_calls
                or combined.cost_usd > budget.cost_usd
            ):
                raise TaskServiceError("child task exceeded its allocated budget")
            updated = handle.model_copy(update={"usage": combined})
            self._handles[child_run_id] = updated
            self._ensure_parent_usage_fits()
            return updated

    def complete(self, child_run_id: str, result_artifact: ArtifactHandle) -> TaskHandle:
        return self._finish(
            child_run_id,
            status=ChildTaskStatus.COMPLETED,
            result_artifact=result_artifact,
        )

    def fail(self, child_run_id: str, detail: str) -> TaskHandle:
        return self._finish(
            child_run_id,
            status=ChildTaskStatus.FAILED,
            failure_detail=detail,
        )

    def cancel(self, child_run_id: str) -> TaskHandle:
        return self._finish(child_run_id, status=ChildTaskStatus.CANCELLED)

    def get(self, child_run_id: str) -> TaskHandle:
        try:
            return self._handles[child_run_id]
        except KeyError as error:
            raise TaskServiceError(f"unknown child task: {child_run_id}") from error

    def handles(self) -> tuple[TaskHandle, ...]:
        return tuple(self._handles.values())

    def total_usage(self) -> TaskUsage:
        return TaskUsage(
            wall_seconds=sum(handle.usage.wall_seconds for handle in self._handles.values()),
            capability_calls=sum(
                handle.usage.capability_calls for handle in self._handles.values()
            ),
            cost_usd=sum(handle.usage.cost_usd for handle in self._handles.values()),
        )

    def _finish(
        self,
        child_run_id: str,
        *,
        status: ChildTaskStatus,
        result_artifact: ArtifactHandle | None = None,
        failure_detail: str | None = None,
    ) -> TaskHandle:
        with self._lock:
            handle = self.get(child_run_id)
            if handle.status is not ChildTaskStatus.RUNNING:
                raise TaskServiceError("child task has already finished")
            updated = handle.model_copy(
                update={
                    "status": status,
                    "result_artifact": result_artifact,
                    "failure_detail": failure_detail,
                }
            )
            self._handles[child_run_id] = updated
            if handle.lease_id is not None:
                self.leases.release(handle.lease_id)
            self.events.append(
                EventKind.CHILD_TASK_COMPLETED,
                subject_id=child_run_id,
                payload={"status": status.value, "failure_detail": failure_detail},
            )
            return updated

    def _ensure_allocation_fits(self, candidate: TaskBudget) -> None:
        allocated = TaskBudget(
            wall_seconds=sum(
                handle.budget_allocation.wall_seconds for handle in self._handles.values()
            )
            + candidate.wall_seconds,
            capability_calls=sum(
                handle.budget_allocation.capability_calls for handle in self._handles.values()
            )
            + candidate.capability_calls,
            cost_usd=sum(handle.budget_allocation.cost_usd for handle in self._handles.values())
            + candidate.cost_usd,
        )
        if (
            allocated.wall_seconds > self.parent_budget.wall_seconds
            or allocated.capability_calls > self.parent_budget.capability_calls
            or allocated.cost_usd > self.parent_budget.cost_usd
        ):
            raise TaskServiceError("child budget allocation exceeds parent budget")

    def _ensure_parent_usage_fits(self) -> None:
        usage = self.total_usage()
        if (
            usage.wall_seconds > self.parent_budget.wall_seconds
            or usage.capability_calls > self.parent_budget.capability_calls
            or usage.cost_usd > self.parent_budget.cost_usd
        ):
            raise TaskServiceError("attributed child usage exceeds parent budget")


def _safe_scope(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise TaskServiceError(f"unsafe mutation scope: {value}")
    return path.as_posix()


def _overlaps(left: str, right: str) -> bool:
    return bool(left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/"))
