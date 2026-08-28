from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.capabilities import (
    CapabilityBroker,
    CapabilityCallError,
    CapabilityPhase,
    ConcurrencyMode,
)
from gameforge.harness.control_language import (
    ControlExecution,
    ControlLimits,
    ControlProgramError,
    SafeInterpreter,
    host_function,
    namespace,
)
from gameforge.harness.engine_lifecycle import EngineLifecycleCoordinator, EngineLifecycleState
from gameforge.harness.errors import InfrastructureFailure
from gameforge.harness.events import EventKind, EventStore
from gameforge.harness.evidence import EvidenceSubjectKind
from gameforge.harness.game_tasks import PlaytestScenario
from gameforge.harness.gate_planner import FinishDecision, GatePlanner
from gameforge.harness.playtest import PlaytestRunner
from gameforge.harness.program_state import ProgramState


class CapabilityCallSpec(StrictModel):
    name: str = Field(min_length=1)
    arguments: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str | None = None


class FinishProposal(StrictModel):
    summary: str = Field(min_length=1)
    cited_evidence: tuple[str, ...] = ()
    public_decision: FinishDecision


class ControlCellResult(StrictModel):
    cell_number: int = Field(ge=1)
    execution: ControlExecution
    observations: tuple[dict[str, object], ...]
    notes: tuple[str, ...]
    finish_proposal: FinishProposal | None = None


@dataclass(frozen=True)
class FinishContext:
    no_unapproved_changes: bool = True
    budgets_within_limits: bool = True
    no_uncertain_mutation: bool = True


