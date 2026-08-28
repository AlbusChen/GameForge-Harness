from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from gameforge.harness.base import StrictModel
from gameforge.harness.events import EventKind, EventStore


class EvidenceSubjectKind(StrEnum):
    PROJECT_REVISION = "project_revision"
    IMPORTED_REVISION = "imported_revision"
    BUILD_ARTIFACT = "build_artifact"
    PLAY_SESSION = "play_session"
    OBSERVATION = "observation"


class EvidenceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNCERTAIN = "uncertain"


class PlaySessionState(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"
    CRASHED = "crashed"
    INVALID = "invalid"


class ObservationKind(StrEnum):
    STRUCTURED = "structured"
    LOG = "log"
    INPUT_TRACE = "input_trace"
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    AUDIO = "audio"
    PERFORMANCE = "performance"


class GateEvidenceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class ProjectRevision(StrictModel):
    id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    source_tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    diff_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    asset_graph_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    changed_paths: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ImportedRevision(StrictModel):
    id: str = Field(min_length=1)
    project_revision_id: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    plugin_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    import_settings_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    cache_epoch: int = Field(default=0, ge=0)
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH
    artifact_hashes: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BuildArtifact(StrictModel):
    id: str = Field(min_length=1)
    imported_revision_id: str = Field(min_length=1)
    target_platform: str = Field(min_length=1)
    build_configuration: str = Field(min_length=1)
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_hashes: tuple[str, ...] = Field(min_length=1)
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlaySession(StrictModel):
    id: str = Field(min_length=1)
    parent_kind: EvidenceSubjectKind
    parent_id: str = Field(min_length=1)
    entry_point: str = Field(min_length=1)
    scenario_id: str | None = Field(default=None, min_length=1)
    seed: int
    fixed_timestep_seconds: float | None = Field(default=None, gt=0, le=1)
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: PlaySessionState = PlaySessionState.ACTIVE
    input_trace_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    @model_validator(mode="after")
    def parent_is_runnable(self) -> PlaySession:
        if self.parent_kind not in {
            EvidenceSubjectKind.IMPORTED_REVISION,
            EvidenceSubjectKind.BUILD_ARTIFACT,
        }:
            raise ValueError("play session parent must be an imported revision or build artifact")
        return self


class ObservationArtifact(StrictModel):
    id: str = Field(min_length=1)
    subject_kind: EvidenceSubjectKind
    subject_id: str = Field(min_length=1)
    kind: ObservationKind
    artifact_hashes: tuple[str, ...] = Field(min_length=1)
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_trace_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    frame_start: int | None = Field(default=None, ge=0)
    frame_end: int | None = Field(default=None, ge=0)
    camera: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def frame_range_is_ordered(self) -> ObservationArtifact:
        if (
            self.frame_start is not None
            and self.frame_end is not None
            and self.frame_end < self.frame_start
        ):
            raise ValueError("observation frame range is reversed")
        if self.kind in {ObservationKind.SCREENSHOT, ObservationKind.VIDEO}:
            if self.width is None or self.height is None:
                raise ValueError("visual observations require width and height")
            if not self.camera:
                raise ValueError("visual observations require camera identity")
        return self


class GateEvidence(StrictModel):
    id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    subject_kind: EvidenceSubjectKind
    subject_id: str = Field(min_length=1)
    status: GateEvidenceStatus
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_hashes: tuple[str, ...] = ()
    evaluator_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    detail: str = ""
    score: float | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvidenceSnapshot(StrictModel):
    current_project_id: str | None = None
    project_revisions: tuple[ProjectRevision, ...] = ()
    imported_revisions: tuple[ImportedRevision, ...] = ()
    builds: tuple[BuildArtifact, ...] = ()
    sessions: tuple[PlaySession, ...] = ()
    observations: tuple[ObservationArtifact, ...] = ()
    gates: tuple[GateEvidence, ...] = ()


class EvidenceError(RuntimeError):
    pass


class EvidenceGraph:
    def __init__(self, *, events: EventStore | None = None) -> None:
        self.events = events
        self.current_project_id: str | None = None
        self.project_revisions: dict[str, ProjectRevision] = {}
        self.imported_revisions: dict[str, ImportedRevision] = {}
        self.builds: dict[str, BuildArtifact] = {}
        self.sessions: dict[str, PlaySession] = {}
        self.observations: dict[str, ObservationArtifact] = {}
        self.gates: dict[str, GateEvidence] = {}

    @classmethod
    def from_snapshot(
        cls, snapshot: EvidenceSnapshot, *, events: EventStore | None = None
    ) -> EvidenceGraph:
        graph = cls(events=events)
        graph.current_project_id = snapshot.current_project_id
        graph.project_revisions = {item.id: item for item in snapshot.project_revisions}
        graph.imported_revisions = {item.id: item for item in snapshot.imported_revisions}
        graph.builds = {item.id: item for item in snapshot.builds}
        graph.sessions = {item.id: item for item in snapshot.sessions}
        graph.observations = {item.id: item for item in snapshot.observations}
        graph.gates = {item.id: item for item in snapshot.gates}
        if graph.current_project_id is not None:
            graph._require_project(graph.current_project_id)
        return graph

    def snapshot(self) -> EvidenceSnapshot:
        return EvidenceSnapshot(
            current_project_id=self.current_project_id,
            project_revisions=tuple(self.project_revisions.values()),
            imported_revisions=tuple(self.imported_revisions.values()),
            builds=tuple(self.builds.values()),
            sessions=tuple(self.sessions.values()),
            observations=tuple(self.observations.values()),
            gates=tuple(self.gates.values()),
        )

    def commit_project_revision(
        self,
        *,
        source_tree_hash: str,
        diff_hash: str,
        asset_graph_digest: str,
        changed_paths: tuple[str, ...] = (),
    ) -> ProjectRevision:
        generation = (
            0
            if self.current_project_id is None
            else self.project_revisions[self.current_project_id].generation + 1
        )
        payload = {
            "generation": generation,
            "source_tree_hash": source_tree_hash,
            "diff_hash": diff_hash,
            "asset_graph_digest": asset_graph_digest,
            "changed_paths": sorted(changed_paths),
        }
        revision = ProjectRevision(
            id=_stable_id("project", payload),
            generation=generation,
            source_tree_hash=source_tree_hash,
            diff_hash=diff_hash,
            asset_graph_digest=asset_graph_digest,
            changed_paths=tuple(sorted(changed_paths)),
        )
        self._invalidate_downstream()
        self.project_revisions[revision.id] = revision
        self.current_project_id = revision.id
        self._emit(
            EventKind.PROJECT_MUTATION_COMMITTED,
            subject_id=revision.id,
            payload=revision.model_dump(mode="json"),
        )
        self._emit(
            EventKind.CANDIDATE_GENERATION_ADVANCED,
            subject_id=revision.id,
            payload={"generation": generation},
        )
        return revision

    def record_imported_revision(
        self,
        *,
        project_revision_id: str,
        engine_version: str,
        plugin_digest: str,
        import_settings_digest: str,
        environment_digest: str,
        cache_epoch: int = 0,
        artifact_hashes: tuple[str, ...] = (),
    ) -> ImportedRevision:
        self._require_current_project(project_revision_id)
        payload = {
            "project_revision_id": project_revision_id,
            "engine_version": engine_version,
            "plugin_digest": plugin_digest,
            "import_settings_digest": import_settings_digest,
            "environment_digest": environment_digest,
            "cache_epoch": cache_epoch,
            "artifact_hashes": artifact_hashes,
        }
        imported = ImportedRevision(
            id=_stable_id("import", payload),
            project_revision_id=project_revision_id,
            engine_version=engine_version,
            plugin_digest=plugin_digest,
            import_settings_digest=import_settings_digest,
            environment_digest=environment_digest,
            cache_epoch=cache_epoch,
            artifact_hashes=artifact_hashes,
        )
        self.imported_revisions[imported.id] = imported
        return imported

    def record_build(
        self,
        *,
        imported_revision_id: str,
        target_platform: str,
        build_configuration: str,
        environment_digest: str,
        artifact_hashes: tuple[str, ...],
    ) -> BuildArtifact:
        imported = self._require_import(imported_revision_id)
        self._require_fresh(imported.freshness, imported.id)
        self._require_current_project(imported.project_revision_id)
        payload = {
            "imported_revision_id": imported_revision_id,
            "target_platform": target_platform,
            "build_configuration": build_configuration,
            "environment_digest": environment_digest,
            "artifact_hashes": artifact_hashes,
        }
        build = BuildArtifact(id=_stable_id("build", payload), **payload)
        self.builds[build.id] = build
        return build

    def start_session(
        self,
        *,
        parent_kind: EvidenceSubjectKind,
        parent_id: str,
        entry_point: str,
        scenario_id: str | None = None,
        seed: int,
        fixed_timestep_seconds: float | None,
        environment_digest: str,
    ) -> PlaySession:
        for session in self.sessions.values():
            if session.state is PlaySessionState.ACTIVE:
                raise EvidenceError("an active play session already exists")
        self._require_fresh_subject(parent_kind, parent_id)
        payload = {
            "parent_kind": parent_kind.value,
            "parent_id": parent_id,
            "entry_point": entry_point,
            "scenario_id": scenario_id,
            "seed": seed,
            "fixed_timestep_seconds": fixed_timestep_seconds,
            "environment_digest": environment_digest,
            "ordinal": len(self.sessions) + 1,
        }
        session = PlaySession(
            id=_stable_id("session", payload),
            parent_kind=parent_kind,
            parent_id=parent_id,
            entry_point=entry_point,
            scenario_id=scenario_id,
            seed=seed,
            fixed_timestep_seconds=fixed_timestep_seconds,
            environment_digest=environment_digest,
        )
        self.sessions[session.id] = session
        self._emit(
            EventKind.PLAY_SESSION_STARTED,
            subject_id=session.id,
            payload=session.model_dump(mode="json"),
        )
        return session

    def bind_input_trace(self, session_id: str, trace_hash: str) -> PlaySession:
        session = self._require_active_session(session_id)
        updated = session.model_copy(update={"input_trace_hash": trace_hash})
        self.sessions[session_id] = updated
        return updated

    def stop_session(self, session_id: str, *, crashed: bool = False) -> PlaySession:
        session = self._require_active_session(session_id)
        state = PlaySessionState.CRASHED if crashed else PlaySessionState.STOPPED
        updated = session.model_copy(update={"state": state, "ended_at": datetime.now(UTC)})
        self.sessions[session_id] = updated
        self._emit(
            EventKind.PLAY_SESSION_CRASHED if crashed else EventKind.PLAY_SESSION_STOPPED,
            subject_id=session_id,
            payload={"state": state.value},
        )
        if crashed:
            self._mark_runtime_uncertain(session)
        return updated

    def record_observation(
        self,
        *,
        subject_kind: EvidenceSubjectKind,
        subject_id: str,
        kind: ObservationKind,
        artifact_hashes: tuple[str, ...],
        environment_digest: str,
        input_trace_hash: str | None = None,
        frame_start: int | None = None,
        frame_end: int | None = None,
        camera: str | None = None,
        width: int | None = None,
        height: int | None = None,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> ObservationArtifact:
        self._require_fresh_subject(subject_kind, subject_id)
        if subject_kind is EvidenceSubjectKind.PLAY_SESSION:
            session = self._require_usable_session(subject_id, active_only=True)
            if input_trace_hash is None:
                input_trace_hash = session.input_trace_hash
            if input_trace_hash is None:
                raise EvidenceError("runtime observation requires an input trace")
            if input_trace_hash != session.input_trace_hash:
                raise EvidenceError("runtime observation input trace does not match its session")
            if environment_digest != session.environment_digest:
                raise EvidenceError("runtime observation environment does not match its session")
        payload = {
            "subject_kind": subject_kind.value,
            "subject_id": subject_id,
            "kind": kind.value,
            "artifact_hashes": artifact_hashes,
            "environment_digest": environment_digest,
            "input_trace_hash": input_trace_hash,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "camera": camera,
            "width": width,
            "height": height,
            "metadata": metadata or {},
        }
        observation = ObservationArtifact(id=_stable_id("observation", payload), **payload)
        self.observations[observation.id] = observation
        self._emit(
            EventKind.OBSERVATION_CAPTURED,
            subject_id=observation.id,
            payload=observation.model_dump(mode="json"),
        )
        return observation

    def record_gate(
        self,
        *,
        requirement_id: str,
        subject_kind: EvidenceSubjectKind,
        subject_id: str,
        status: GateEvidenceStatus,
        environment_digest: str,
        evaluator_digest: str,
        artifact_hashes: tuple[str, ...] = (),
        detail: str = "",
        score: float | None = None,
        uncertainty: float | None = None,
    ) -> GateEvidence:
        self._require_fresh_subject(subject_kind, subject_id)
        payload = {
            "requirement_id": requirement_id,
            "subject_kind": subject_kind.value,
            "subject_id": subject_id,
            "status": status.value,
            "environment_digest": environment_digest,
            "evaluator_digest": evaluator_digest,
            "artifact_hashes": artifact_hashes,
            "detail": detail,
            "score": score,
            "uncertainty": uncertainty,
        }
        gate = GateEvidence(id=_stable_id("gate", payload), **payload)
        self.gates[gate.id] = gate
        self._emit(
            EventKind.GATE_COMPLETED,
            subject_id=gate.id,
            payload=gate.model_dump(mode="json"),
        )
        return gate

    def fresh_passing_gate(
        self, requirement_id: str, environment_digest: str
    ) -> GateEvidence | None:
        candidates = tuple(
            gate
            for gate in self.gates.values()
            if gate.requirement_id == requirement_id
            and gate.status is GateEvidenceStatus.PASS
            and gate.environment_digest == environment_digest
            and self.subject_is_fresh(gate.subject_kind, gate.subject_id)
        )
        return candidates[-1] if candidates else None

    def is_current_descendant(self, kind: EvidenceSubjectKind, subject_id: str) -> bool:
        if self.current_project_id is None:
            return False
        return self.project_ancestor(kind, subject_id) == self.current_project_id

    def subject_is_fresh(self, kind: EvidenceSubjectKind, subject_id: str) -> bool:
        try:
            self._require_fresh_subject(kind, subject_id)
        except EvidenceError:
            return False
        return True

    def invalidate_engine_state(self, *, uncertain: bool = False) -> None:
        freshness = EvidenceFreshness.UNCERTAIN if uncertain else EvidenceFreshness.STALE
        self.imported_revisions = {
            key: value.model_copy(update={"freshness": freshness})
            for key, value in self.imported_revisions.items()
        }
        self.builds = {
            key: value.model_copy(update={"freshness": freshness})
            for key, value in self.builds.items()
        }
        self.observations = {
            key: value.model_copy(update={"freshness": freshness})
            for key, value in self.observations.items()
        }
        self.sessions = {
            key: value.model_copy(
                update={
                    "state": (
                        PlaySessionState.CRASHED
                        if uncertain and value.state is PlaySessionState.ACTIVE
                        else (
                            PlaySessionState.INVALID
                            if value.state is PlaySessionState.ACTIVE
                            else value.state
                        )
                    )
                }
            )
            for key, value in self.sessions.items()
        }

    def project_ancestor(self, kind: EvidenceSubjectKind, subject_id: str) -> str:
        if kind is EvidenceSubjectKind.PROJECT_REVISION:
            return self._require_project(subject_id).id
        if kind is EvidenceSubjectKind.IMPORTED_REVISION:
            return self._require_import(subject_id).project_revision_id
        if kind is EvidenceSubjectKind.BUILD_ARTIFACT:
            build = self._require_build(subject_id)
            return self._require_import(build.imported_revision_id).project_revision_id
        if kind is EvidenceSubjectKind.PLAY_SESSION:
            session = self._require_session(subject_id)
            return self.project_ancestor(session.parent_kind, session.parent_id)
        if kind is EvidenceSubjectKind.OBSERVATION:
            observation = self._require_observation(subject_id)
            return self.project_ancestor(observation.subject_kind, observation.subject_id)
        raise EvidenceError(f"unknown evidence subject kind: {kind}")

    def _invalidate_downstream(self) -> None:
        self.imported_revisions = {
            key: value.model_copy(update={"freshness": EvidenceFreshness.STALE})
            for key, value in self.imported_revisions.items()
        }
        self.builds = {
            key: value.model_copy(update={"freshness": EvidenceFreshness.STALE})
            for key, value in self.builds.items()
        }
        self.observations = {
            key: value.model_copy(update={"freshness": EvidenceFreshness.STALE})
            for key, value in self.observations.items()
        }
        self.sessions = {
            key: value.model_copy(
                update={
                    "state": (
                        PlaySessionState.INVALID
                        if value.state is PlaySessionState.ACTIVE
                        else value.state
                    )
                }
            )
            for key, value in self.sessions.items()
        }

    def _mark_runtime_uncertain(self, session: PlaySession) -> None:
        project_id = self.project_ancestor(session.parent_kind, session.parent_id)
        self.imported_revisions = {
            key: value.model_copy(
                update={
                    "freshness": (
                        EvidenceFreshness.UNCERTAIN
                        if value.project_revision_id == project_id
                        else value.freshness
                    )
                }
            )
            for key, value in self.imported_revisions.items()
        }

    def _require_fresh_subject(self, kind: EvidenceSubjectKind, subject_id: str) -> None:
        if kind is EvidenceSubjectKind.PROJECT_REVISION:
            self._require_current_project(subject_id)
        elif kind is EvidenceSubjectKind.IMPORTED_REVISION:
            imported = self._require_import(subject_id)
            self._require_fresh(imported.freshness, subject_id)
            self._require_current_project(imported.project_revision_id)
        elif kind is EvidenceSubjectKind.BUILD_ARTIFACT:
            build = self._require_build(subject_id)
            self._require_fresh(build.freshness, subject_id)
            self._require_fresh_subject(
                EvidenceSubjectKind.IMPORTED_REVISION, build.imported_revision_id
            )
        elif kind is EvidenceSubjectKind.PLAY_SESSION:
            self._require_usable_session(subject_id)
        elif kind is EvidenceSubjectKind.OBSERVATION:
            observation = self._require_observation(subject_id)
            self._require_fresh(observation.freshness, subject_id)
            self._require_fresh_subject(observation.subject_kind, observation.subject_id)

    @staticmethod
    def _require_fresh(freshness: EvidenceFreshness, subject_id: str) -> None:
        if freshness is not EvidenceFreshness.FRESH:
            raise EvidenceError(f"evidence subject is not fresh: {subject_id}")

    def _require_current_project(self, subject_id: str) -> ProjectRevision:
        revision = self._require_project(subject_id)
        if revision.id != self.current_project_id:
            raise EvidenceError("project revision is not current")
        return revision

    def _require_project(self, subject_id: str) -> ProjectRevision:
        try:
            return self.project_revisions[subject_id]
        except KeyError as error:
            raise EvidenceError(f"unknown project revision: {subject_id}") from error

    def _require_import(self, subject_id: str) -> ImportedRevision:
        try:
            return self.imported_revisions[subject_id]
        except KeyError as error:
            raise EvidenceError(f"unknown imported revision: {subject_id}") from error

    def _require_build(self, subject_id: str) -> BuildArtifact:
        try:
            return self.builds[subject_id]
        except KeyError as error:
            raise EvidenceError(f"unknown build artifact: {subject_id}") from error

    def _require_session(self, subject_id: str) -> PlaySession:
        try:
            return self.sessions[subject_id]
        except KeyError as error:
            raise EvidenceError(f"unknown play session: {subject_id}") from error

    def _require_active_session(self, subject_id: str) -> PlaySession:
        return self._require_usable_session(subject_id, active_only=True)

    def _require_usable_session(self, subject_id: str, *, active_only: bool = False) -> PlaySession:
        session = self._require_session(subject_id)
        allowed = (
            {PlaySessionState.ACTIVE}
            if active_only
            else {
                PlaySessionState.ACTIVE,
                PlaySessionState.STOPPED,
            }
        )
        if session.state not in allowed:
            qualifier = "active" if active_only else "usable"
            raise EvidenceError(f"play session is not {qualifier}: {subject_id}")
        if not self.is_current_descendant(EvidenceSubjectKind.PLAY_SESSION, subject_id):
            raise EvidenceError("play session does not descend from current project")
        self._require_fresh_subject(session.parent_kind, session.parent_id)
        return session

    def _require_observation(self, subject_id: str) -> ObservationArtifact:
        try:
            return self.observations[subject_id]
        except KeyError as error:
            raise EvidenceError(f"unknown observation: {subject_id}") from error

    def _emit(
        self,
        kind: EventKind,
        *,
        subject_id: str,
        payload: dict[str, object],
    ) -> None:
        if self.events is not None:
            self.events.append(kind, subject_id=subject_id, payload=payload)


def environment_digest(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def empty_digest() -> str:
    return hashlib.sha256(b"").hexdigest()


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
