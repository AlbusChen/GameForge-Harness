from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from gameforge.harness.capabilities import CapabilityBroker, CapabilityCallError
from gameforge.harness.events import EventKind, EventStore
from gameforge.harness.evidence import (
    BuildArtifact,
    EvidenceFreshness,
    EvidenceGraph,
    EvidenceSubjectKind,
    ImportedRevision,
    PlaySession,
    PlaySessionState,
)


class EngineLifecycleState(StrEnum):
    COLD = "COLD"
    SYNCING = "SYNCING"
    READY = "READY"
    PLAYING = "PLAYING"
    BUILDING = "BUILDING"
    DIRTY_OR_CRASHED = "DIRTY_OR_CRASHED"
    RESETTING = "RESETTING"


class EngineLifecycleError(RuntimeError):
    pass


class EngineLifecycleCoordinator:
    def __init__(
        self,
        *,
        broker: CapabilityBroker,
        evidence: EvidenceGraph,
        events: EventStore,
        engine_version: str,
        plugin_digest: str,
        import_settings_digest: str,
        environment_digest: str,
    ) -> None:
        self.broker = broker
        self.evidence = evidence
        self.events = events
        self.engine_version = engine_version
        self.plugin_digest = plugin_digest
        self.import_settings_digest = import_settings_digest
        self.environment_digest = environment_digest
        self.state = EngineLifecycleState.COLD
        self.current_imported_id: str | None = None
        self.current_build_id: str | None = None
        self.active_session_id: str | None = None
        self.cache_epoch = 0

    def value(self, key: str) -> str | None:
        return {
            "engine_state": self.state.value,
            "project_revision_id": self.evidence.current_project_id,
            "imported_revision_id": self.current_imported_id,
            "build_artifact_id": self.current_build_id,
            "play_session_id": self.active_session_id,
        }.get(key)

    def invalidate(self, kinds: tuple[str, ...]) -> None:
        invalidated = set(kinds)
        if invalidated & {"import", "build", "playtest", "capture", "gate"}:
            self.current_imported_id = None
            self.current_build_id = None
            self.active_session_id = None
            self.state = EngineLifecycleState.COLD

    def synchronize(
        self,
        capability: str,
        arguments: Mapping[str, object],
        *,
        artifact_hashes: tuple[str, ...] = (),
    ) -> tuple[ImportedRevision, object]:
        project_id = self.evidence.current_project_id
        if project_id is None:
            raise EngineLifecycleError("cannot synchronize before a project revision exists")
        if self.state not in {EngineLifecycleState.COLD, EngineLifecycleState.READY}:
            raise EngineLifecycleError(f"cannot synchronize while engine is {self.state}")
        self.state = EngineLifecycleState.SYNCING
        self.events.append(
            EventKind.ENGINE_SYNC_STARTED,
            subject_id=project_id,
            payload={"capability": capability, "cache_epoch": self.cache_epoch},
        )
        try:
            call = self.broker.invoke(capability, arguments)
            imported = self.evidence.record_imported_revision(
                project_revision_id=project_id,
                engine_version=self.engine_version,
                plugin_digest=self.plugin_digest,
                import_settings_digest=self.import_settings_digest,
                environment_digest=self.environment_digest,
                cache_epoch=self.cache_epoch,
                artifact_hashes=artifact_hashes,
            )
        except Exception as error:
            self.state = EngineLifecycleState.DIRTY_OR_CRASHED
            self.events.append(
                EventKind.ENGINE_SYNC_FAILED,
                subject_id=project_id,
                payload={"capability": capability, "error": str(error)},
            )
            raise
        self.current_imported_id = imported.id
        self.current_build_id = None
        self.active_session_id = None
        self.state = EngineLifecycleState.READY
        self.events.append(
            EventKind.ENGINE_SYNC_COMPLETED,
            subject_id=imported.id,
            payload={"call_id": call.call_id, "project_revision_id": project_id},
        )
        return imported, call.output

    def build(
        self,
        capability: str,
        arguments: Mapping[str, object],
        *,
        target_platform: str,
        build_configuration: str,
        artifact_hashes: tuple[str, ...],
    ) -> tuple[BuildArtifact, object]:
        imported = self._current_imported()
        self._require_state(EngineLifecycleState.READY)
        self.state = EngineLifecycleState.BUILDING
        self.events.append(
            EventKind.BUILD_STARTED,
            subject_id=imported.id,
            payload={
                "capability": capability,
                "target_platform": target_platform,
                "build_configuration": build_configuration,
            },
        )
        try:
            call = self.broker.invoke(capability, arguments)
            build = self.evidence.record_build(
                imported_revision_id=imported.id,
                target_platform=target_platform,
                build_configuration=build_configuration,
                environment_digest=self.environment_digest,
                artifact_hashes=artifact_hashes,
            )
        except Exception as error:
            self.state = EngineLifecycleState.DIRTY_OR_CRASHED
            self.events.append(
                EventKind.BUILD_FAILED,
                subject_id=imported.id,
                payload={"capability": capability, "error": str(error)},
            )
            raise
        self.current_build_id = build.id
        self.state = EngineLifecycleState.READY
        self.events.append(
            EventKind.BUILD_COMPLETED,
            subject_id=build.id,
            payload={"call_id": call.call_id},
        )
        return build, call.output

    def launch(
        self,
        capability: str,
        arguments: Mapping[str, object],
        *,
        entry_point: str,
        scenario_id: str | None = None,
        seed: int,
        fixed_timestep_seconds: float | None,
        prefer_build: bool = False,
    ) -> tuple[PlaySession, object]:
        self._require_state(EngineLifecycleState.READY)
        if prefer_build:
            if self.current_build_id is None:
                raise EngineLifecycleError("launch requested a build but no fresh build exists")
            parent_kind = EvidenceSubjectKind.BUILD_ARTIFACT
            parent_id = self.current_build_id
        else:
            imported = self._current_imported()
            parent_kind = EvidenceSubjectKind.IMPORTED_REVISION
            parent_id = imported.id
        try:
            call = self.broker.invoke(capability, arguments)
            session = self.evidence.start_session(
                parent_kind=parent_kind,
                parent_id=parent_id,
                entry_point=entry_point,
                scenario_id=scenario_id,
                seed=seed,
                fixed_timestep_seconds=fixed_timestep_seconds,
                environment_digest=self.environment_digest,
            )
        except Exception:
            self.state = EngineLifecycleState.DIRTY_OR_CRASHED
            raise
        self.active_session_id = session.id
        self.state = EngineLifecycleState.PLAYING
        return session, call.output

    def stop(
        self,
        capability: str,
        arguments: Mapping[str, object],
    ) -> tuple[PlaySession, object]:
        self._require_state(EngineLifecycleState.PLAYING)
        session_id = self._active_session_id()
        try:
            call = self.broker.invoke(capability, arguments)
        except CapabilityCallError as error:
            self.evidence.stop_session(session_id, crashed=True)
            self.active_session_id = None
            self.state = EngineLifecycleState.DIRTY_OR_CRASHED
            raise EngineLifecycleError(str(error)) from error
        session = self.evidence.stop_session(session_id)
        self.active_session_id = None
        self.state = EngineLifecycleState.READY
        return session, call.output

    def mark_crashed(self, detail: str) -> None:
        if self.active_session_id is not None:
            session = self.evidence.sessions[self.active_session_id]
            if session.state is PlaySessionState.ACTIVE:
                self.evidence.stop_session(session.id, crashed=True)
        self.active_session_id = None
        self.evidence.invalidate_engine_state(uncertain=True)
        self.state = EngineLifecycleState.DIRTY_OR_CRASHED
        self.events.append(
            EventKind.PLAY_SESSION_CRASHED,
            payload={"detail": detail},
        )

    def reset(
        self,
        capability: str | None = None,
        arguments: Mapping[str, object] | None = None,
    ) -> object | None:
        if self.state is EngineLifecycleState.PLAYING:
            raise EngineLifecycleError("stop the active play session before reset")
        self.state = EngineLifecycleState.RESETTING
        output: object | None = None
        if capability is not None:
            output = self.broker.invoke(capability, arguments or {}).output
        self.evidence.invalidate_engine_state()
        self.cache_epoch += 1
        self.current_imported_id = None
        self.current_build_id = None
        self.active_session_id = None
        self.state = EngineLifecycleState.COLD
        return output

    def _current_imported(self) -> ImportedRevision:
        if self.current_imported_id is None:
            raise EngineLifecycleError("no imported revision is ready")
        imported = self.evidence.imported_revisions[self.current_imported_id]
        if imported.freshness is not EvidenceFreshness.FRESH:
            raise EngineLifecycleError("current imported revision is not fresh")
        if imported.project_revision_id != self.evidence.current_project_id:
            raise EngineLifecycleError("current imported revision is not for current project")
        return imported

    def _active_session_id(self) -> str:
        if self.active_session_id is None:
            raise EngineLifecycleError("no active play session")
        return self.active_session_id

    def _require_state(self, expected: EngineLifecycleState) -> None:
        if self.state is not expected:
            raise EngineLifecycleError(f"engine must be {expected}; actual state is {self.state}")