class RestrictedControlSession:
    def __init__(
        self,
        *,
        broker: CapabilityBroker,
        gates: GatePlanner,
        events: EventStore,
        state: ProgramState | None = None,
        limits: ControlLimits | None = None,
        lifecycle: EngineLifecycleCoordinator | None = None,
        playtest_runner: PlaytestRunner | None = None,
        playtest_scenarios: Mapping[str, PlaytestScenario] | None = None,
        lifecycle_state: Callable[[], EngineLifecycleState] | None = None,
        finish_context: Callable[[], FinishContext] | None = None,
        cell_completion_hook: Callable[[], tuple[dict[str, object], ...]] | None = None,
        maximum_parallel_calls: int = 8,
    ) -> None:
        if maximum_parallel_calls < 1 or maximum_parallel_calls > 32:
            raise ValueError("maximum_parallel_calls must be between 1 and 32")
        self.broker = broker
        self.gates = gates
        self.events = events
        self.state = state or ProgramState()
        self.limits = limits or ControlLimits()
        self.lifecycle = lifecycle
        self.playtest_runner = playtest_runner
        self.playtest_scenarios = dict(playtest_scenarios or {})
        self.lifecycle_state = lifecycle_state or (
            (lambda: lifecycle.state)
            if lifecycle is not None
            else (lambda: EngineLifecycleState.COLD)
        )
        self.finish_context = finish_context or (lambda: FinishContext())
        self.cell_completion_hook = cell_completion_hook
        self.maximum_parallel_calls = maximum_parallel_calls
        self._observations: list[dict[str, object]] = []
        self._pending_notes: list[str] = []
        self._finish_proposal: FinishProposal | None = None
        self._observation_lock = threading.RLock()
        forge = namespace(
            "forge",
            {
                "call": host_function("forge.call", self._call),
                "parallel": host_function("forge.parallel", self._parallel),
                "note": host_function("forge.note", self._note),
                "engine": namespace(
                    "forge.engine",
                    {
                        "sync": host_function("forge.engine.sync", self._engine_sync),
                        "build": host_function("forge.engine.build", self._engine_build),
                        "reset": host_function("forge.engine.reset", self._engine_reset),
                    },
                ),
                "playtest": namespace(
                    "forge.playtest",
                    {"run": host_function("forge.playtest.run", self._playtest_run)},
                ),
                "capture": namespace(
                    "forge.capture",
                    {"run": host_function("forge.capture.run", self._capture_run)},
                ),
                "gates": namespace(
                    "forge.gates", {"run": host_function("forge.gates.run", self._run_gate)}
                ),
                "finish": namespace(
                    "forge.finish",
                    {
                        "propose": host_function(
                            "forge.finish.propose", self._propose_finish_for_program
                        )
                    },
                ),
                "state": namespace(
                    "forge.state",
                    {
                        "get": host_function("forge.state.get", self._state_get),
                        "set": host_function("forge.state.set", self._state_set),
                    },
                ),
            },
        )
        self._interpreter = SafeInterpreter(
            namespaces={"forge": forge},
            variables=self.state.variables,
            function_sources=self.state.functions,
            limits=self.limits,
        )

    def execute_cell(self, source: str) -> ControlCellResult:
        cell_number = self.state.cell_number + 1
        self._observations = []
        self._pending_notes = []
        self._finish_proposal = None
        self.events.append(
            EventKind.PROGRAM_CELL_STARTED,
            payload={"cell_number": cell_number, "source_bytes": len(source.encode("utf-8"))},
        )
        try:
            execution = self._interpreter.execute(source)
        except ControlProgramError as error:
            if self._finish_proposal is not None:
                self.events.append(
                    EventKind.FINISH_REJECTED,
                    payload={"reason": "control cell failed after proposing finish"},
                )
            self._finish_proposal = None
            self.events.append(
                EventKind.PROGRAM_CELL_FAILED,
                payload={"cell_number": cell_number, "error": str(error)},
            )
            raise
        if self.cell_completion_hook is not None:
            self._observations.extend(self.cell_completion_hook())
        self.state = ProgramState(
            cell_number=cell_number,
            variables=execution.variables,
            functions=execution.functions,
            notes=(*self.state.notes, *self._pending_notes),
        )
        completed = self.events.append(
            EventKind.PROGRAM_CELL_COMPLETED,
            payload={
                "cell_number": cell_number,
                "steps": execution.steps,
                "observation_count": len(self._observations),
            },
        )
        del completed
        return ControlCellResult(
            cell_number=cell_number,
            execution=execution,
            observations=tuple(self._observations),
            notes=tuple(self._pending_notes),
            finish_proposal=self._finish_proposal,
        )

    def execute_direct_capability(
        self,
        capability: str,
        arguments: Mapping[str, object],
    ) -> ControlCellResult:
        """Execute one ordinary author/observe capability without a nested language layer."""
        descriptor = self.broker.registry.get(capability).descriptor
        if descriptor.phase not in {CapabilityPhase.OBSERVE, CapabilityPhase.AUTHOR}:
            raise ControlProgramError(f"{capability} is not an ordinary author/observe capability")
        return self._execute_direct(
            capability,
            arguments,
            lambda: self._call(capability, **dict(arguments)),
        )

    def execute_registered_capability(
        self,
        capability: str,
        arguments: Mapping[str, object],
    ) -> ControlCellResult:
        """Dispatch one registered capability through its Host-owned lifecycle semantics."""
        descriptor = self.broker.registry.get(capability).descriptor
        if descriptor.phase in {CapabilityPhase.OBSERVE, CapabilityPhase.AUTHOR}:

            def invoke() -> object:
                return self._call(capability, **dict(arguments))
        elif descriptor.phase is CapabilityPhase.ENGINE_SYNC:

            def invoke() -> object:
                return self._engine_sync(capability, dict(arguments))
        elif descriptor.phase is CapabilityPhase.CAPTURE:

            def invoke() -> object:
                return self._capture_run(capability, dict(arguments))
        else:
            raise ControlProgramError(f"{capability} requires a specialized lifecycle decision")
        return self._execute_direct(capability, arguments, invoke)

    def _execute_direct(
        self,
        capability: str,
        arguments: Mapping[str, object],
        invoke: Callable[[], object],
    ) -> ControlCellResult:
        """Execute a selected Host operation and retain its normal observations."""
        cell_number = self.state.cell_number + 1
        self._observations = []
        self._pending_notes = []
        self._finish_proposal = None
        self.events.append(
            EventKind.PROGRAM_CELL_STARTED,
            payload={
                "cell_number": cell_number,
                "source_bytes": 0,
                "direct_capability": capability,
            },
        )
        try:
            invoke()
        except InfrastructureFailure:
            raise
        except ControlProgramError as error:
            self.events.append(
                EventKind.PROGRAM_CELL_FAILED,
                payload={"cell_number": cell_number, "error": str(error)},
            )
            raise
        except Exception as error:
            wrapped = ControlProgramError(f"host call forge.call failed: {error}")
            self.events.append(
                EventKind.PROGRAM_CELL_FAILED,
                payload={"cell_number": cell_number, "error": str(wrapped)},
            )
            raise wrapped from error
        if self.cell_completion_hook is not None:
            self._observations.extend(self.cell_completion_hook())
        execution = ControlExecution(
            value=None,
            steps=0,
            variables=self._interpreter.variables,
            functions=self._interpreter.function_sources,
        )
        self.state = ProgramState(
            cell_number=cell_number,
            variables=execution.variables,
            functions=execution.functions,
            notes=self.state.notes,
        )
        self.events.append(
            EventKind.PROGRAM_CELL_COMPLETED,
            payload={
                "cell_number": cell_number,
                "steps": 0,
                "observation_count": len(self._observations),
                "direct_capability": capability,
            },
        )
        return ControlCellResult(
            cell_number=cell_number,
            execution=execution,
            observations=tuple(self._observations),
            notes=(),
            finish_proposal=None,
        )

    @property
    def last_cell_observations(self) -> tuple[dict[str, object], ...]:
        return tuple(self._observations)

    def propose_finish(self, summary: str, evidence: tuple[str, ...] = ()) -> FinishProposal:
        return self._propose_finish(summary=summary, evidence=list(evidence))

    def _call(
        self,
        name: str,
        *,
        idempotency_key: str | None = None,
        **arguments: object,
    ) -> object:
        descriptor = self.broker.registry.get(name).descriptor
        if descriptor.phase not in {CapabilityPhase.OBSERVE, CapabilityPhase.AUTHOR}:
            route = {
                CapabilityPhase.ENGINE_SYNC: "forge.engine.sync",
                CapabilityPhase.BUILD: "forge.engine.build",
                CapabilityPhase.PLAYTEST: "forge.playtest.run",
                CapabilityPhase.CAPTURE: "forge.capture.run",
            }.get(descriptor.phase, "the matching lifecycle namespace")
            raise ControlProgramError(f"{name} is lifecycle-managed; use {route}")
        result = self.broker.invoke(name, arguments, idempotency_key=idempotency_key)
        observation = _program_observation(
            name=name,
            output=result.output,
            call_id=result.call_id,
            event_id=result.event_id,
            cached=result.cached,
        )
        with self._observation_lock:
            self._observations.append(observation)
        return observation["value"]

    def _parallel(self, calls: list[object]) -> list[object]:
        if len(calls) > self.maximum_parallel_calls:
            raise ControlProgramError("parallel capability count exceeds configured limit")
        specs = tuple(CapabilityCallSpec.model_validate(call) for call in calls)
        for spec in specs:
            descriptor = self.broker.registry.get(spec.name).descriptor
            if descriptor.concurrency is not ConcurrencyMode.PARALLEL_READ:
                raise ControlProgramError(
                    f"parallel only accepts parallel_read capabilities: {spec.name}"
                )

        results = self.broker.invoke_parallel_read(
            tuple((spec.name, spec.arguments, spec.idempotency_key) for spec in specs)
        )
        observations = [
            _program_observation(
                name=spec.name,
                output=result.output,
                call_id=result.call_id,
                event_id=result.event_id,
                cached=result.cached,
            )
            for spec, result in zip(specs, results, strict=True)
        ]
        with self._observation_lock:
            self._observations.extend(observations)
        return [observation["value"] for observation in observations]

    def _note(self, message: str) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ControlProgramError("note must be a non-empty string")
        if len(message.encode("utf-8")) > 4 * 1024:
            raise ControlProgramError("note exceeds 4 KiB")
        self._pending_notes.append(message)

    def _run_gate(self, requirement_id: str, *, subject_kind: str, subject_id: str) -> object:
        gate = self.gates.run_public(
            requirement_id,
            subject_kind=EvidenceSubjectKind(subject_kind),
            subject_id=subject_id,
        )
        payload = gate.model_dump(mode="json")
        with self._observation_lock:
            self._observations.append(
                {
                    "capability": "forge.gates.run",
                    "value": payload,
                    "event_id": gate.id,
                    "cached": False,
                }
            )
        return payload

    def _engine_sync(
        self,
        capability: str,
        arguments: dict[str, object] | None = None,
        artifact_hashes: list[object] | None = None,
    ) -> dict[str, object]:
        lifecycle = self._require_lifecycle()
        imported, output = lifecycle.synchronize(
            capability,
            arguments or {},
            artifact_hashes=tuple(str(item) for item in (artifact_hashes or [])),
        )
        result = {"subject": imported.model_dump(mode="json"), "output": output}
        self._record_host_observation(f"forge.engine.sync:{capability}", result)
        return result

    def _engine_build(
        self,
        capability: str,
        *,
        target_platform: str,
        build_configuration: str,
        artifact_hashes: list[object],
        arguments: dict[str, object] | None = None,
    ) -> dict[str, object]:
        lifecycle = self._require_lifecycle()
        build, output = lifecycle.build(
            capability,
            arguments or {},
            target_platform=target_platform,
            build_configuration=build_configuration,
            artifact_hashes=tuple(str(item) for item in artifact_hashes),
        )
        result = {"subject": build.model_dump(mode="json"), "output": output}
        self._record_host_observation(f"forge.engine.build:{capability}", result)
        return result

    def _engine_reset(
        self,
        capability: str | None = None,
        arguments: dict[str, object] | None = None,
    ) -> object:
        result = self._require_lifecycle().reset(capability, arguments)
        self._record_host_observation("forge.engine.reset", result)
        return result

    def _playtest_run(self, scenario_id: str) -> object:
        if self.playtest_runner is None:
            raise ControlProgramError("no playtest runner is configured")
        try:
            scenario = self.playtest_scenarios[scenario_id]
        except KeyError as error:
            raise ControlProgramError(f"unknown playtest scenario: {scenario_id}") from error
        result = [item.model_dump(mode="json") for item in self.playtest_runner.run(scenario)]
        self._record_host_observation(f"forge.playtest.run:{scenario_id}", result)
        return result

    def _capture_run(
        self,
        capability: str,
        arguments: dict[str, object] | None = None,
    ) -> object:
        descriptor = self.broker.registry.get(capability).descriptor
        if descriptor.phase is not CapabilityPhase.CAPTURE:
            raise ControlProgramError(
                f"forge.capture.run requires a capture capability: {capability}"
            )
        result = self.broker.invoke(capability, arguments or {})
        observation = _program_observation(
            name=f"forge.capture.run:{capability}",
            output=result.output,
            call_id=result.call_id,
            event_id=result.event_id,
            cached=result.cached,
        )
        with self._observation_lock:
            self._observations.append(observation)
        return observation["value"]

    def _record_host_observation(self, capability: str, value: object) -> None:
        """Keep lifecycle results visible independently from mutable program variables."""
        with self._observation_lock:
            self._observations.append(
                {
                    "capability": capability,
                    "value": value,
                    "cached": False,
                }
            )

    def _propose_finish(
        self, *, summary: str, evidence: list[object] | None = None
    ) -> FinishProposal:
        if not isinstance(summary, str) or not summary.strip():
            raise ControlProgramError("finish summary must be a non-empty string")
        evidence_ids = tuple(str(item) for item in (evidence or []))
        context = self.finish_context()
        engine_state = self.lifecycle_state()
        reconciled = engine_state not in {
            EngineLifecycleState.PLAYING,
            EngineLifecycleState.DIRTY_OR_CRASHED,
            EngineLifecycleState.SYNCING,
            EngineLifecycleState.BUILDING,
            EngineLifecycleState.RESETTING,
        }
        decision = self.gates.finish_decision(
            no_unapproved_changes=context.no_unapproved_changes,
            budgets_within_limits=context.budgets_within_limits,
            no_uncertain_mutation=context.no_uncertain_mutation,
            engine_runtime_reconciled=reconciled,
            include_hidden=False,
        )
        proposal = FinishProposal(
            summary=summary.strip(),
            cited_evidence=evidence_ids,
            public_decision=decision,
        )
        self._finish_proposal = proposal
        event = self.events.append(
            EventKind.FINISH_PROPOSED,
            payload=proposal.model_dump(mode="json"),
        )
        self.events.append(
            EventKind.FINISH_ACCEPTED if decision.allowed else EventKind.FINISH_REJECTED,
            payload={"proposal_event_id": event.event_id, "reasons": list(decision.reasons)},
        )
        return proposal

    def _propose_finish_for_program(
        self, *, summary: str, evidence: list[object] | None = None
    ) -> dict[str, object]:
        return self._propose_finish(summary=summary, evidence=evidence).model_dump(mode="json")

    def _state_get(self, key: str, default: object = None) -> object:
        state = self._interpreter.variables.get("forge_state", {})
        if not isinstance(state, dict):
            raise ControlProgramError("forge_state is not an object")
        return state.get(key, default)

    def _state_set(self, key: str, value: object) -> object:
        if not isinstance(key, str) or not key or key.startswith("_"):
            raise ControlProgramError("state key must be a public non-empty string")
        state = self._interpreter.variables.setdefault("forge_state", {})
        if not isinstance(state, dict):
            raise ControlProgramError("forge_state is not an object")
        state[key] = value
        return value

    def _require_lifecycle(self) -> EngineLifecycleCoordinator:
        if self.lifecycle is None:
            raise ControlProgramError("no engine lifecycle coordinator is configured")
        return self.lifecycle


def _program_observation(
    *,
    name: str,
    output: object,
    call_id: str,
    event_id: str,
    cached: bool,
) -> dict[str, object]:
    return {
        "capability": name,
        "value": output,
        "call_id": call_id,
        "event_id": event_id,
        "cached": cached,
    }


__all__ = [
    "CapabilityCallError",
    "ControlCellResult",
    "FinishContext",
    "FinishProposal",
    "RestrictedControlSession",
]
