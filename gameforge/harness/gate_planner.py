from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.events import EventKind, EventStore
from gameforge.harness.evidence import (
    EvidenceGraph,
    EvidenceSubjectKind,
    GateEvidence,
    GateEvidenceStatus,
    ObservationKind,
    PlaySessionState,
)
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    GameTaskSpec,
    RequirementEnforcement,
)


class GateLevel(StrEnum):
    SOURCE = "L0_SOURCE"
    ENGINE_SYNC = "L1_ENGINE_SYNC"
    BEHAVIOR = "L2_BEHAVIOR"
    PRESENTATION = "L3_PRESENTATION"
    PERFORMANCE_BUILD = "L4_PERFORMANCE_BUILD"
    FINAL = "L5_FINAL"


class GateExecutionResult(StrictModel):
    status: GateEvidenceStatus
    detail: str = ""
    artifact_hashes: tuple[str, ...] = ()
    score: float | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    subject_kind: EvidenceSubjectKind | None = None
    subject_id: str | None = None


class GateDescriptor(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    level: GateLevel
    subject_kinds: tuple[EvidenceSubjectKind, ...] = Field(min_length=1)
    model_visible: bool = True
    evaluator_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


GateHandler = Callable[[AcceptanceRequirement, EvidenceSubjectKind, str], GateExecutionResult]


@dataclass(frozen=True)
class RegisteredGate:
    descriptor: GateDescriptor
    handler: GateHandler


class GateRegistry:
    def __init__(self) -> None:
        self._gates: dict[str, RegisteredGate] = {}
        self._frozen = False

    def register(self, descriptor: GateDescriptor, handler: GateHandler) -> None:
        if self._frozen:
            raise RuntimeError("gate registry is frozen")
        if descriptor.name in self._gates:
            raise ValueError(f"gate is already registered: {descriptor.name}")
        self._gates[descriptor.name] = RegisteredGate(descriptor, handler)

    def freeze(self) -> None:
        self._frozen = True

    def get(self, name: str) -> RegisteredGate:
        try:
            return self._gates[name]
        except KeyError as error:
            raise GatePlannerError(f"gate is not registered: {name}") from error

    def public_descriptors(self) -> tuple[GateDescriptor, ...]:
        return tuple(
            registration.descriptor
            for name in sorted(self._gates)
            if (registration := self._gates[name]).descriptor.model_visible
        )


class FinishDecision(StrictModel):
    allowed: bool
    reasons: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class GatePlannerError(RuntimeError):
    pass


class GatePlanner:
    def __init__(
        self,
        *,
        game_task: GameTaskSpec,
        environment_digest: str,
        evidence: EvidenceGraph,
        events: EventStore,
        registry: GateRegistry,
        target_platform: str | None = None,
    ) -> None:
        self.game_task = game_task
        self.environment_digest = environment_digest
        self.evidence = evidence
        self.events = events
        self.registry = registry
        self.target_platform = target_platform
        self.registry.freeze()

    def public_descriptions(self) -> tuple[dict[str, object], ...]:
        public_gate_names = {
            requirement.evaluator_ref for requirement in self._public_requirements()
        }
        return tuple(
            descriptor.model_dump(mode="json")
            for descriptor in self.registry.public_descriptors()
            if descriptor.name in public_gate_names
        )

    def run_public(
        self,
        requirement_id: str,
        *,
        subject_kind: EvidenceSubjectKind,
        subject_id: str,
    ) -> GateEvidence:
        requirement = self._requirement(requirement_id)
        if not requirement.model_visible:
            raise GatePlannerError("gate requirement is not model-visible")
        registration = self.registry.get(requirement.evaluator_ref)
        if subject_kind not in registration.descriptor.subject_kinds:
            raise GatePlannerError(
                f"gate {registration.descriptor.name} does not accept {subject_kind}"
            )
        if not self.evidence.is_current_descendant(subject_kind, subject_id):
            raise GatePlannerError("gate subject does not descend from current project revision")
        existing = self.evidence_for_requirement(requirement_id, passing_only=False)
        if (
            existing is not None
            and existing.status is GateEvidenceStatus.PASS
            and self._evidence_covers_requested_subject(
                existing,
                subject_kind=subject_kind,
                subject_id=subject_id,
            )
        ):
            self.events.append(
                EventKind.GATE_COMPLETED,
                subject_id=existing.id,
                payload={
                    "requirement_id": requirement.id,
                    "gate": registration.descriptor.name,
                    "cached": True,
                },
            )
            return existing
        return self.run_host(
            requirement_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )

    def _evidence_covers_requested_subject(
        self,
        gate: GateEvidence,
        *,
        subject_kind: EvidenceSubjectKind,
        subject_id: str,
    ) -> bool:
        if gate.subject_kind is subject_kind and gate.subject_id == subject_id:
            return True
        return (
            subject_kind is EvidenceSubjectKind.PROJECT_REVISION
            and self.evidence.project_ancestor(gate.subject_kind, gate.subject_id) == subject_id
        )

    def run_host(
        self,
        requirement_id: str,
        *,
        subject_kind: EvidenceSubjectKind,
        subject_id: str,
    ) -> GateEvidence:
        requirement = self._requirement(requirement_id)
        registration = self.registry.get(requirement.evaluator_ref)
        if subject_kind not in registration.descriptor.subject_kinds:
            raise GatePlannerError(
                f"gate {registration.descriptor.name} does not accept {subject_kind}"
            )
        if not self.evidence.is_current_descendant(subject_kind, subject_id):
            raise GatePlannerError("gate subject does not descend from current project revision")
        self.events.append(
            EventKind.GATE_STARTED,
            subject_id=subject_id,
            payload={
                "requirement_id": requirement.id,
                "gate": registration.descriptor.name,
                "level": registration.descriptor.level.value,
            },
        )
        result = registration.handler(requirement, subject_kind, subject_id)
        result_subject_kind = result.subject_kind or subject_kind
        result_subject_id = result.subject_id or subject_id
        if (result.subject_kind is None) != (result.subject_id is None):
            raise GatePlannerError("gate result must override subject kind and id together")
        if not self.evidence.is_current_descendant(result_subject_kind, result_subject_id):
            raise GatePlannerError("gate result subject does not descend from current project")
        return self.evidence.record_gate(
            requirement_id=requirement.id,
            subject_kind=result_subject_kind,
            subject_id=result_subject_id,
            status=result.status,
            environment_digest=self.environment_digest,
            evaluator_digest=registration.descriptor.evaluator_digest,
            artifact_hashes=result.artifact_hashes,
            detail=result.detail,
            score=result.score,
            uncertainty=result.uncertainty,
        )

    def finish_decision(
        self,
        *,
        no_unapproved_changes: bool,
        budgets_within_limits: bool,
        no_uncertain_mutation: bool,
        engine_runtime_reconciled: bool,
        include_hidden: bool = True,
    ) -> FinishDecision:
        reasons: list[str] = []
        evidence_ids: list[str] = []
        if not no_unapproved_changes:
            reasons.append("unapproved project changes remain")
        if not budgets_within_limits:
            reasons.append("run budgets are exhausted")
        if not no_uncertain_mutation:
            reasons.append("an uncertain mutation has not been reconciled")
        if not engine_runtime_reconciled:
            reasons.append("engine runtime state is not reconciled")
        for requirement in self.game_task.required_requirements():
            if not include_hidden and not requirement.model_visible:
                continue
            latest = self.latest_evidence_for_requirement(requirement.id)
            gate = self.evidence_for_requirement(requirement.id, passing_only=False)
            if gate is None:
                reasons.append(
                    (
                        "missing fresh required evidence: "
                        if latest is None
                        else "required evidence has the wrong game subject or metadata: "
                    )
                    + requirement.id
                )
            elif gate.status is not GateEvidenceStatus.PASS:
                reasons.append(f"latest required evidence did not pass: {requirement.id}")
            else:
                evidence_ids.append(gate.id)
        return FinishDecision(
            allowed=not reasons,
            reasons=tuple(reasons),
            evidence_ids=tuple(evidence_ids),
        )

    def scored_evidence(self) -> tuple[GateEvidence, ...]:
        scored = {
            requirement.id
            for requirement in self.game_task.requirements
            if requirement.enforcement is RequirementEnforcement.SCORED
        }
        return tuple(
            gate
            for gate in self.evidence.gates.values()
            if gate.requirement_id in scored
            and self.evidence.is_current_descendant(gate.subject_kind, gate.subject_id)
        )

    def evidence_for_requirement(
        self, requirement_id: str, *, passing_only: bool = True
    ) -> GateEvidence | None:
        requirement = self._requirement(requirement_id)
        gate = self.latest_evidence_for_requirement(requirement_id)
        if gate is None or not self._evidence_matches_requirement(requirement, gate):
            return None
        if passing_only and gate.status is not GateEvidenceStatus.PASS:
            return None
        return gate

    def latest_evidence_for_requirement(self, requirement_id: str) -> GateEvidence | None:
        requirement = self._requirement(requirement_id)
        candidates = tuple(
            gate
            for gate in self.evidence.gates.values()
            if gate.requirement_id == requirement.id
            and gate.environment_digest == self.environment_digest
            and self.evidence.subject_is_fresh(gate.subject_kind, gate.subject_id)
        )
        return candidates[-1] if candidates else None

    def _requirement(self, requirement_id: str) -> AcceptanceRequirement:
        for requirement in self.game_task.requirements:
            if requirement.id == requirement_id:
                return requirement
        raise GatePlannerError(f"unknown acceptance requirement: {requirement_id}")

    def _public_requirements(self) -> tuple[AcceptanceRequirement, ...]:
        return tuple(
            requirement for requirement in self.game_task.requirements if requirement.model_visible
        )

    def _evidence_matches_requirement(
        self,
        requirement: AcceptanceRequirement,
        gate: GateEvidence,
    ) -> bool:
        kind = gate.subject_kind
        subject_id = gate.subject_id
        session_id: str | None = None
        if kind is EvidenceSubjectKind.OBSERVATION:
            observation = self.evidence.observations[subject_id]
            if observation.environment_digest != self.environment_digest:
                return False
            if observation.subject_kind is EvidenceSubjectKind.PLAY_SESSION:
                session_id = observation.subject_id
            if requirement.dimension is AcceptanceDimension.STRUCTURE:
                valid = observation.kind is ObservationKind.STRUCTURED
            elif requirement.dimension is AcceptanceDimension.BEHAVIOR:
                valid = observation.kind in {
                    ObservationKind.STRUCTURED,
                    ObservationKind.LOG,
                    ObservationKind.INPUT_TRACE,
                }
            elif requirement.dimension is AcceptanceDimension.VISUAL:
                valid = observation.kind in {
                    ObservationKind.SCREENSHOT,
                    ObservationKind.VIDEO,
                }
            elif requirement.dimension is AcceptanceDimension.AUDIO:
                valid = observation.kind is ObservationKind.AUDIO
            elif requirement.dimension is AcceptanceDimension.PERFORMANCE:
                valid = observation.kind is ObservationKind.PERFORMANCE
            else:
                valid = False
            if not valid:
                return False
            if (
                self.game_task.evidence_policy.require_input_trace_for_runtime_evidence
                and observation.input_trace_hash is None
            ):
                return False
        elif kind is EvidenceSubjectKind.PLAY_SESSION:
            session_id = subject_id
            valid = requirement.dimension is AcceptanceDimension.BEHAVIOR
        elif kind is EvidenceSubjectKind.BUILD_ARTIFACT:
            build = self.evidence.builds[subject_id]
            valid = requirement.dimension is AcceptanceDimension.BUILD
            if self.target_platform is not None and build.target_platform != self.target_platform:
                return False
            if build.environment_digest != self.environment_digest:
                return False
        elif kind is EvidenceSubjectKind.IMPORTED_REVISION:
            imported = self.evidence.imported_revisions[subject_id]
            valid = requirement.dimension is AcceptanceDimension.STRUCTURE
            if imported.environment_digest != self.environment_digest:
                return False
        else:
            valid = requirement.dimension in {
                AcceptanceDimension.STRUCTURE,
                AcceptanceDimension.PRESERVATION,
                AcceptanceDimension.ASSET_COMPLIANCE,
            }
        if not valid:
            return False
        if session_id is not None:
            session = self.evidence.sessions[session_id]
            if session.state not in {PlaySessionState.ACTIVE, PlaySessionState.STOPPED}:
                return False
            if session.environment_digest != self.environment_digest:
                return False
            if (
                self.game_task.evidence_policy.require_input_trace_for_runtime_evidence
                and session.input_trace_hash is None
            ):
                return False
            if requirement.scenario_refs and session.scenario_id not in requirement.scenario_refs:
                return False
        elif requirement.scenario_refs:
            return False
        return True
