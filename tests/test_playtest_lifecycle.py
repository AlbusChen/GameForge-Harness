from __future__ import annotations

from pathlib import Path

import pytest

from gameforge.harness.artifacts import ContentAddressedArtifactStore
from gameforge.harness.capabilities import (
    CapabilityBroker,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPhase,
    CapabilityRegistry,
    ConcurrencyMode,
)
from gameforge.harness.engine_lifecycle import (
    EngineLifecycleCoordinator,
    EngineLifecycleError,
    EngineLifecycleState,
)
from gameforge.harness.events import InMemoryEventStore
from gameforge.harness.evidence import (
    EvidenceFreshness,
    EvidenceGraph,
    EvidenceSubjectKind,
    ObservationKind,
    PlaySessionState,
    empty_digest,
)
from gameforge.harness.game_tasks import (
    CaptureKind,
    CaptureRequest,
    PlaytestScenario,
    RuntimeProbe,
    TimedInputAction,
)
from gameforge.harness.playtest import PlaytestBindings, PlaytestRunner


def _register(
    registry: CapabilityRegistry,
    name: str,
    phase: CapabilityPhase,
    handler: object,
) -> None:
    mutating = phase is not CapabilityPhase.OBSERVE
    registry.register(
        CapabilityDescriptor(
            name=name,
            version="1",
            purpose=name,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            effects=(CapabilityEffect.ENGINE_STATE if mutating else CapabilityEffect.PROJECT_READ,),
            phase=phase,
            concurrency=(
                ConcurrencyMode.EXCLUSIVE_ENGINE if mutating else ConcurrencyMode.PARALLEL_READ
            ),
        ),
        handler,  # type: ignore[arg-type]
    )


def test_scripted_game_lifecycle_is_replayable_and_revision_bound(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    actions: list[str] = []
    _register(registry, "sync", CapabilityPhase.ENGINE_SYNC, lambda arguments: {"ok": True})
    _register(
        registry,
        "launch",
        CapabilityPhase.PLAYTEST,
        lambda arguments: {"session_id": "engine-session"},
    )
    _register(
        registry,
        "action",
        CapabilityPhase.PLAYTEST,
        lambda arguments: actions.append(str(arguments["action"]["action"])) or {"ok": True},
    )
    _register(
        registry,
        "probe",
        CapabilityPhase.PLAYTEST,
        lambda arguments: {"grounded": True, "x": 4},
    )
    _register(
        registry,
        "capture",
        CapabilityPhase.CAPTURE,
        lambda arguments: {"frame": 3},
    )
    _register(registry, "stop", CapabilityPhase.PLAYTEST, lambda arguments: {"ok": True})
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    broker = CapabilityBroker(run_id="run", registry=registry, events=events)
    lifecycle = EngineLifecycleCoordinator(
        broker=broker,
        evidence=evidence,
        events=events,
        engine_version="4.4",
        plugin_digest=digest,
        import_settings_digest=digest,
        environment_digest=digest,
    )
    broker.prerequisites = lifecycle
    lifecycle.synchronize("sync", {})
    runner = PlaytestRunner(
        broker=broker,
        lifecycle=lifecycle,
        evidence=evidence,
        events=events,
        artifacts=ContentAddressedArtifactStore(tmp_path / "artifacts", events=events),
        bindings=PlaytestBindings(
            launch_capability="launch",
            stop_capability="stop",
            action_capability="action",
            probe_capability="probe",
            capture_capabilities={CaptureKind.SCREENSHOT: "capture"},
        ),
    )
    scenario = PlaytestScenario(
        id="jump",
        entry_point="Main",
        fixed_timestep_seconds=1 / 60,
        actions=(TimedInputAction(action="jump", frame=2),),
        public_probes=(RuntimeProbe(name="player", at_frame=3),),
        captures=(CaptureRequest(kind=CaptureKind.SCREENSHOT, at_frame=3),),
    )

    execution = runner.run(scenario)[0]

    assert actions == ["jump"]
    assert lifecycle.state is EngineLifecycleState.READY
    assert execution.bundle.structured[0].kind is ObservationKind.STRUCTURED
    assert execution.bundle.structured[0].metadata["selector"] == "{}"
    assert execution.bundle.media[0].kind is ObservationKind.SCREENSHOT
    evidence.commit_project_revision(
        source_tree_hash="1" * 64,
        diff_hash="2" * 64,
        asset_graph_digest="3" * 64,
    )
    with pytest.raises(EngineLifecycleError, match="imported revision"):
        lifecycle.launch(
            "launch",
            {},
            entry_point="Main",
            seed=0,
            fixed_timestep_seconds=1 / 60,
        )


def test_engine_reset_invalidates_imported_and_runtime_evidence() -> None:
    registry = CapabilityRegistry()
    _register(registry, "sync", CapabilityPhase.ENGINE_SYNC, lambda arguments: {"ok": True})
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    broker = CapabilityBroker(run_id="run", registry=registry, events=events)
    lifecycle = EngineLifecycleCoordinator(
        broker=broker,
        evidence=evidence,
        events=events,
        engine_version="4.4",
        plugin_digest=digest,
        import_settings_digest=digest,
        environment_digest=digest,
    )
    imported, _ = lifecycle.synchronize("sync", {})

    lifecycle.reset()

    assert lifecycle.state is EngineLifecycleState.COLD
    assert not evidence.subject_is_fresh(
        EvidenceSubjectKind.IMPORTED_REVISION,
        imported.id,
    )


def test_engine_crash_requires_clean_reset_and_resync() -> None:
    registry = CapabilityRegistry()
    _register(registry, "sync", CapabilityPhase.ENGINE_SYNC, lambda arguments: {"ok": True})
    _register(
        registry,
        "launch",
        CapabilityPhase.PLAYTEST,
        lambda arguments: {"session_id": "engine-session"},
    )
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    broker = CapabilityBroker(run_id="run", registry=registry, events=events)
    lifecycle = EngineLifecycleCoordinator(
        broker=broker,
        evidence=evidence,
        events=events,
        engine_version="4.4",
        plugin_digest=digest,
        import_settings_digest=digest,
        environment_digest=digest,
    )
    imported, _ = lifecycle.synchronize("sync", {})
    session, _ = lifecycle.launch(
        "launch",
        {},
        entry_point="Main",
        scenario_id="crash",
        seed=0,
        fixed_timestep_seconds=1 / 60,
    )

    lifecycle.mark_crashed("fixture crash")

    assert lifecycle.state is EngineLifecycleState.DIRTY_OR_CRASHED
    assert evidence.sessions[session.id].state is PlaySessionState.CRASHED
    assert evidence.imported_revisions[imported.id].freshness is EvidenceFreshness.UNCERTAIN
    with pytest.raises(EngineLifecycleError, match="engine must be"):
        lifecycle.launch(
            "launch",
            {},
            entry_point="Main",
            seed=0,
            fixed_timestep_seconds=1 / 60,
        )

    lifecycle.reset()
    recovered, _ = lifecycle.synchronize("sync", {})

    assert lifecycle.state is EngineLifecycleState.READY
    assert recovered.id != imported.id
    assert recovered.freshness is EvidenceFreshness.FRESH
