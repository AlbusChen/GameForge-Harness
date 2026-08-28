from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import Field

from gameforge.harness.artifacts import ArtifactHandle, ContentAddressedArtifactStore
from gameforge.harness.base import StrictModel
from gameforge.harness.capabilities import CapabilityBroker
from gameforge.harness.engine_lifecycle import EngineLifecycleCoordinator
from gameforge.harness.events import EventKind, EventStore
from gameforge.harness.evidence import (
    EvidenceGraph,
    EvidenceSubjectKind,
    ObservationArtifact,
    ObservationKind,
    PlaySession,
)
from gameforge.harness.game_tasks import (
    CaptureKind,
    CaptureRequest,
    PlaytestScenario,
    RuntimeProbe,
    TimedInputAction,
)


class ObservationBundle(StrictModel):
    play_session_id: str
    input_trace: ArtifactHandle
    structured: tuple[ObservationArtifact, ...] = ()
    temporal: tuple[ObservationArtifact, ...] = ()
    media: tuple[ObservationArtifact, ...] = ()
    quality: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class PlaytestExecution(StrictModel):
    scenario_id: str
    repetition: int = Field(ge=1)
    session: PlaySession
    bundle: ObservationBundle


LaunchArguments = Callable[[PlaytestScenario], Mapping[str, object]]
ActionArguments = Callable[[str, TimedInputAction], Mapping[str, object]]
ProbeArguments = Callable[[str, RuntimeProbe], Mapping[str, object]]
CaptureArguments = Callable[[str, CaptureRequest], Mapping[str, object]]
StopArguments = Callable[[str], Mapping[str, object]]


@dataclass(frozen=True)
class PlaytestBindings:
    launch_capability: str
    stop_capability: str
    action_capability: str | None = None
    probe_capability: str | None = None
    capture_capabilities: Mapping[CaptureKind, str] | None = None
    launch_arguments: LaunchArguments = lambda scenario: {
        "entry_point": scenario.entry_point,
        "seed": scenario.seed,
    }
    action_arguments: ActionArguments = lambda session_id, action: {
        "session_id": session_id,
        "action": action.model_dump(mode="json", exclude_none=True),
    }
    probe_arguments: ProbeArguments = lambda session_id, probe: {
        "session_id": session_id,
        "probe": probe.model_dump(mode="json", exclude_none=True),
    }
    capture_arguments: CaptureArguments = lambda session_id, capture: {
        "session_id": session_id,
        "capture": capture.model_dump(mode="json", exclude_none=True),
    }
    stop_arguments: StopArguments = lambda session_id: {"session_id": session_id}


class PlaytestError(RuntimeError):
    pass


