from __future__ import annotations

from gameforge.harness.events import InMemoryEventStore
from gameforge.harness.evidence import (
    EvidenceFreshness,
    EvidenceGraph,
    EvidenceSubjectKind,
    GateEvidenceStatus,
    ObservationKind,
    PlaySessionState,
    empty_digest,
)


def test_evidence_lineage_invalidates_all_downstream_subjects() -> None:
    digest = empty_digest()
    graph = EvidenceGraph(events=InMemoryEventStore("run"))
    first = graph.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    imported = graph.record_imported_revision(
        project_revision_id=first.id,
        engine_version="4.4",
        plugin_digest=digest,
        import_settings_digest=digest,
        environment_digest=digest,
    )
    build = graph.record_build(
        imported_revision_id=imported.id,
        target_platform="linux",
        build_configuration="debug",
        environment_digest=digest,
        artifact_hashes=(digest,),
    )
    session = graph.start_session(
        parent_kind=EvidenceSubjectKind.BUILD_ARTIFACT,
        parent_id=build.id,
        entry_point="Main",
        seed=0,
        fixed_timestep_seconds=1 / 60,
        environment_digest=digest,
    )
    graph.bind_input_trace(session.id, digest)
    observation = graph.record_observation(
        subject_kind=EvidenceSubjectKind.PLAY_SESSION,
        subject_id=session.id,
        kind=ObservationKind.SCREENSHOT,
        artifact_hashes=(digest,),
        environment_digest=digest,
        input_trace_hash=digest,
        camera="main",
        width=1280,
        height=720,
    )
    gate = graph.record_gate(
        requirement_id="visual",
        subject_kind=EvidenceSubjectKind.OBSERVATION,
        subject_id=observation.id,
        status=GateEvidenceStatus.PASS,
        environment_digest=digest,
        evaluator_digest=digest,
    )

    assert graph.fresh_passing_gate("visual", digest) == gate
    graph.commit_project_revision(
        source_tree_hash="1" * 64,
        diff_hash="2" * 64,
        asset_graph_digest="3" * 64,
        changed_paths=("scene.tscn",),
    )

    assert graph.imported_revisions[imported.id].freshness is EvidenceFreshness.STALE
    assert graph.builds[build.id].freshness is EvidenceFreshness.STALE
    assert graph.sessions[session.id].state is PlaySessionState.INVALID
    assert graph.observations[observation.id].freshness is EvidenceFreshness.STALE
    assert graph.fresh_passing_gate("visual", digest) is None


def test_evidence_graph_round_trips_snapshot() -> None:
    digest = empty_digest()
    graph = EvidenceGraph()
    revision = graph.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )

    restored = EvidenceGraph.from_snapshot(graph.snapshot())

    assert restored.current_project_id == revision.id
    assert (
        restored.project_ancestor(EvidenceSubjectKind.PROJECT_REVISION, revision.id) == revision.id
    )
