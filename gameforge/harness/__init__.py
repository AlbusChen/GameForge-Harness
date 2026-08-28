"""Model-, benchmark-, and engine-neutral agent harness primitives."""

from gameforge.harness.contracts import (
    ArtifactRecord,
    EngineName,
    EvaluationSpec,
    GateOutcome,
    GateStatus,
    HarnessProfile,
    ModelRunSpec,
    NormalizedTask,
    RunBudgets,
    RunResult,
    RunSpec,
    RunStatus,
    TaskSourceKind,
)
from gameforge.harness.preservation import PreservationReport, ProjectSnapshot
from gameforge.harness.project_context import ProjectContext, SourceDocument

__all__ = [
    "ArtifactRecord",
    "EngineName",
    "EvaluationSpec",
    "GateOutcome",
    "GateStatus",
    "HarnessProfile",
    "ModelRunSpec",
    "NormalizedTask",
    "ProjectContext",
    "ProjectSnapshot",
    "PreservationReport",
    "RunBudgets",
    "RunResult",
    "RunSpec",
    "RunStatus",
    "SourceDocument",
    "TaskSourceKind",
]