class PlaytestRunner:
    def __init__(
        self,
        *,
        broker: CapabilityBroker,
        lifecycle: EngineLifecycleCoordinator,
        evidence: EvidenceGraph,
        events: EventStore,
        artifacts: ContentAddressedArtifactStore,
        bindings: PlaytestBindings,
    ) -> None:
        self.broker = broker
        self.lifecycle = lifecycle
        self.evidence = evidence
        self.events = events
        self.artifacts = artifacts
        self.bindings = bindings

    def run(self, scenario: PlaytestScenario) -> tuple[PlaytestExecution, ...]:
        executions: list[PlaytestExecution] = []
        for repetition in range(1, scenario.repetitions + 1):
            executions.append(self._run_once(scenario, repetition))
        return tuple(executions)

    def _run_once(self, scenario: PlaytestScenario, repetition: int) -> PlaytestExecution:
        session, launch_output = self.lifecycle.launch(
            self.bindings.launch_capability,
            self.bindings.launch_arguments(scenario),
            entry_point=scenario.entry_point,
            scenario_id=scenario.id,
            seed=scenario.seed,
            fixed_timestep_seconds=scenario.fixed_timestep_seconds,
        )
        external_session_id = _external_session_id(launch_output, session.id)
        trace = self.artifacts.put_json(
            {
                "scenario_id": scenario.id,
                "repetition": repetition,
                "entry_point": scenario.entry_point,
                "seed": scenario.seed,
                "fixed_timestep_seconds": scenario.fixed_timestep_seconds,
                "warmup_frames": scenario.warmup_frames,
                "actions": [action.model_dump(mode="json") for action in scenario.actions],
            },
            kind="input_trace",
            metadata={"play_session_id": session.id},
        )
        session = self.evidence.bind_input_trace(session.id, trace.sha256)
        self.events.append(
            EventKind.INPUT_REPLAY_STARTED,
            subject_id=session.id,
            payload={"trace_hash": trace.sha256, "action_count": len(scenario.actions)},
        )
        observations: list[ObservationArtifact] = []
        try:
            self._replay_actions(external_session_id, scenario.actions)
            self.events.append(
                EventKind.INPUT_REPLAY_COMPLETED,
                subject_id=session.id,
                payload={"trace_hash": trace.sha256},
            )
            observations.extend(
                self._run_probes(session, external_session_id, scenario.public_probes)
            )
            observations.extend(self._run_captures(session, external_session_id, scenario.captures))
        except Exception as error:
            self.lifecycle.mark_crashed(str(error))
            raise
        else:
            self.lifecycle.stop(
                self.bindings.stop_capability,
                self.bindings.stop_arguments(external_session_id),
            )
        bundle = ObservationBundle(
            play_session_id=session.id,
            input_trace=trace,
            structured=tuple(
                item for item in observations if item.kind is ObservationKind.STRUCTURED
            ),
            temporal=tuple(
                item
                for item in observations
                if item.kind in {ObservationKind.LOG, ObservationKind.PERFORMANCE}
            ),
            media=tuple(
                item
                for item in observations
                if item.kind
                in {ObservationKind.SCREENSHOT, ObservationKind.VIDEO, ObservationKind.AUDIO}
            ),
            quality={
                "deterministic": scenario.fixed_timestep_seconds is not None,
                "observation_count": len(observations),
            },
        )
        return PlaytestExecution(
            scenario_id=scenario.id,
            repetition=repetition,
            session=self.evidence.sessions[session.id],
            bundle=bundle,
        )

    def _replay_actions(
        self, external_session_id: str, actions: tuple[TimedInputAction, ...]
    ) -> None:
        if actions and self.bindings.action_capability is None:
            raise PlaytestError("scenario contains actions but no action capability is configured")
        for action in sorted(
            actions,
            key=lambda item: (
                item.frame if item.frame is not None else float("inf"),
                item.seconds if item.seconds is not None else float("inf"),
            ),
        ):
            assert self.bindings.action_capability is not None
            self.broker.invoke(
                self.bindings.action_capability,
                self.bindings.action_arguments(external_session_id, action),
            )

    def _run_probes(
        self,
        session: PlaySession,
        external_session_id: str,
        probes: tuple[RuntimeProbe, ...],
    ) -> tuple[ObservationArtifact, ...]:
        if probes and self.bindings.probe_capability is None:
            raise PlaytestError("scenario contains probes but no probe capability is configured")
        observations: list[ObservationArtifact] = []
        for probe in probes:
            assert self.bindings.probe_capability is not None
            result = self.broker.invoke(
                self.bindings.probe_capability,
                self.bindings.probe_arguments(external_session_id, probe),
            )
            handle = self.artifacts.put_json(
                result.output,
                kind="runtime_probe",
                metadata={"play_session_id": session.id, "probe": probe.name},
            )
            observations.append(
                self.evidence.record_observation(
                    subject_kind=EvidenceSubjectKind.PLAY_SESSION,
                    subject_id=session.id,
                    kind=ObservationKind.STRUCTURED,
                    artifact_hashes=(handle.sha256,),
                    environment_digest=self.lifecycle.environment_digest,
                    input_trace_hash=session.input_trace_hash,
                    frame_start=probe.at_frame,
                    frame_end=probe.at_frame,
                    metadata={
                        "probe": probe.name,
                        "selector": json.dumps(probe.selector, sort_keys=True),
                        "scenario_id": session.scenario_id,
                        "call_id": result.call_id,
                    },
                )
            )
        return tuple(observations)

    def _run_captures(
        self,
        session: PlaySession,
        external_session_id: str,
        captures: tuple[CaptureRequest, ...],
    ) -> tuple[ObservationArtifact, ...]:
        capabilities = self.bindings.capture_capabilities or {}
        observations: list[ObservationArtifact] = []
        for capture in captures:
            capability = capabilities.get(capture.kind)
            if capability is None:
                raise PlaytestError(f"no capture capability configured for {capture.kind}")
            result = self.broker.invoke(
                capability,
                self.bindings.capture_arguments(external_session_id, capture),
            )
            handle = self.artifacts.put_json(
                result.output,
                kind=f"{capture.kind.value}_capture",
                metadata={"play_session_id": session.id},
            )
            observations.append(
                self.evidence.record_observation(
                    subject_kind=EvidenceSubjectKind.PLAY_SESSION,
                    subject_id=session.id,
                    kind=_observation_kind(capture.kind),
                    artifact_hashes=(handle.sha256,),
                    environment_digest=self.lifecycle.environment_digest,
                    input_trace_hash=session.input_trace_hash,
                    frame_start=capture.at_frame,
                    frame_end=(
                        None
                        if capture.at_frame is None
                        else capture.at_frame + capture.frame_count - 1
                    ),
                    camera=capture.camera,
                    width=capture.width,
                    height=capture.height,
                    metadata={
                        "scenario_id": session.scenario_id,
                        "call_id": result.call_id,
                    },
                )
            )
        return tuple(observations)


def _external_session_id(output: object, fallback: str) -> str:
    if isinstance(output, Mapping):
        session_id = output.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return fallback


def _observation_kind(kind: CaptureKind) -> ObservationKind:
    return {
        CaptureKind.SCREENSHOT: ObservationKind.SCREENSHOT,
        CaptureKind.VIDEO: ObservationKind.VIDEO,
        CaptureKind.AUDIO: ObservationKind.AUDIO,
        CaptureKind.PERFORMANCE: ObservationKind.PERFORMANCE,
    }[kind]


__all__ = [
    "ObservationBundle",
    "PlaytestBindings",
    "PlaytestError",
    "PlaytestExecution",
    "PlaytestRunner",
]
